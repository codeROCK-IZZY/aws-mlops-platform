"""Promotion gates: the checks a candidate must pass before it may replace the champion.

Both models are scored on the **same fresh holdout month**, each with its own stored threshold, so
the comparison is apples to apples. The gates:

====================  =========================================================================
``min_quality``       candidate PR-AUC ≥ an absolute floor
``no_regression``     candidate PR-AUC ≥ champion PR-AUC − tolerance (on today's data)
``calibration``       expected calibration error ≤ limit
``slice_recall``      recall at the operating threshold ≥ limit for *every* equipment type, so an
                      average can't hide a fleet the model has stopped protecting
``latency``           single-row p95 latency within the serving budget
``contract``          the holdout batch itself passed the data contract
``improvement``       (drift-triggered retrains only) candidate must *beat* the champion, so a
                      retrain that learned nothing new is not deployed just because it ran
====================  =========================================================================

All gates must pass. The report is stored with the pipeline run and as an MLflow artifact.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.config import Settings


@dataclass
class Check:
    name: str
    passed: bool
    value: float | None
    limit: float | None
    detail: str


@dataclass
class GateReport:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    candidate: dict[str, Any] = field(default_factory=dict)
    champion: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_gates(candidate: dict[str, Any], champion: dict[str, Any] | None, s: Settings, *,
              contract_passed: bool = True, min_improvement: float = 0.0) -> GateReport:  # fmt: skip
    checks = [
        Check("min_quality", candidate["pr_auc"] >= s.gate_min_pr_auc, candidate["pr_auc"], s.gate_min_pr_auc,
              f"PR-AUC {candidate['pr_auc']:.3f} (floor {s.gate_min_pr_auc:.2f})"),
        Check("calibration", candidate["ece"] <= s.gate_max_ece, candidate["ece"], s.gate_max_ece,
              f"expected calibration error {candidate['ece']:.3f} (max {s.gate_max_ece:.2f})"),
        Check("latency", candidate["p95_latency_ms"] <= s.gate_max_p95_latency_ms, candidate["p95_latency_ms"],
              s.gate_max_p95_latency_ms, f"p95 {candidate['p95_latency_ms']:.1f} ms (budget {s.gate_max_p95_latency_ms:.0f} ms)"),
        Check("contract", contract_passed, None, None, "holdout batch passed the data contract" if contract_passed
              else "holdout batch failed the data contract"),
    ]  # fmt: skip
    slices = candidate.get("slices", {})
    weakest = min(slices.items(), key=lambda kv: kv[1]["recall"]) if slices else None
    if weakest:
        name, sl = weakest
        checks.append(Check("slice_recall", sl["recall"] >= s.gate_min_slice_recall, sl["recall"], s.gate_min_slice_recall,
                            f"weakest slice {name}: recall {sl['recall']:.2f} (min {s.gate_min_slice_recall:.2f})"))  # fmt: skip
    if champion is None:
        checks.append(Check("no_regression", True, None, None, "no champion yet: first model"))
    else:
        floor = champion["pr_auc"] - s.gate_max_regression
        checks.append(Check("no_regression", candidate["pr_auc"] >= floor, candidate["pr_auc"], round(floor, 4),
                            f"PR-AUC {candidate['pr_auc']:.3f} vs champion {champion['pr_auc']:.3f} on the same month"))  # fmt: skip
        if min_improvement > 0:
            target = champion["pr_auc"] + min_improvement
            checks.append(Check("improvement", candidate["pr_auc"] >= target, candidate["pr_auc"], round(target, 4),
                                f"a retrain triggered by drift must beat the champion by {min_improvement:.2f} PR-AUC"))  # fmt: skip
    return GateReport(all(c.passed for c in checks), checks, candidate, champion)
