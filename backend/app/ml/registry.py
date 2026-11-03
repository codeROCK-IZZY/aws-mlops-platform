"""The model registry (MLflow) and the two aliases that matter.

* ``champion``: the version serving production traffic.
* ``challenger``: the newest version that passed the promotion gates and is waiting for approval
  (or for the canary deployment).

Promotion moves ``champion`` and remembers the previous champion in a version tag, so a rollback is
one call and needs no detective work.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import mlflow
from mlflow.exceptions import MlflowException
from sklearn.pipeline import Pipeline

from app.core.config import get_settings

CHAMPION = "champion"
CHALLENGER = "challenger"


@dataclass
class ModelVersion:
    version: str
    aliases: list[str]
    tags: dict[str, str]
    run_id: str | None
    created_at: int

    @property
    def threshold(self) -> float:
        return float(self.tags.get("threshold", "0.5"))


def client() -> mlflow.MlflowClient:
    mlflow.set_tracking_uri(get_settings().mlflow_tracking_uri)
    return mlflow.MlflowClient()


def _wrap(mv: Any) -> ModelVersion:
    return ModelVersion(
        str(mv.version), list(mv.aliases or []), dict(mv.tags or {}), mv.run_id, int(mv.creation_timestamp or 0)
    )


def get_alias(alias: str) -> ModelVersion | None:
    try:
        return _wrap(client().get_model_version_by_alias(get_settings().model_name, alias))
    except MlflowException:
        return None


def get_version(version: str) -> ModelVersion:
    return _wrap(client().get_model_version(get_settings().model_name, version))


def list_versions() -> list[ModelVersion]:
    try:
        found = client().search_model_versions(f"name='{get_settings().model_name}'")
    except MlflowException:
        return []
    return sorted((_wrap(v) for v in found), key=lambda v: int(v.version), reverse=True)


def set_challenger(version: str) -> None:
    client().set_registered_model_alias(get_settings().model_name, CHALLENGER, version)


def promote(version: str, *, by: str) -> ModelVersion | None:
    """Make ``version`` the champion. Returns the previous champion (if any)."""
    c = client()
    name = get_settings().model_name
    previous = get_alias(CHAMPION)
    c.set_registered_model_alias(name, CHAMPION, version)
    c.set_model_version_tag(name, version, "promoted_by", by)
    if previous and previous.version != version:
        c.set_model_version_tag(name, version, "previous_champion", previous.version)
    challenger = get_alias(CHALLENGER)
    if challenger and challenger.version == version:
        c.delete_registered_model_alias(name, CHALLENGER)
    load_model.cache_clear()
    return previous


def rollback(*, by: str) -> tuple[str, str]:
    """Return production to the champion before the current one. Returns (from, to)."""
    current = get_alias(CHAMPION)
    if current is None or "previous_champion" not in current.tags:
        raise ValueError("there is no previous champion to roll back to")
    target = current.tags["previous_champion"]
    c = client()
    name = get_settings().model_name
    c.set_registered_model_alias(name, CHAMPION, target)
    c.set_model_version_tag(name, current.version, "rolled_back_by", by)
    load_model.cache_clear()
    return current.version, target


@lru_cache(maxsize=8)
def load_model(version: str) -> Pipeline:
    client()  # sets the tracking URI
    model: Pipeline = mlflow.sklearn.load_model(f"models:/{get_settings().model_name}/{version}")
    return model


def load_reference(run_id: str) -> dict[str, Any]:
    client()
    return dict(mlflow.artifacts.load_dict(f"runs:/{run_id}/monitoring/reference.json"))
