"""Versioned datasets in S3 (or an S3-compatible store locally).

Layout in the data bucket::

    raw/<month>/telemetry.parquet            as delivered
    validated/<month>/telemetry.parquet      passed the data contract
    validated/<month>/manifest.json          sha256, rows, contract report, scenario
    capture/<date>/<request-id>.jsonl        sampled predictions from the serving endpoint
                                             (the same idea as SageMaker data capture)

A training run records the sha256 of every month it used, so any model can be traced back to the
exact bytes it learned from.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
import pandas as pd
from botocore.exceptions import ClientError

from app.core.config import get_settings


@lru_cache
def s3() -> Any:
    s = get_settings()
    return boto3.client("s3", endpoint_url=s.s3_endpoint_url, region_name=s.s3_region)


def ensure_buckets() -> None:
    s = get_settings()
    client = s3()
    for bucket in (s.data_bucket, s.artifact_bucket):
        try:
            client.head_bucket(Bucket=bucket)
        except ClientError:
            client.create_bucket(Bucket=bucket)


@dataclass(frozen=True)
class DatasetRef:
    month: str
    stage: str  # raw | validated
    key: str
    sha256: str
    rows: int


def _put_parquet(key: str, df: pd.DataFrame) -> tuple[str, int]:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    data = buf.getvalue()
    s3().put_object(Bucket=get_settings().data_bucket, Key=key, Body=data)
    return hashlib.sha256(data).hexdigest(), len(df)


def put_batch(month: str, df: pd.DataFrame, *, stage: str, manifest: dict[str, Any] | None = None) -> DatasetRef:
    key = f"{stage}/{month}/telemetry.parquet"
    sha, rows = _put_parquet(key, df)
    if manifest is not None:
        body = json.dumps({**manifest, "month": month, "sha256": sha, "rows": rows}, indent=2, default=str)
        s3().put_object(Bucket=get_settings().data_bucket, Key=f"{stage}/{month}/manifest.json", Body=body.encode())
    return DatasetRef(month, stage, key, sha, rows)


def get_batch(month: str, stage: str = "validated") -> pd.DataFrame:
    obj = s3().get_object(Bucket=get_settings().data_bucket, Key=f"{stage}/{month}/telemetry.parquet")
    return pd.read_parquet(io.BytesIO(obj["Body"].read()))


def get_manifest(month: str, stage: str = "validated") -> dict[str, Any] | None:
    try:
        obj = s3().get_object(Bucket=get_settings().data_bucket, Key=f"{stage}/{month}/manifest.json")
    except ClientError:
        return None
    return dict(json.loads(obj["Body"].read()))


def list_months(stage: str = "validated") -> list[str]:
    resp = s3().list_objects_v2(Bucket=get_settings().data_bucket, Prefix=f"{stage}/", Delimiter="/")
    return sorted(p["Prefix"].split("/")[1] for p in resp.get("CommonPrefixes", []))


def put_json(key: str, payload: dict[str, Any], *, bucket: str | None = None) -> None:
    s3().put_object(Bucket=bucket or get_settings().artifact_bucket, Key=key,
                    Body=json.dumps(payload, indent=2, default=str).encode())  # fmt: skip
