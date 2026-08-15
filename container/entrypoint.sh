#!/usr/bin/env bash
# Run FastAPI + Vite together. Backend on 127.0.0.1:<PORT+10000> (internal),
# frontend on the public port set by aw.json (AW_APP_PORT env).
set -euo pipefail

PORT="${AW_APP_PORT:-10021}"
RELOAD_FLAG=""
if [ "${AUTORELOAD:-0}" = "1" ]; then
    RELOAD_FLAG="--reload"
fi

# All custom apps share aw-sandbox's network namespace — derive a unique
# internal backend port from AW_APP_PORT (see memory:
# aw-app-builder-shared-netns-backend-port). Project mock backends get
# their own separate pool (30021-30120, see src/api/supervisor.py).
BACKEND_PORT=$((PORT + 10000))

export UX_PROTO_DATA_ROOT="/data/projects"

cd /app/src/api
python -m uvicorn main:app --host 127.0.0.1 --port "${BACKEND_PORT}" ${RELOAD_FLAG} &
BACKEND_PID=$!

cd /app/src/app

exec npx vite --host 0.0.0.0 --port "${PORT}" --strictPort
