"""Deploying the champion.

* ``local``: tells the model server to reload; it resolves the ``champion`` alias in MLflow.
* ``sagemaker``: creates a SageMaker Model for the version (the same serving image, with the
  version baked into its environment), a new EndpointConfig with data capture to S3, and updates
  the endpoint with a **canary** deployment. 10% of traffic moves first and is watched for 10
  minutes; if the endpoint's error or latency alarms fire, SageMaker rolls back to the previous
  config by itself.

The SageMaker path is unit-tested against botocore's Stubber, so the exact API calls are checked
without an AWS account.
"""

from __future__ import annotations

import os
import time
from typing import Any

import boto3
import httpx

from app.core.config import get_settings

CANARY_PERCENT = 10
BAKE_SECONDS = 600


def deploy(version: str) -> dict[str, Any]:
    s = get_settings()
    if s.deploy_target == "sagemaker":
        return deploy_sagemaker(version)
    try:
        token = os.environ.get("MLP_SERVING_ADMIN_TOKEN")
        r = httpx.post(f"{s.serving_url}/admin/reload", headers={"X-Admin-Token": token} if token else {}, timeout=30)
        r.raise_for_status()
        return {"target": "local", "version": version, "server": r.json()}
    except httpx.HTTPError as exc:  # the server picks up the alias on its next start anyway
        return {"target": "local", "version": version, "warning": f"model server not reachable: {exc}"}


def deploy_sagemaker(version: str, *, client: Any | None = None, image_uri: str | None = None,
                     role_arn: str | None = None, alarm_names: list[str] | None = None,
                     capture_s3_uri: str | None = None, instance_type: str = "ml.m5.large") -> dict[str, Any]:  # fmt: skip
    s = get_settings()
    sm = client or boto3.client("sagemaker", region_name=s.s3_region)
    image_uri = image_uri or os.environ["MLP_SERVING_IMAGE_URI"]
    role_arn = role_arn or os.environ["MLP_SAGEMAKER_ROLE_ARN"]
    capture_s3_uri = capture_s3_uri or f"s3://{s.data_bucket}/capture"
    alarm_names = (
        alarm_names if alarm_names is not None else [f"{s.sagemaker_endpoint}-5xx", f"{s.sagemaker_endpoint}-latency"]
    )
    stamp = time.strftime("%Y%m%d%H%M%S")
    model_name = f"{s.model_name}-v{version}-{stamp}"
    config_name = f"{s.sagemaker_endpoint}-v{version}-{stamp}"

    sm.create_model(
        ModelName=model_name,
        ExecutionRoleArn=role_arn,
        PrimaryContainer={"Image": image_uri, "Environment": {
            "MLP_MODEL_VERSION": version, "MLP_MLFLOW_TRACKING_URI": s.mlflow_tracking_uri, "MLP_MODEL_NAME": s.model_name}},
    )  # fmt: skip
    sm.create_endpoint_config(
        EndpointConfigName=config_name,
        ProductionVariants=[{"VariantName": "primary", "ModelName": model_name, "InstanceType": instance_type,
                             "InitialInstanceCount": 2, "InitialVariantWeight": 1.0}],
        DataCaptureConfig={"EnableCapture": True, "InitialSamplingPercentage": 20, "DestinationS3Uri": capture_s3_uri,
                           "CaptureOptions": [{"CaptureMode": "Input"}, {"CaptureMode": "Output"}]},
    )  # fmt: skip
    sm.update_endpoint(
        EndpointName=s.sagemaker_endpoint,
        EndpointConfigName=config_name,
        DeploymentConfig={
            "BlueGreenUpdatePolicy": {
                "TrafficRoutingConfiguration": {"Type": "CANARY", "WaitIntervalInSeconds": BAKE_SECONDS,
                                                "CanarySize": {"Type": "CAPACITY_PERCENT", "Value": CANARY_PERCENT}},
                "TerminationWaitInSeconds": 300,
            },
            "AutoRollbackConfiguration": {"Alarms": [{"AlarmName": a} for a in alarm_names]},
        },
    )  # fmt: skip
    return {"target": "sagemaker", "version": version, "model": model_name, "endpoint_config": config_name,
            "canary_percent": CANARY_PERCENT, "rollback_alarms": alarm_names}  # fmt: skip
