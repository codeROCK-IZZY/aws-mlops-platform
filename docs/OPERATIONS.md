# Operations

## Configuration

Every setting is an `MLP_*` environment variable ([config.py](../backend/app/core/config.py)).

| Setting | Default | Meaning |
|---|---|---|
| `MLP_APPROVAL_POLICY` | `manual` | `manual`: an approver promotes each candidate. `auto`: candidates that pass every gate deploy themselves |
| `MLP_DEPLOY_TARGET` | `local` | `local` reloads the model server; `sagemaker` runs a canary update of the endpoint |
| `MLP_GATE_MIN_PR_AUC` | 0.55 | absolute quality floor |
| `MLP_GATE_MAX_REGRESSION` | 0.02 | allowed PR-AUC loss vs. the champion on the same month |
| `MLP_GATE_MIN_IMPROVEMENT_ON_DRIFT` | 0.01 | a drift-triggered retrain must beat the champion by this much |
| `MLP_GATE_MAX_ECE` | 0.08 | calibration limit |
| `MLP_GATE_MIN_SLICE_RECALL` | 0.5 | recall floor for every equipment type |
| `MLP_GATE_MAX_P95_LATENCY_MS` | 100 | single-row serving budget |
| `MLP_DRIFT_PSI_WARN` / `_ALERT` | 0.1 / 0.25 | PSI levels |
| `MLP_MONITOR_MAX_PR_AUC_DROP` | 0.08 | production PR-AUC drop that counts as degradation |
| `MLP_MONITOR_MIN_PRECISION` | 0.35 | alert precision below this is alert fatigue |
| `MLP_RETRAIN_ON_DRIFT` | true | start a retraining run on every monitoring alert |
| `MLP_SERVING_ADMIN_TOKEN` | — | protects the model server's `/admin/reload` |
| `MLP_SERVING_REFRESH_SECONDS` | 30 | how often each model-server process re-checks the champion alias |

Before changing a gate or monitoring threshold, run the lifecycle evaluation with the new value. It
measures false alarms on 12 stable months and detection delay for each scenario.

## Health and signals

| Signal | Where |
|---|---|
| API liveness / readiness | `/api/health/live`, `/api/health/ready` (database) |
| Model server ready | `/ping` (200 once a model is loaded; SageMaker uses this) |
| Serving counters, latency histogram | model server `/metrics` (Prometheus) |
| Monitoring outcome | dashboard → Monitoring; on AWS also CloudWatch `MLOps/Monitoring` `DriftAlert`, `PrAuc`, `AlertPrecision` |
| Endpoint errors / latency | CloudWatch alarms `<endpoint>-5xx`, `<endpoint>-latency` (these also drive automatic rollback) |
| Job queue | dashboard → Administration (attempts, last error) |
| Audit chain | dashboard → Administration (verified on every load) |

## Runbooks

### Monitoring raised an alert

1. Open the monitoring report. Find which rule fired:
   - **feature PSI:** which feature?
   - **prediction PSI**
   - **lost ranking:** PR-AUC below baseline
   - **alert fatigue:** precision below 35%
2. **Sensor or pipeline problem, not the world.** Examples: one sensor type jumped overnight, or a unit
   change. Fix the data upstream. Reject the retrained candidate if it was trained on the broken
   readings; otherwise it learns the fault.
3. **Real change.** The retraining run was already started. Open it from the report. If it passes the
   gates it waits in Models & approvals; compare the two models on the same month and approve.
4. **The retrain was blocked.** This is usually right: the new behaviour isn't learnable yet from one
   month (see the October retrain in the [lifecycle report](../eval/reports/lifecycle.md)). Keep the
   champion. The next month's monitoring run will try again with more data. If production is
   unacceptable meanwhile, roll back or lower alerting expectations with the maintenance team. Don't
   relax the gates under pressure.

### A deployment looks bad

- **On SageMaker during the 10-minute canary bake:** if the 5xx or latency alarm fires, SageMaker rolls
  back by itself. Nothing to do except find the cause.
- **After the bake, or for quality problems:** use Models & approvals → **Roll back champion**
  (approvers). This moves the `champion` alias to the recorded previous version, deploys it, and records
  an audit event. Then reject or investigate the bad version.

### A pipeline run failed (not blocked)

The run's steps show the step that failed and the error. The job is retried by the queue: attempts and
the last error are in Administration → Job queue. Stale leases are recovered automatically. Common causes:
- the MLflow server is unreachable (check `MLP_MLFLOW_TRACKING_URI` and the task role)
- S3 permissions
- the worker ran out of memory: training holds about 30,000 rows × 11 features, well within the 8 GB task

## Data and backups

- **PostgreSQL:** platform metadata, runs, promotions, reports, users and the audit chain. On AWS: RDS
  automated backups (7 days) and deletion protection.
- **S3:** datasets, manifests and captured predictions. Versioned (a dataset can't be silently
  overwritten). Captured predictions expire after 90 days; validated batches move to Infrequent Access
  after 90 days.
- **MLflow:** run metadata and the registry live in the tracking server; model artifacts are in S3.
  Deleting a registered model in MLflow removes the platform's lineage for it. Archive instead.

## Retention of captured predictions

The model server captures a 20% sample of requests (features, score, alert, latency) to PostgreSQL
locally. On AWS, SageMaker data capture writes to S3 under `capture/`, which expires after 90 days. The
features are equipment telemetry, not personal data.
