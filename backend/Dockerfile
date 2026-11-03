# One image, four roles:
#   API      uvicorn app.main:app                 (default CMD)
#   worker   python -m app.cli worker             (pipeline runs from the PostgreSQL job queue)
#   serving  serve                                (model server; SageMaker's container contract, port 8080)
#   jobs     python -m app.cli lifecycle|drain    (one-off / scheduled ECS tasks)
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MLFLOW_DISABLE_AGENT_HINT=1 MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR=false
# xgboost needs the OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# Optional build secret `extra_ca` for networks with a TLS-inspecting proxy (not stored in the image):
#   docker build --secret id=extra_ca,src=ca.crt ...
RUN --mount=type=secret,id=extra_ca,required=false,mode=0444 \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install -r requirements.txt
COPY alembic.ini pyproject.toml docker-entrypoint.sh ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY app ./app
RUN chmod 0755 docker-entrypoint.sh && useradd --uid 10001 --create-home mlp
USER mlp
EXPOSE 8000 8080
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "1"]
