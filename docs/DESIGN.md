# Design

## Goals

1. A model in production is replaced only by a model that is **measurably better on today's data**, and a
   person (or an explicit policy) decides.
2. When the data changes in a way that hurts the model, the platform **notices within a month and acts**,
   and it does not raise alarms when nothing has changed.
3. Every production model can be traced to its **exact training data, code, parameters and approver**,
   and the previous model is **one action away**.
4. The same code runs on a laptop (`docker compose`) and on AWS. Only configuration differs.

## Components

| Component | Local | AWS | Role |
|---|---|---|---|
| Control plane | FastAPI container | ECS Fargate service behind an ALB | REST API, roles, audit; enqueues pipeline runs |
| Pipeline worker | container | ECS Fargate service (2 vCPU / 8 GB) | runs ingest, train, monitor; deploys |
| Scheduler | — (manual / dashboard) | EventBridge Scheduler → ECS task `monitor-latest` | daily monitoring |
| Metadata + queue | PostgreSQL 17 | RDS PostgreSQL (encrypted, Secrets Manager) | runs, promotions, reports, jobs, users, audit chain |
| Object store | SeaweedFS (S3 API) | S3, versioned, SSE-KMS, TLS-only | datasets + manifests, gate reports, captured predictions |
| Tracking + registry | MLflow server (proxied artifacts) | SageMaker managed MLflow tracking server | runs, metrics, models, `champion` / `challenger` aliases |
| Model server | `serve` container, follows the alias | SageMaker real-time endpoint, pinned version | `/ping`, `/invocations`, `/v1/predict`, `/metrics` |
| Dashboard | nginx + React | ECS service | overview, runs, approvals, monitoring, datasets, admin |

One backend image provides the API, worker and model server. SageMaker starts inference containers with
the argument `serve`; [docker-entrypoint.sh](../backend/docker-entrypoint.sh) maps that to the model
server and passes every other command through.

## The pipeline

Every operation is a `PipelineRun` (`ingest`, `train` or `monitor`) with a trigger (`manual`, `schedule`
or `drift`). It records an ordered list of steps with status, timing and details. Runs are executed by the
worker through a PostgreSQL job queue (`FOR UPDATE SKIP LOCKED`), so several workers can share it and a
crashed job is retried. A step that refuses to continue raises `PipelineBlocked`, for example a failed
contract or failed gates. The run then ends as **blocked**, with the reason, rather than failed: the
platform worked, and the candidate did not qualify.

```
ingest   receive → validate (data contract) → publish (S3 + manifest + sha256)
train    train (window → probe → threshold → final fit → holdout eval → MLflow)
         → evaluate_champion (same holdout) → gates → register (challenger alias + pending promotion)
         → [approve → deploy]        when approval_policy=auto, or for the very first model
monitor  score (champion) → drift (PSI) → performance (labels) → [trigger_retrain]
```

### Data contract

[contract.py](../backend/app/ml/contract.py) checks each batch before anything can use it:
- required columns
- physical ranges (no negative vibration, load ≤ 120%)
- null rates ≤ 5% (pressure is excluded on motors, which have no pressure sensor)
- unique `(unit_id, event_date)` keys
- known equipment types
- a label rate between 0.5% and 25%
- at least 500 rows

Medians outside a typical band are *warnings*: worth a look, but not a reason to stop. Rejected batches
are recorded with their errors and never reach `validated/`.

### Training

- **Window.** The four validated months before the holdout month. Older data reflects an older plant;
  a rolling window is what lets the model adapt.
- **Threshold.** A probe model is fit on all but the last training month. The alert threshold is the
  lowest score whose precision on that last month is still ≥ 50%. The final model is then refit on the
  whole window. Choosing the threshold on later data matches how it will be used: on next month's data.
- **Model.** XGBoost in a scikit-learn pipeline (one-hot equipment type, native missing values), wrapped
  in isotonic `CalibratedClassifierCV`. Calibration matters because the probability is shown to people
  and the monitoring rules reason about it. A logistic-regression baseline is available (`algorithm`).
- **What MLflow records.** Parameters, the data hashes of every month used, holdout metrics including
  per-equipment-type slices, single-row p95 latency (best of three passes, to filter out CPU contention),
  the evaluation report, and the **drift reference** (feature histograms and the score distribution on
  the training data). The model is stored with skops (no pickle) and registered. Version tags carry the
  threshold, the window and the holdout PR-AUC.

### Promotion gates

[gates.py](../backend/app/ml/gates.py). The candidate and the champion are scored **on the same holdout
month**, each at its own threshold, so the comparison is fair and month-to-month noise cancels.

| Gate | Rule | Why |
|---|---|---|
| `min_quality` | PR-AUC ≥ 0.55 | absolute floor: never deploy a weak model just because the old one got worse |
| `no_regression` | PR-AUC ≥ champion − 0.02 | small tolerance: same data, so differences are real |
| `improvement` | PR-AUC ≥ champion + 0.01, **drift-triggered runs only** | a retrain that learned nothing should not churn production |
| `calibration` | expected calibration error ≤ 0.08 | probabilities are shown to people |
| `slice_recall` | recall ≥ 0.5 for **every** equipment type | an average can hide a fleet the model stopped protecting |
| `latency` | single-row p95 ≤ 100 ms | the serving budget |
| `contract` | the holdout batch passed the data contract | no promotion on bad data |

The PR-AUC used is average precision. With about 3% positives it is far more informative than ROC-AUC.

### Approval, deployment, rollback

A candidate that passes becomes the `challenger` alias and gets a **pending promotion**, which holds the
full gate report. An approver sees both models side by side and approves or rejects it, with an optional
note. Approval moves the `champion` alias and records the previous champion on the version. It then
deploys:
- **Local:** POST `/admin/reload` (token-protected) on the model server. Each server process also checks
  the alias every 30 seconds, so every worker converges.
- **SageMaker:** create a Model for the version (the version is pinned in the container environment, so a
  canary really serves the new model), then a new EndpointConfig with **data capture** to S3. Then
  `UpdateEndpoint` with a **blue/green canary**: 10% of capacity, a 10-minute bake, and
  **AutoRollbackConfiguration** on the endpoint's 5xx and p95 latency alarms. If either alarm fires during
  the bake, SageMaker returns to the previous config by itself. The API calls are unit-tested with
  botocore's Stubber.

Rollback (approvers) moves `champion` back to the recorded previous version and deploys it. Promotions,
rejections and rollbacks are all audit events in a hash chain: each event stores the hash of the previous
one, and a database trigger rejects UPDATE and DELETE. `/admin/audit` verifies the chain.

### Monitoring

[`pipeline.assess`](../backend/app/ml/pipeline.py) is the single monitoring decision. The API, the
schedule and the lifecycle evaluation all use it.

- **Input drift:** PSI per feature against the champion's training reference. Numeric features use
  decile bins from the reference; categoricals use their levels; null rates are reported alongside. PSI ≥ 0.1
  is a warning and ≥ 0.25 is an alert.
- **Prediction drift:** PSI on the score distribution.
- **Lost ranking:** PR-AUC below the champion's holdout PR-AUC by more than **0.08**.
- **Alert fatigue:** precision at the operating threshold below **35%**.

Any alert, by default, enqueues a `train` run with the monitored month as its holdout and trigger
`drift`. That run must pass every gate, including `improvement`. On AWS each assessment also publishes
`DriftAlert`, `PrAuc` and `AlertPrecision` to CloudWatch (namespace `MLOps/Monitoring`), and the
`model-drift` alarm notifies the on-call engineer.

**Why 0.08 and not 0.02.** The first version reused the promotion tolerance. The lifecycle evaluation then
ran the same decision on 12 extra stable months with new seeds, and **6 of 12 raised false alarms**.
About 200 failures a month gives monthly PR-AUC a spread of roughly ±0.03, so the monitoring tolerance has
to sit outside that (≈ 2.5 σ). With 0.08: 0 of 12. Promotion keeps 0.02 because both models see the same
month there.

**Why performance and not just PSI.** The seal-leak failure mode is concept drift: the relationship between
inputs and failure changes. Feature PSI was already alerting on the earlier sensor and fleet changes, and
the prediction distribution barely moved. What showed something new was PR-AUC falling from 0.77 to 0.39.
This needs labels, which arrive with a lag; see [known limitations](known-limitations.md).

## Data model (PostgreSQL)

| Table | Contents |
|---|---|
| `datasets` | one row per month: status, scenario, rows, sha256, the contract report |
| `pipeline_runs` | kind, trigger, params, ordered steps (JSONB), result, model version, who and when |
| `promotions` | version, from_version, promote / rollback, pending / approved / rejected, gate report, decider, note |
| `monitoring_reports` | month, model version, drift report, performance, status, the retrain it triggered |
| `predictions` | a 20% sample of served predictions (features, score, alert, latency) for live drift |
| `jobs` | the work queue (attempts, last error, stale-lease recovery) |
| `users`, `refresh_tokens` | argon2 password hashes; only hashes of refresh tokens are stored |
| `audit_events` | hash-chained, append-only |

The schema is created by one explicit SQL migration
([0001_initial_schema.py](../backend/migrations/versions/0001_initial_schema.py)), with CHECK constraints on
every status column.

## Security

- **Authentication.** JWT access tokens (15 minutes) are held in memory by the SPA. Refresh tokens
  rotate, are stored as httpOnly cookies, and reuse is detected. Logins are rate-limited, and accounts
  lock after repeated failures.
- **Roles.** Every route checks a role rank: viewer < engineer < approver < admin. Approval and rollback
  need *approver*, so the engineer who trains a model cannot promote it unless also given that role.
- **Model server.** It has no user accounts. On AWS it is reachable only through SageMaker's IAM-signed
  `InvokeEndpoint`. Locally `/admin/reload` needs a shared token, and the published ports bind to
  127.0.0.1.
- **Storage.** S3 buckets block public access, require TLS, are versioned, and are encrypted with a KMS key
  that rotates. The serving image repository is scanned on push and encrypted with the same key. Secrets live in Secrets
  Manager and reach tasks only as injected environment variables.
- **Models are stored with skops, not pickle.** Loading accepts only an explicit list of trusted types
  (`TRUSTED_TYPES` in [train.py](../backend/app/ml/train.py)).

## Decisions and trade-offs

| Decision | Alternatives | Why |
|---|---|---|
| PostgreSQL job queue | SQS, Step Functions, Airflow | one fewer service; transactional with run state; enough for a pipeline that runs a few times a day. Step Functions would be the next step with many models |
| MLflow registry with aliases | SageMaker Model Registry | the same API locally and on AWS (SageMaker managed MLflow); aliases make champion / challenger explicit |
| Gates in code, run inside the training run | a separate approval service | gates are versioned with the code that trains the model, and their report travels with the promotion |
| SageMaker canary with alarm rollback | shadow traffic, A/B by hand | catches the failures that hurt users (errors, latency) automatically; model *quality* is checked before deployment by the gates |
| Rolling four-month window | all history, weighted history | adapts within a quarter; measured: recovery after sensor drift took one retrain |
| Synthetic data with scripted drift | a public dataset | the platform's job is reacting to change, so the changes have to be known to measure detection delay and false alarms |
| SeaweedFS locally | MinIO, moto | MinIO no longer publishes its server images; SeaweedFS is Apache-2.0, persistent, and speaks the S3 API |
