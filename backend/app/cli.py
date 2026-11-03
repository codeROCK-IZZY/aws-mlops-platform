"""Command-line tools: ``python -m app.cli <command>``.

migrate                            apply database migrations and create the S3 buckets
create-user --email E --role R     create a user (password from MLP_USER_PASSWORD or prompt)
worker                             run the background worker (pipeline runs)
drain                              process all queued jobs, then exit
demo                               demo users + a year of data replayed through the full lifecycle
monitor-latest                     monitor the newest month the champion has not seen (the daily schedule)
lifecycle --reset --out DIR        RESETS the platform, replays the year, writes the evaluation report
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import configure_logging

DEMO_USERS = {"erin": "engineer", "amir": "approver", "admin": "admin", "vera": "viewer"}


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from app.ml import storage

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = get_settings().database_url
    cfg.attributes["skip_logging_config"] = True
    command.upgrade(cfg, "head")
    storage.ensure_buckets()
    print("migrations applied, buckets ready")


def _create_user(email: str, role: str, password: str | None = None) -> None:
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.db.models import User
    from app.db.session import session_scope

    password = password or os.environ.get("MLP_USER_PASSWORD") or getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        sys.exit("password must be at least 12 characters")
    with session_scope() as db:
        if db.scalar(select(User).where(User.email == email.lower())):
            print(f"user {email} exists — skipped")
            return
        db.add(
            User(
                email=email.lower(),
                full_name=email.split("@")[0].title(),
                password_hash=hash_password(password),
                role=role,
            )
        )
    print(f"user {email} ({role}) created")


def demo_users() -> None:
    password = os.environ.get("MLP_DEMO_PASSWORD")
    if not password or len(password) < 12:
        sys.exit("set MLP_DEMO_PASSWORD (12+ characters) for the demo users")
    for name, role in DEMO_USERS.items():
        _create_user(f"{name}@example.com", role, password)


def monitor_latest() -> None:
    """Scheduled job: monitor the newest validated month the current champion has not seen yet.

    Any retraining it triggers is queued and run here too, so one scheduled task does the whole loop.
    """
    from sqlalchemy import select

    from app.db.models import MonitoringReport
    from app.db.session import new_session
    from app.jobs.worker import drain
    from app.ml import pipeline, registry

    champion = registry.get_alias(registry.CHAMPION)
    if champion is None:
        print("no champion model yet; nothing to monitor")
        return
    with new_session() as db:
        seen = set(db.scalars(select(MonitoringReport.month).where(MonitoringReport.model_version == champion.version)))
        months = [m for m in pipeline.validated_months(db) if m not in seen]
        if not months:
            print(f"champion v{champion.version} has already been monitored on every validated month")
            return
        run = pipeline.create_run(db, "monitor", {"month": months[-1]}, trigger="schedule", created_by="schedule")
        db.commit()
        print(f"monitoring {months[-1]} with champion v{champion.version} (run {run.id})")
    print(f"processed {drain()} jobs")


def _lifecycle(out: Path | None, reset: bool, gate: bool, leave_pending: bool) -> dict[str, Any]:
    from app.evaluation.lifecycle import gate_failures, run_lifecycle, write_report

    if not reset:
        sys.exit("lifecycle deletes all platform data and model versions; pass --reset to confirm")
    report = run_lifecycle(leave_last_pending=leave_pending)
    if out:
        write_report(report, out)
    print(json.dumps(report["summary"], indent=1, default=str))
    if gate and (failures := gate_failures(report)):
        sys.exit("lifecycle gate failed: " + "; ".join(failures))
    return report


def main(argv: list[str] | None = None) -> None:
    configure_logging(get_settings().log_level, as_json=False)
    p = argparse.ArgumentParser(
        prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--role", choices=["admin", "approver", "engineer", "viewer"], default="engineer")
    sub.add_parser("worker")
    sub.add_parser("drain")
    sub.add_parser("monitor-latest")
    sub.add_parser("demo")
    lc = sub.add_parser("lifecycle")
    lc.add_argument("--out", type=Path, default=Path("../eval/reports"))
    lc.add_argument("--reset", action="store_true", help="confirm that all platform data may be deleted")
    lc.add_argument("--gate", action="store_true", help="exit non-zero if a lifecycle check fails")
    args = p.parse_args(argv)
    if args.cmd == "migrate":
        _migrate()
    elif args.cmd == "create-user":
        _create_user(args.email, args.role)
    elif args.cmd == "worker":
        from app.jobs.worker import run_forever

        run_forever()
    elif args.cmd == "drain":
        from app.jobs.worker import drain

        print(f"processed {drain()} jobs")
    elif args.cmd == "monitor-latest":
        monitor_latest()
    elif args.cmd == "demo":
        _migrate()
        demo_users()
        _lifecycle(None, reset=True, gate=False, leave_pending=True)
    elif args.cmd == "lifecycle":
        _lifecycle(args.out, args.reset, args.gate, leave_pending=False)


if __name__ == "__main__":
    main()
