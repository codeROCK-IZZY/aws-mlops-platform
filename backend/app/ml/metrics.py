"""Model quality metrics used by training, the promotion gates and production monitoring.

Failures are rare (a few percent of machine-days), so accuracy is meaningless: a model that never
predicts a failure is 95% "accurate". The platform uses:

* **PR-AUC**: ranking quality where it matters, among the rare positives (random = the base rate)
* **recall at the operating threshold**: how many failures maintenance actually gets told about
* **precision at the operating threshold**: how many of those alerts are real
* **ECE** (expected calibration error): whether "30% risk" really means 30%, which matters
  because planners act on the probability
* **Brier score**: overall probabilistic accuracy
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

TARGET_PRECISION = 0.5  # operating point: at least one in two alerts is a real failure


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    total = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            total += m.mean() * abs(float(p[m].mean()) - float(y[m].mean()))
    return float(total)


def choose_threshold(y: np.ndarray, p: np.ndarray, target_precision: float = TARGET_PRECISION) -> float:
    """The lowest threshold whose precision reaches the target (maximizes recall at that precision)."""
    order = np.argsort(-p)
    ys, ps = y[order], p[order]
    tp = np.cumsum(ys)
    precision = tp / np.arange(1, len(ys) + 1)
    ok = np.where(precision >= target_precision)[0]
    if len(ok) == 0:
        return 0.5
    return float(ps[ok.max()])


def at_threshold(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, float]:
    pred = p >= threshold
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    return {
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "alerts": int(pred.sum()),
    }


def evaluate(y: np.ndarray, p: np.ndarray, threshold: float, groups: np.ndarray | None = None) -> dict[str, Any]:
    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=float)
    out: dict[str, Any] = {
        "rows": len(y),
        "positives": int(y.sum()),
        "base_rate": round(float(y.mean()), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4) if y.any() else 0.0,
        "roc_auc": round(float(roc_auc_score(y, p)), 4) if 0 < y.sum() < len(y) else 0.0,
        "brier": round(float(brier_score_loss(y, p)), 4),
        "ece": round(ece(y, p), 4),
        "threshold": round(threshold, 4),
        **{k: round(v, 4) for k, v in at_threshold(y, p, threshold).items()},
    }
    if groups is not None:
        out["slices"] = {
            str(g): {
                "rows": int((groups == g).sum()),
                **{k: round(v, 4) for k, v in at_threshold(y[groups == g], p[groups == g], threshold).items()},
            }
            for g in sorted(set(groups))
        }
    return out
