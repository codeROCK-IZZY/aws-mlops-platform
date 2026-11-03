"""Drift detection: is production data still like the data the model learned from?

For every input feature and for the model's predicted probability, the **Population Stability
Index** compares production against the training reference:

    PSI = Σ (p_prod − p_ref) · ln(p_prod / p_ref)   over the reference's decile bins

Conventional reading: < 0.1 stable, 0.1–0.25 moderate shift, > 0.25 significant shift. Categorical
features use their categories as bins.

The reference (bin edges and proportions) is computed once at training time and stored with the
model, so monitoring never needs the training data itself.

Drift doesn't prove the model got worse; labels arrive late (a failure is only known up to a week
after the prediction). So the platform tracks both: drift as the early warning, and measured
performance once labels mature.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

EPS = 1e-4


def reference(
    df: pd.DataFrame, numeric: list[str], categorical: list[str], scores: np.ndarray | None = None
) -> dict[str, Any]:
    ref: dict[str, Any] = {"numeric": {}, "categorical": {}}
    for col in numeric:
        values = df[col].dropna().to_numpy(dtype=float)
        edges = np.unique(np.quantile(values, np.linspace(0, 1, 11))) if len(values) else np.array([0.0, 1.0])
        ref["numeric"][col] = {"edges": edges.tolist(), "props": _props_numeric(values, edges).tolist(),
                               "null_rate": float(df[col].isna().mean())}  # fmt: skip
    for col in categorical:
        props = df[col].astype(str).value_counts(normalize=True)
        ref["categorical"][col] = props.to_dict()
    if scores is not None:
        edges = np.unique(np.quantile(scores, np.linspace(0, 1, 11)))
        ref["score"] = {"edges": edges.tolist(), "props": _props_numeric(scores, edges).tolist()}
    return ref


def _props_numeric(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    if len(edges) < 2:
        return np.array([1.0])
    inner = edges[1:-1]
    idx = np.searchsorted(inner, values, side="right")
    counts = np.bincount(idx, minlength=len(edges) - 1).astype(float)
    return counts / max(counts.sum(), 1.0)


def psi(ref_props: np.ndarray, cur_props: np.ndarray) -> float:
    r = np.clip(np.asarray(ref_props, dtype=float), EPS, None)
    c = np.clip(np.asarray(cur_props, dtype=float), EPS, None)
    return float(np.sum((c - r) * np.log(c / r)))


def level(value: float, warn: float, alert: float) -> str:
    return "alert" if value >= alert else "warn" if value >= warn else "ok"


def compare(ref: dict[str, Any], df: pd.DataFrame, scores: np.ndarray | None = None, *,
            warn: float = 0.1, alert: float = 0.25) -> dict[str, Any]:  # fmt: skip
    """Drift of ``df`` (and its ``scores``) against a stored reference."""
    features: dict[str, dict[str, Any]] = {}
    for col, r in ref["numeric"].items():
        values = df[col].dropna().to_numpy(dtype=float)
        cur = _props_numeric(values, np.asarray(r["edges"]))
        v = psi(np.asarray(r["props"]), cur)
        features[col] = {"psi": round(v, 4), "level": level(v, warn, alert),
                         "null_rate": round(float(df[col].isna().mean()), 4)}  # fmt: skip
    for col, r in ref["categorical"].items():
        cats = sorted(set(r) | set(df[col].astype(str).unique()))
        cur_counts = df[col].astype(str).value_counts(normalize=True)
        v = psi(np.array([r.get(c, 0.0) for c in cats]), np.array([cur_counts.get(c, 0.0) for c in cats]))
        features[col] = {"psi": round(v, 4), "level": level(v, warn, alert)}
    out: dict[str, Any] = {"features": features}
    if scores is not None and "score" in ref:
        cur = _props_numeric(np.asarray(scores, dtype=float), np.asarray(ref["score"]["edges"]))
        v = psi(np.asarray(ref["score"]["props"]), cur)
        out["prediction"] = {"psi": round(v, 4), "level": level(v, warn, alert)}
    worst = max(features.items(), key=lambda kv: kv[1]["psi"]) if features else ("", {"psi": 0.0})
    out["max_feature_psi"] = {"feature": worst[0], "psi": worst[1]["psi"]}
    levels = [f["level"] for f in features.values()] + ([out["prediction"]["level"]] if "prediction" in out else [])
    out["status"] = "alert" if "alert" in levels else "warn" if "warn" in levels else "ok"
    out["drifted_features"] = sorted(k for k, f in features.items() if f["level"] != "ok")
    return out
