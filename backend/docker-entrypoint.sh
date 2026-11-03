#!/bin/sh
# SageMaker starts inference containers as `docker run <image> serve`; everything else is passed through.
set -e
if [ "$1" = "serve" ]; then
  exec uvicorn app.serving:app --host 0.0.0.0 --port 8080 --workers "${MLP_SERVING_WORKERS:-2}" --timeout-keep-alive 75 --timeout-worker-healthcheck 60
fi
exec "$@"
