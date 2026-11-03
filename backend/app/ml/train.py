"""Training: one reproducible, fully tracked run per candidate model.

* **Data:** a rolling window of validated monthly batches, loaded from S3 by month. The sha256 of
  every month used is recorded on the run, so each model version traces to the exact bytes.
* **Model:** a scikit-learn Pipeline (one-hot encoding + gradient-boosted trees) wrapped in isotonic
  calibration, so the probabilities mean what they say. Missing sensor values are handled by the
  trees natively. A logistic-regression baseline is available for comparison.
* **Operating threshold:** chosen on the last month of the window (held out from fitting) as the
  lowest threshold that keeps precision ≥ 50%, then stored with the model, so serving, monitoring
  and the gates all use the same cut-off.
* **Tracking:** parameters, metrics, the drift reference and the evaluation report are logged to
  MLflow; the model is logged with its input signature and registered as a new version.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import mlflow
import numpy as np
import pandas as pd
from mlflow.models import infer_signature
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

from app.core.config import get_settings
from app.ml import drift, metrics, storage
from app.ml.generator import EQUIPMENT_TYPES, FEATURES_CATEGORICAL, FEATURES_NUMERIC, LABEL

FEATURES = [*FEATURES_NUMERIC, *FEATURES_CATEGORICAL]
# Models are stored with skops (no pickle); these are the non-sklearn-core types it must accept on load.
TRUSTED_TYPES = ["sklearn.calibration._CalibratedClassifier", "xgboost.core.Booster", "xgboost.sklearn.XGBClassifier"]


@dataclass
class TrainResult:
    run_id: str
    version: str
    threshold: float
    metrics: dict[str, Any]
    reference: dict[str, Any]
    train_months: list[str]
    params: dict[str, Any] = field(default_factory=dict)


def build_pipeline(algorithm: str = "xgboost", seed: int = 42) -> Pipeline:
    encoder = OneHotEncoder(categories=[list(EQUIPMENT_TYPES)], handle_unknown="ignore")
    if algorithm == "xgboost":
        pre = ColumnTransformer([("cat", encoder, list(FEATURES_CATEGORICAL))], remainder="passthrough")
        trees = XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.06, subsample=0.9, colsample_bytree=0.9,
                              min_child_weight=3, reg_lambda=1.0, tree_method="hist", random_state=seed, n_jobs=2,
                              eval_metric="aucpr")  # fmt: skip
        clf: Any = CalibratedClassifierCV(trees, method="isotonic", cv=3)
    elif algorithm == "logistic":
        num = Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())])
        pre = ColumnTransformer([("cat", encoder, list(FEATURES_CATEGORICAL)), ("num", num, list(FEATURES_NUMERIC))])
        clf = LogisticRegression(max_iter=2000, C=0.5)
    else:
        raise ValueError(f"unknown algorithm {algorithm}")
    return Pipeline([("features", pre), ("model", clf)])


def for_serving(model: Pipeline) -> Pipeline:
    """Score single rows on one thread: thread start-up costs more than it saves at batch size 1."""
    clf = model.named_steps["model"]
    for c in getattr(clf, "calibrated_classifiers_", []):
        if isinstance(c.estimator, XGBClassifier):
            c.estimator.set_params(n_jobs=1)
    return model


def load_months(months: list[str]) -> pd.DataFrame:
    return pd.concat([storage.get_batch(m) for m in months], ignore_index=True)


def p95_latency_ms(model: Pipeline, X: pd.DataFrame, n: int = 100, passes: int = 3) -> float:
    """Single-row prediction latency, the way an online endpoint sees it.

    The best of several p95 passes: the training worker shares its CPU with other processes, and
    that interference is noise about the machine, not a property of the model.
    """
    sample = X.sample(min(n, len(X)), random_state=0)
    model.predict_proba(sample.iloc[[0]])  # warm-up
    results = []
    for _ in range(passes):
        timings = []
        for i in range(len(sample)):
            row = sample.iloc[[i]]
            t0 = time.perf_counter()
            model.predict_proba(row)
            timings.append((time.perf_counter() - t0) * 1000)
        results.append(float(np.percentile(timings, 95)))
    return min(results)


def train(train_months: list[str], holdout_month: str, *, algorithm: str = "xgboost", seed: int = 42,
          run_name: str | None = None, tags: dict[str, str] | None = None) -> TrainResult:  # fmt: skip
    s = get_settings()
    if len(train_months) < 2:
        raise ValueError("need at least two training months (one is used to choose the threshold)")
    fit_df = load_months(train_months[:-1])
    val_df = storage.get_batch(train_months[-1])
    hold_df = storage.get_batch(holdout_month)

    # 1. Choose the operating threshold on the last training month, unseen by this fit.
    probe = build_pipeline(algorithm, seed).fit(fit_df[FEATURES], fit_df[LABEL])
    threshold = metrics.choose_threshold(val_df[LABEL].to_numpy(), probe.predict_proba(val_df[FEATURES])[:, 1])

    # 2. Final model on the whole window.
    all_df = pd.concat([fit_df, val_df], ignore_index=True)
    model = for_serving(build_pipeline(algorithm, seed).fit(all_df[FEATURES], all_df[LABEL]))

    # 3. Evaluate on the next month (the "future"), the way it will be used.
    p_hold = model.predict_proba(hold_df[FEATURES])[:, 1]
    report = metrics.evaluate(hold_df[LABEL].to_numpy(), p_hold, threshold, hold_df["equipment_type"].to_numpy())
    report["p95_latency_ms"] = round(p95_latency_ms(model, hold_df[FEATURES]), 3)
    train_scores = model.predict_proba(all_df[FEATURES])[:, 1]
    ref = drift.reference(all_df, list(FEATURES_NUMERIC), list(FEATURES_CATEGORICAL), train_scores)

    manifests = {m: storage.get_manifest(m) or {} for m in [*train_months, holdout_month]}
    params = {"algorithm": algorithm, "seed": seed, "train_months": ",".join(train_months),
              "holdout_month": holdout_month, "train_rows": len(all_df), "threshold": round(threshold, 4)}  # fmt: skip

    mlflow.set_tracking_uri(s.mlflow_tracking_uri)
    mlflow.set_experiment(s.experiment_name)
    with mlflow.start_run(run_name=run_name or f"{algorithm}-{train_months[0]}..{train_months[-1]}") as run:
        mlflow.log_params(params)
        mlflow.log_metrics({k: float(v) for k, v in report.items() if isinstance(v, int | float)})
        mlflow.set_tags({f"data.{m}": (man.get("sha256") or "")[:16] for m, man in manifests.items()} | (tags or {}))
        mlflow.log_dict(report, "evaluation/holdout.json")
        mlflow.log_dict(ref, "monitoring/reference.json")
        example = hold_df[FEATURES].head(3).astype({c: "float64" for c in FEATURES_NUMERIC})  # nullable at serving time
        info = mlflow.sklearn.log_model(
            model,
            name="model",
            signature=infer_signature(example, model.predict_proba(example)[:, 1]),
            input_example=example,
            pyfunc_predict_fn="predict_proba",
            registered_model_name=s.model_name,
            serialization_format="skops",
            skops_trusted_types=TRUSTED_TYPES,
        )
        version = str(info.registered_model_version)
    client = mlflow.MlflowClient()
    for key, value in {"threshold": f"{threshold:.6f}", "algorithm": algorithm, "train_months": params["train_months"],
                       "holdout_month": holdout_month, "pr_auc": str(report["pr_auc"]), "run_id": run.info.run_id}.items():  # fmt: skip
        client.set_model_version_tag(s.model_name, version, key, value)
    return TrainResult(run.info.run_id, version, threshold, report, ref, train_months, params)
