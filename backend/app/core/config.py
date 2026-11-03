"""Configuration from ``MLP_*`` environment variables (or a git-ignored ``.env``).

The same code runs locally (an S3-compatible store, a local MLflow server) and on AWS (S3, the SageMaker
managed MLflow tracking server, a SageMaker endpoint). Only these settings differ.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MLP_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://mlp:mlp@localhost:5432/mlp"

    # --- security ---
    jwt_secret: SecretStr | None = None  # required by the API (checked at start-up); the model server has no users
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cookie_secure: bool = True
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    login_rate_limit_per_minute: int = 10
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # --- storage (S3 or any S3-compatible store, e.g. SeaweedFS locally) ---
    s3_endpoint_url: str | None = None  # None = real AWS S3
    s3_region: str = "us-east-1"
    data_bucket: str = "mlp-data"  # raw / validated datasets, prediction capture
    artifact_bucket: str = "mlp-artifacts"  # MLflow artifacts, reports

    # --- MLflow tracking + model registry ---
    mlflow_tracking_uri: str = "http://localhost:5000"
    model_name: str = "machine-failure"
    experiment_name: str = "machine-failure"

    # --- serving ---
    serving_url: str = "http://localhost:8080"  # model server (SageMaker-compatible)
    deploy_target: Literal["local", "sagemaker"] = "local"
    sagemaker_endpoint: str = "machine-failure"

    # --- promotion gates ---
    gate_min_pr_auc: float = 0.55
    gate_max_regression: float = 0.02  # candidate may not be worse than champion by more than this
    gate_max_ece: float = 0.08  # expected calibration error
    gate_min_slice_recall: float = 0.5  # recall at the operating threshold, per equipment type
    gate_max_p95_latency_ms: float = 100.0  # single-row CPU scoring incl. feature pipeline
    gate_min_improvement_on_drift: float = 0.01  # a drift-triggered retrain must actually beat the champion
    approval_policy: Literal["auto", "manual"] = "manual"

    # --- monitoring ---
    drift_psi_warn: float = 0.1
    drift_psi_alert: float = 0.25
    retrain_on_drift: bool = True
    # Monthly PR-AUC on *stable* data varies by about ±0.03 (≈200 failures a month), so a production drop
    # only counts as degradation beyond ~2.5 standard deviations. (Promotion compares two models on
    # the same month, where that noise cancels, hence its much tighter gate_max_regression.)
    monitor_max_pr_auc_drop: float = 0.08
    monitor_min_precision: float = 0.35  # alert precision below this = maintenance is drowning in false alarms

    # --- logging ---
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("jwt_secret")
    @classmethod
    def _secret_strength(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 32:
            raise ValueError("MLP_JWT_SECRET must be at least 32 characters")
        return v

    def signing_key(self) -> str:
        if self.jwt_secret is None:
            raise RuntimeError("MLP_JWT_SECRET is not set")
        return self.jwt_secret.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
