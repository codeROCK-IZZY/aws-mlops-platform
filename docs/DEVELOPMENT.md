# Development

## Layout

```
backend/
  app/
    ml/            generator, contract, storage (S3), train, metrics, drift, gates, registry (MLflow),
                   pipeline (runs, steps, monitoring decision), deploy (local / SageMaker canary)
    modules/       API routers: auth, users, platform (datasets, runs, models, promotions, monitoring), admin
    evaluation/    lifecycle.py: the year replayed through the pipeline, and its gate
    jobs/          PostgreSQL job queue and worker
    serving.py     model server (SageMaker container contract)
    cli.py         migrate, create-user, worker, drain, monitor-latest, demo, lifecycle
  migrations/      one explicit SQL migration
  scripts/         lifecycle_offline.py (moto S3 + SQLite MLflow; used by CI)
  tests/           unit tests, API + worker end-to-end tests, model-server contract
frontend/          React 18 + TypeScript + Tailwind dashboard
infra/terraform/   network, data (RDS + secrets), app (ECS + ALB), ml (S3/KMS, ECR, MLflow, SageMaker, schedule)
eval/reports/      the committed lifecycle report
```

## Backend without Docker

You need Python 3.13 and PostgreSQL. S3 and MLflow are not needed for tests: the tests start an in-process
moto S3 server and use a throwaway SQLite MLflow store.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

Create the test database (any PostgreSQL 15+). The tests default to
`postgresql+psycopg://mlp:mlp@127.0.0.1:5437/mlp_test`; override with `MLP_DATABASE_URL`.

```bash
docker run -d --name mlp-test-db -p 127.0.0.1:5437:5432 -e POSTGRES_USER=mlp -e POSTGRES_PASSWORD=mlp \
  -e POSTGRES_DB=mlp_test postgres:17-alpine
```
```bash
pytest                                  # about 2 minutes: real training runs, real registry
ruff check app tests scripts migrations && ruff format --check app tests scripts migrations && mypy app scripts
```

What the tests cover:
- the generator's scenarios
- every contract rule
- metrics on known values
- PSI behaviour
- each gate in isolation
- the monitoring decision (drift, lost ranking, alert fatigue)
- the SageMaker canary and the CloudWatch metric calls (botocore Stubber)
- auth and roles
- the full model lifecycle through the API and the worker: ingest → train → gates → pending → approve →
  rollback → monitor
- scheduled monitoring
- the model server's `/ping`, `/invocations` (JSON and CSV), validation and metrics

## The lifecycle evaluation

```bash
cd backend
MLP_DATABASE_URL=postgresql+psycopg://mlp:mlp@127.0.0.1:5437/mlp_eval python scripts/lifecycle_offline.py --out ../eval/reports --gate
```

It takes about 5 minutes. It **deletes** the platform data in the database it points at. Use a separate
database, as above. With `--gate` it exits non-zero in any of these cases:
- a scenario goes undetected for more than a month
- a stable month of the year raises an alarm
- more than one of 12 independent stable months raises an alarm
- the managed model is not at least 0.03 PR-AUC better than the static one during drift

Inside the Docker stack the same evaluation runs against SeaweedFS and the MLflow server:
`docker compose run --rm api python -m app.cli lifecycle --reset`.

## Frontend

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api to localhost:8000
npm run lint && npm run typecheck && npm test && npm run build
```

## Conventions

- **Pipeline steps.** Every pipeline step goes through `StepRecorder.step(name)`, which records its
  timing and details. Raise `PipelineBlocked` when the platform correctly refuses to continue; use an
  ordinary exception for a real failure.
- **Monitoring rules** live only in `pipeline.assess`, so the API, the schedule and the evaluation can't
  disagree.
- **Thresholds** are settings (`MLP_GATE_*`, `MLP_DRIFT_*`, `MLP_MONITOR_*`), never literals in code.
