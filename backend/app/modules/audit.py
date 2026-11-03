"""Tamper-evident audit trail.

Every event stores ``hash = SHA-256(prev_hash + canonical JSON of the event)``. The table rejects
UPDATE and DELETE (database trigger), and ``verify_chain`` recomputes every hash, so an edited or
removed row breaks the chain at a specific event id.

Writers take a transaction-scoped advisory lock so concurrent API and worker processes append in a
single, well-defined order.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import AuditEvent

GENESIS = "0" * 64
_LOCK_KEY = 7_401_220  # arbitrary constant identifying the audit-chain lock


def _canonical(
    ts: datetime,
    actor: str,
    action: str,
    entity: str,
    entity_id: str | None,
    run_id: uuid.UUID | None,
    data: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "ts": ts.astimezone(UTC).isoformat(timespec="microseconds"),
            "actor": actor,
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "run_id": str(run_id) if run_id else None,
            "data": data,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hash(prev: str, payload: str) -> str:
    return hashlib.sha256((prev + payload).encode()).hexdigest()


def record(
    db: Session,
    actor: uuid.UUID | str | None,
    action: str,
    entity: str,
    entity_id: str | None = None,
    *,
    run_id: uuid.UUID | None = None,
    **data: Any,
) -> AuditEvent:
    """Append an event to the current transaction (committed by the caller).
    ``actor`` is a user id, or a label such as ``agent:refund`` or ``system``."""
    actor_label = f"user:{actor}" if isinstance(actor, uuid.UUID) else (actor or "system")
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = db.scalar(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1)) or GENESIS
    ts = datetime.now(UTC)
    data = json.loads(json.dumps(data, default=str))  # store exactly what was hashed
    payload = _canonical(ts, actor_label, action, entity, entity_id, run_id, data)
    event = AuditEvent(
        ts=ts,
        actor=actor_label,
        action=action,
        entity=entity,
        entity_id=entity_id,
        run_id=run_id,
        data=data,
        prev_hash=prev,
        hash=_hash(prev, payload),
    )
    db.add(event)
    db.flush()
    return event


def verify_chain(db: Session) -> dict[str, Any]:
    """Recompute every hash in order. Returns the first broken event, if any."""
    prev = GENESIS
    count = 0
    for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id).execution_options(yield_per=1000)):
        expected = _hash(prev, _canonical(e.ts, e.actor, e.action, e.entity, e.entity_id, e.run_id, e.data))
        if e.prev_hash != prev or e.hash != expected:
            return {"valid": False, "events": count, "broken_at": e.id}
        prev = e.hash
        count += 1
    return {"valid": True, "events": count, "broken_at": None, "head": prev}
