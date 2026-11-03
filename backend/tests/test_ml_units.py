"""Unit tests for the ML building blocks: no database, no MLflow, no S3."""

import numpy as np
import pandas as pd
import pytest
from botocore.stub import ANY, Stubber

from app.core.config import get_settings
from app.ml import contract, drift, gates, metrics
from app.ml.deploy import deploy_sagemaker
from app.ml.generator import FEATURES_CATEGORICAL, FEATURES_NUMERIC, LABEL, BatchSpec, default_timeline, generate_batch


@pytest.fixture(scope="module")
def jan() -> pd.DataFrame:
    return generate_batch(BatchSpec("2026-01"))


# ---------------------------------------------------------------- generator
def test_batches_are_reproducible_and_realistic(jan):
    again = generate_batch(BatchSpec("2026-01"))
    pd.testing.assert_frame_equal(jan, again)
    assert len(jan) > 5000 and 0.02 < jan[LABEL].mean() < 0.15
    assert set(jan["equipment_type"]) == {"pump", "compressor", "motor"}
    assert jan.loc[jan["equipment_type"] == "motor", "pressure_mean"].isna().all()


def test_scenarios_change_the_data_in_the_intended_way(jan):
    drifted = generate_batch(BatchSpec("2026-01", scenario="sensor_drift"))
    pumps = lambda df: df.loc[df["equipment_type"] == "pump", "vibration_mean"].median()  # noqa: E731
    assert pumps(drifted) > 1.25 * pumps(jan)
    mix = generate_batch(BatchSpec("2026-01", scenario="fleet_mix"))
    assert (mix["equipment_type"] == "compressor").mean() > 0.45 > (jan["equipment_type"] == "compressor").mean()
    with pytest.raises(ValueError):
        BatchSpec("2026-01", scenario="alien_invasion")
    assert [b.scenario for b in default_timeline()][6:] == [
        "sensor_drift",
        "sensor_drift",
        "sensor_drift+fleet_mix",
    ] + ["sensor_drift+fleet_mix+new_failure"] * 3
    combo = BatchSpec("2026-01", scenario="sensor_drift+new_failure")
    assert {"sensor_drift", "new_failure"} <= set(combo.effects)


# ---------------------------------------------------------------- data contract
def test_clean_batch_passes_the_contract(jan):
    report = contract.validate(jan)
    assert report.passed, report.errors
    assert 0.02 < report.stats["label_rate"] < 0.15


def test_contract_blocks_broken_batches(jan):
    bad = jan.copy()
    bad.loc[:10, "vibration_mean"] = -1.0  # physically impossible
    bad.loc[:, "load_pct"] = np.where(np.arange(len(bad)) % 5 == 0, np.nan, bad["load_pct"])  # 20% missing
    bad = pd.concat([bad, bad.head(3)])  # duplicate keys
    bad.loc[bad.index[:2], "equipment_type"] = "turbine"
    report = contract.validate(bad)
    assert not report.passed
    text = " | ".join(report.errors)
    for needle in ("outside the physical range", "missing (maximum", "duplicate", "unknown equipment types"):
        assert needle in text
    assert not contract.validate(jan.drop(columns=["current_mean"])).passed
    assert not contract.validate(jan.head(100)).passed


def test_unlabelled_batches_can_be_validated_for_scoring(jan):
    assert contract.validate(jan.drop(columns=[LABEL]), labelled=False).passed


# ---------------------------------------------------------------- metrics
def test_metrics_on_known_values():
    y = np.array([0] * 90 + [1] * 10)
    perfect = y.astype(float)
    assert metrics.evaluate(y, perfect, 0.5)["pr_auc"] == 1.0
    assert metrics.ece(y, np.full(100, 0.1)) == pytest.approx(0.0)  # 10% predicted, 10% observed
    assert metrics.ece(y, np.full(100, 0.6)) == pytest.approx(0.5)
    p = np.linspace(0, 1, 100)
    t = metrics.choose_threshold(y, p, target_precision=0.5)
    assert metrics.at_threshold(y, p, t)["precision"] >= 0.5


def test_slice_metrics_expose_a_weak_fleet():
    y = np.array([1, 1, 0, 0, 1, 1, 0, 0])
    p = np.array([0.9, 0.8, 0.1, 0.2, 0.1, 0.2, 0.1, 0.1])
    groups = np.array(["pump"] * 4 + ["motor"] * 4)
    out = metrics.evaluate(y, p, 0.5, groups)
    assert out["slices"]["pump"]["recall"] == 1.0 and out["slices"]["motor"]["recall"] == 0.0


# ---------------------------------------------------------------- drift
def test_psi_is_zero_for_identical_and_large_for_shifted(jan):
    ref = drift.reference(jan, list(FEATURES_NUMERIC), list(FEATURES_CATEGORICAL))
    same = drift.compare(ref, jan)
    assert same["status"] == "ok" and same["max_feature_psi"]["psi"] < 0.01
    shifted = drift.compare(ref, generate_batch(BatchSpec("2026-01", scenario="sensor_drift")))
    assert shifted["status"] in ("warn", "alert") and "vibration_mean" in shifted["drifted_features"]
    mix = drift.compare(ref, generate_batch(BatchSpec("2026-01", scenario="fleet_mix")))
    assert mix["features"]["equipment_type"]["psi"] > 0.1


def test_next_stable_month_does_not_alert(jan):
    ref = drift.reference(jan, list(FEATURES_NUMERIC), list(FEATURES_CATEGORICAL))
    feb = generate_batch(BatchSpec("2026-02"))
    assert drift.compare(ref, feb)["status"] != "alert"


# ---------------------------------------------------------------- gates
GOOD = {
    "pr_auc": 0.80,
    "ece": 0.02,
    "p95_latency_ms": 8.0,
    "slices": {"pump": {"recall": 0.8}, "motor": {"recall": 0.7}},
}


def test_gates_pass_a_good_first_model():
    report = gates.run_gates(GOOD, None, get_settings())
    assert report.passed and {c.name for c in report.checks} >= {
        "min_quality",
        "calibration",
        "latency",
        "slice_recall",
        "no_regression",
    }


@pytest.mark.parametrize(
    ("change", "failing"),
    [
        ({"pr_auc": 0.40}, "min_quality"),
        ({"ece": 0.20}, "calibration"),
        ({"p95_latency_ms": 500.0}, "latency"),
        ({"slices": {"pump": {"recall": 0.9}, "motor": {"recall": 0.1}}}, "slice_recall"),
    ],
)
def test_each_gate_blocks(change, failing):
    report = gates.run_gates({**GOOD, **change}, None, get_settings())
    assert not report.passed and [c.name for c in report.checks if not c.passed] == [failing]


def test_no_regression_against_the_champion():
    s = get_settings()
    assert gates.run_gates(GOOD, {"pr_auc": 0.81}, s).passed  # within tolerance
    worse = gates.run_gates(GOOD, {"pr_auc": 0.90}, s)
    assert not worse.passed and [c.name for c in worse.checks if not c.passed] == ["no_regression"]
    # a drift-triggered retrain has to beat the champion, not merely match it
    same = gates.run_gates(GOOD, {"pr_auc": 0.80}, s, min_improvement=0.01)
    assert [c.name for c in same.checks if not c.passed] == ["improvement"]
    assert gates.run_gates(GOOD, {"pr_auc": 0.70}, s, min_improvement=0.01).passed
    assert not gates.run_gates(GOOD, None, s, contract_passed=False).passed


# ---------------------------------------------------------------- SageMaker deployment (no AWS: botocore Stubber)
def test_sagemaker_deploy_issues_a_canary_update_with_auto_rollback():
    import boto3

    sm = boto3.client("sagemaker", region_name="us-east-1")
    stub = Stubber(sm)
    stub.add_response("create_model", {"ModelArn": "arn:aws:sagemaker:us-east-1:123456789012:model/m"},
                      {"ModelName": ANY, "ExecutionRoleArn": "arn:aws:iam::123456789012:role/mlp-sagemaker", "PrimaryContainer": ANY})  # fmt: skip
    stub.add_response("create_endpoint_config", {"EndpointConfigArn": "arn:aws:sagemaker:us-east-1:123456789012:endpoint-config/c"},
                      {"EndpointConfigName": ANY, "ProductionVariants": ANY, "DataCaptureConfig": ANY})  # fmt: skip
    expected_update = {
        "EndpointName": "machine-failure",
        "EndpointConfigName": ANY,
        "DeploymentConfig": {
            "BlueGreenUpdatePolicy": {
                "TrafficRoutingConfiguration": {"Type": "CANARY", "WaitIntervalInSeconds": 600,
                                                "CanarySize": {"Type": "CAPACITY_PERCENT", "Value": 10}},
                "TerminationWaitInSeconds": 300,
            },
            "AutoRollbackConfiguration": {"Alarms": [{"AlarmName": "machine-failure-5xx"}, {"AlarmName": "machine-failure-latency"}]},
        },
    }  # fmt: skip
    stub.add_response(
        "update_endpoint",
        {"EndpointArn": "arn:aws:sagemaker:us-east-1:123456789012:endpoint/machine-failure"},
        expected_update,
    )
    with stub:
        out = deploy_sagemaker("7", client=sm, image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/serving:abc",
                               role_arn="arn:aws:iam::123456789012:role/mlp-sagemaker")  # fmt: skip
    stub.assert_no_pending_responses()
    assert out["canary_percent"] == 10 and out["model"].startswith("machine-failure-v7-")


# ---------------------------------------------------------------- monitoring decision
def test_monitoring_alerts_on_drift_on_lost_ranking_and_on_alert_fatigue(jan):
    from app.ml.pipeline import assess
    from app.ml.registry import ModelVersion

    s = get_settings()
    y = jan[LABEL].to_numpy()
    rng = np.random.default_rng(0)
    good = np.clip(y * 0.6 + rng.normal(0.1, 0.1, len(y)), 0, 1)  # separates failures well
    ref = drift.reference(jan, list(FEATURES_NUMERIC), list(FEATURES_CATEGORICAL), good)
    champ = ModelVersion("1", ["champion"], {"threshold": "0.4", "pr_auc": "0.9"}, "run", 0)

    calm = assess(jan, good, champ, ref, s)
    assert calm.status == "ok" and not calm.lost_ranking and not calm.alert_fatigue

    noisy = assess(jan, rng.uniform(0, 1, len(y)), champ, ref, s)  # same inputs, useless scores
    assert noisy.status == "alert" and noisy.lost_ranking and noisy.alert_fatigue

    drifted_df = generate_batch(BatchSpec("2026-01", scenario="sensor_drift"))
    yd = drifted_df[LABEL].to_numpy()
    drifted_scores = np.clip(yd * 0.6 + rng.normal(0.1, 0.1, len(yd)), 0, 1)  # still ranks well...
    drifted = assess(drifted_df, drifted_scores, champ, ref, s)
    assert drifted.status == "alert" and not drifted.lost_ranking  # ...but the inputs moved


def test_monitoring_publishes_the_metric_the_drift_alarm_watches():
    import boto3

    from app.ml.pipeline import publish_metrics

    cw = boto3.client("cloudwatch", region_name="us-east-1")
    stub = Stubber(cw)
    dims = [{"Name": "Model", "Value": "machine-failure"}]
    stub.add_response("put_metric_data", {}, {"Namespace": "MLOps/Monitoring", "MetricData": [
        {"MetricName": "DriftAlert", "Dimensions": dims, "Value": 1.0, "Unit": "Count"},
        {"MetricName": "PrAuc", "Dimensions": dims, "Value": 0.39, "Unit": "None"},
        {"MetricName": "AlertPrecision", "Dimensions": dims, "Value": 0.43, "Unit": "None"},
    ]})  # fmt: skip
    with stub:
        publish_metrics("2026-10", "alert", {"pr_auc": 0.39, "precision": 0.43}, get_settings(), client=cw)
    stub.assert_no_pending_responses()
