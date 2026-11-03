"""Lifecycle evaluation: does the platform keep a model healthy through a year of change?

The demo year (``generator.default_timeline``) has six stable months, then sensor drift (months
7–8), a fleet shift (9) and a new failure mode (10–12). The evaluation replays that year through the
real pipeline (the same runs, gates and registry the API uses):

1. ingest and validate every month;
2. train the first model on months 2–5, evaluated on month 6, and promote it;
3. for each later month: monitor (drift + measured performance); when monitoring raises an alert,
   the retraining run it triggers goes through the gates; candidates that pass are approved (an
   approver stands in for the human decision).

It then measures what the platform bought:

* **detection:** months from the start of each drift scenario to the first alert
* **false alarms:** alerts on a stable month, in the year and on 12 independent stable months
* **recovery:** PR-AUC of the platform-managed champion vs. a *static* copy of the first model
  that is never retrained, month by month on the same data
* **safety:** every promoted version passed every gate; failed candidates never reached production
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

import mlflow
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import PipelineRun, Promotion
from app.db.session import new_session
from app.jobs.worker import drain
from app.ml import deploy, metrics, pipeline, registry, storage
from app.ml.generator import LABEL, BatchSpec, default_timeline, generate_batch
from app.ml.train import FEATURES

RESET_TABLES = "predictions, monitoring_reports, promotions, pipeline_runs, datasets, jobs"
INITIAL_WINDOW = ["2026-02", "2026-03", "2026-04", "2026-05"]
INITIAL_HOLDOUT = "2026-06"
APPROVER = "lifecycle-approver@example.com"
STABLE_REPLICAS = 12


def reset(db: Session) -> None:
    db.execute(text(f"TRUNCATE {RESET_TABLES} RESTART IDENTITY CASCADE"))
    db.commit()
    storage.ensure_buckets()
    client = registry.client()
    with contextlib.suppress(mlflow.exceptions.MlflowException):
        client.delete_registered_model(get_settings().model_name)
    registry.load_model.cache_clear()


def _run(db: Session, kind: str, params: dict[str, Any], *, trigger: str = "manual") -> PipelineRun:
    run = pipeline.create_run(db, kind, params, trigger=trigger, created_by="lifecycle")
    db.commit()
    return _wait(db, run.id)


def _wait(db: Session, run_id: Any) -> PipelineRun:
    """Until the run has finished. Jobs run here, unless a worker that is also running claims them first."""
    deadline = time.monotonic() + 1800
    while True:
        drain()
        db.expire_all()
        fresh = db.get(PipelineRun, run_id)
        assert fresh is not None
        if fresh.status not in ("queued", "running"):
            return fresh
        if time.monotonic() > deadline:
            raise TimeoutError(f"run {run_id} did not finish")
        time.sleep(1)


def _pr_auc(version: str, month: str) -> float:
    df = storage.get_batch(month)
    p = registry.load_model(version).predict_proba(df[FEATURES])[:, 1]
    return float(metrics.evaluate(df[LABEL].to_numpy(), p, 0.5)["pr_auc"])


def _stability_check(version: str) -> dict[str, Any]:
    """False-alarm rate: the monitoring decision on independent *stable* months (no scenario).

    The replayed year has only one stable month after the first model, which says little about false
    alarms, so the same decision (``pipeline.assess``) is also run on STABLE_REPLICAS freshly
    generated stable months with other seeds. Any alert there is a false alarm.
    """
    s = get_settings()
    champion = registry.get_version(version)
    assert champion.run_id is not None
    reference = registry.load_reference(champion.run_id)
    model = registry.load_model(version)
    statuses = []
    for k in range(STABLE_REPLICAS):
        df = generate_batch(BatchSpec(INITIAL_HOLDOUT, seed=1000 + k))
        a = pipeline.assess(df, model.predict_proba(df[FEATURES])[:, 1], champion, reference, s)
        statuses.append(a.status)
    return {"months": len(statuses), "alerts": statuses.count("alert"), "warnings": statuses.count("warn")}


def run_lifecycle(*, leave_last_pending: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    db = new_session()
    try:
        reset(db)
        timeline = default_timeline()
        scenario_of = {b.month: b.scenario for b in timeline}
        for b in timeline:
            r = _run(db, "ingest", {"month": b.month, "scenario": b.scenario})
            if r.status != "succeeded":
                raise RuntimeError(f"ingest {b.month}: {r.status} {r.result}")

        first = _run(db, "train", {"train_months": INITIAL_WINDOW, "holdout_month": INITIAL_HOLDOUT})
        if first.status != "succeeded" or first.model_version is None:
            raise RuntimeError(f"initial training failed: {first.status} {first.result}")
        static_version = first.model_version
        stability = _stability_check(static_version)

        months = [b.month for b in timeline if b.month >= INITIAL_HOLDOUT]
        rows: list[dict[str, Any]] = []
        for i, month in enumerate(months):
            champion = registry.get_alias(registry.CHAMPION)
            assert champion is not None
            mon = _run(db, "monitor", {"month": month})
            report = mon.result or {}
            row: dict[str, Any] = {
                "month": month, "scenario": scenario_of[month], "champion": champion.version,
                "monitor_status": report.get("status"), "champion_pr_auc": report.get("pr_auc"),
                "static_pr_auc": round(_pr_auc(static_version, month), 4),
                "retrain": None, "candidate": None, "gates": None, "failed_gates": [], "promoted": False,
            }  # fmt: skip
            drift_step = next((s for s in mon.steps if s["name"] == "drift"), {})
            row["drifted"] = drift_step.get("detail", {}).get("drifted", [])
            row["max_psi"] = drift_step.get("detail", {}).get("max_feature")
            if report.get("retrain_run"):
                retrain = _wait(db, report["retrain_run"])
                row["retrain"] = retrain.status
                row["candidate"] = retrain.model_version
                gate_step = next((s for s in retrain.steps if s["name"] == "gates"), None)
                if gate_step:
                    row["gates"] = "passed" if gate_step["detail"].get("passed") else "failed"
                    row["failed_gates"] = [c["name"] for c in gate_step["detail"].get("checks", []) if not c["passed"]]
                promo = db.scalar(select(Promotion).where(Promotion.pipeline_run_id == retrain.id))
                last = i == len(months) - 1
                if promo and promo.status == "pending" and not (leave_last_pending and last):
                    pipeline.approve(db, promo, by=APPROVER)
                    db.commit()
                    row["promoted"] = True
            rows.append(row)

        champion = registry.get_alias(registry.CHAMPION)
        if champion is not None:
            deploy.deploy(champion.version)  # point a running model server at the final champion
        # Each month is scored by the champion in place at the start of that month (managed), and by the
        # first model, never retrained (static), on the same data.
        comparison = [{"month": r["month"], "scenario": r["scenario"], "managed_version": r["champion"],
                       "managed_pr_auc": r["champion_pr_auc"], "static_pr_auc": r["static_pr_auc"]} for r in rows]  # fmt: skip
        summary = _summarise(rows, comparison, scenario_of)
        summary["stable_replicas"] = stability
        summary["seconds"] = round(time.perf_counter() - started, 1)
        promotions = [
            {"version": p.version, "from": p.from_version, "status": p.status, "decided_by": p.decided_by,
             "gates": "passed" if (p.gate_report or {}).get("passed") else "n/a"}
            for p in db.scalars(select(Promotion).order_by(Promotion.created_at))
        ]  # fmt: skip
        return {"rows": rows, "comparison": comparison, "promotions": promotions, "summary": summary,
                "static_version": static_version}  # fmt: skip
    finally:
        db.close()


def _summarise(
    rows: list[dict[str, Any]], comparison: list[dict[str, Any]], scenario_of: dict[str, str]
) -> dict[str, Any]:
    detection: dict[str, Any] = {}
    for scenario in ("sensor_drift", "fleet_mix", "new_failure"):
        # Scenarios are cumulative ("sensor_drift+fleet_mix"), so a scenario is active from its first month on.
        start = min((m for m, s in scenario_of.items() if scenario in s.split("+")), default=None)
        alert = min((r["month"] for r in rows if start and r["month"] >= start and r["monitor_status"] == "alert"),
                    default=None)  # fmt: skip
        delay = None if start is None or alert is None else (int(alert[5:]) - int(start[5:]))
        detection[scenario] = {"starts": start, "first_alert": alert, "delay_months": delay}
    stable = [r for r in rows if r["scenario"] == "none"]
    drift_rows = [c for c in comparison if c["scenario"] != "none"]
    managed = [c["managed_pr_auc"] for c in drift_rows if c["managed_pr_auc"] is not None]
    static = [c["static_pr_auc"] for c in drift_rows]
    return {
        "detection": detection,
        "false_alarms": sum(1 for r in stable if r["monitor_status"] == "alert"),
        "stable_months_monitored": len(stable),
        "retrains_triggered": sum(1 for r in rows if r["retrain"]),
        "candidates_blocked_by_gates": sum(1 for r in rows if r["gates"] == "failed"),
        "promotions": sum(1 for r in rows if r["promoted"]),
        "mean_pr_auc_managed_during_drift": round(sum(managed) / len(managed), 4) if managed else None,
        "mean_pr_auc_static_during_drift": round(sum(static) / len(static), 4) if static else None,
    }


def gate_failures(report: dict[str, Any]) -> list[str]:
    s = report["summary"]
    out = []
    for scenario, d in s["detection"].items():
        if d["delay_months"] is None or d["delay_months"] > 1:
            out.append(f"{scenario} not detected within one month ({d})")
    if s["false_alarms"]:
        out.append(f"{s['false_alarms']} false alarm(s) on stable months")
    if s["stable_replicas"]["alerts"] > 1:
        out.append(
            f"{s['stable_replicas']['alerts']} alerts on {s['stable_replicas']['months']} independent stable months"
        )
    managed, static = s["mean_pr_auc_managed_during_drift"], s["mean_pr_auc_static_during_drift"]
    if managed is None or static is None or managed < static + 0.03:
        out.append(f"managed model not clearly better than a static one ({managed} vs {static})")
    return out


def write_report(report: dict[str, Any], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "lifecycle.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    s = report["summary"]
    lines = [
        "# Lifecycle evaluation",
        "",
        "A year of machine telemetry replayed through the real pipeline: six stable months, then sensor drift,",
        "a fleet shift and a new failure mode. Generated by `python -m app.cli lifecycle --reset`.",
        "",
        "## Month by month",
        "",
        "| Month | Scenario | Champion | Monitoring | Champion PR-AUC | Static model PR-AUC | Drifted features | Retrain → gates | Promoted |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["rows"]:
        retrain = "—" if not r["retrain"] else f"v{r['candidate']}: {r['gates'] or r['retrain']}" + (
            f" ({', '.join(r['failed_gates'])})" if r["failed_gates"] else "")  # fmt: skip
        lines.append(f"| {r['month']} | {r['scenario']} | v{r['champion']} | {r['monitor_status']} | {r['champion_pr_auc']} | "
                     f"{r['static_pr_auc']} | {', '.join(r['drifted']) or '—'} | {retrain} | {'yes' if r['promoted'] else '—'} |")  # fmt: skip
    lines += ["", "## Summary", "", "| Measure | Result |", "|---|---|"]
    for scenario, d in s["detection"].items():
        lines.append(f"| Detection of `{scenario}` | starts {d['starts']}, first alert {d['first_alert']} "
                     f"(**{d['delay_months']} month(s)**) |")  # fmt: skip
    lines += [
        f"| False alarms on stable months of the year | **{s['false_alarms']}** of {s['stable_months_monitored']} |",
        f"| False alarms on independent stable months (other seeds) | **{s['stable_replicas']['alerts']}** of "
        f"{s['stable_replicas']['months']} ({s['stable_replicas']['warnings']} warnings) |",
        f"| Retraining runs triggered by monitoring | {s['retrains_triggered']} |",
        f"| Candidates blocked by the promotion gates | {s['candidates_blocked_by_gates']} |",
        f"| Promotions | {s['promotions']} |",
        f"| Mean PR-AUC during drift: platform-managed | **{s['mean_pr_auc_managed_during_drift']}** |",
        f"| Mean PR-AUC during drift: static model (never retrained) | {s['mean_pr_auc_static_during_drift']} |",
        f"| Runtime | {s['seconds']} s |",
        "",
    ]
    (out / "lifecycle.md").write_text("\n".join(lines), encoding="utf-8")
