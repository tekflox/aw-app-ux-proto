#!/usr/bin/env bash
# Run bundled Postgres + FastAPI + Vite together. Backend on
# 127.0.0.1:<PORT+10000> (internal), frontend on the public port set by
# aw.json (AW_APP_PORT env), Postgres on 127.0.0.1:5432 (the app's default
# DB URL — see src/api/db.py).
set -euo pipefail

# --- Bundled Postgres bootstrap (one-container-per-app model, see aw-app-kb) ---
# The workspace has no companion-container mechanism for marketplace apps, so
# UX-Proto ships its own Postgres. pgdata lives under the $AW_APP_DATA volume
# (mounted at /data via aw-app.json) so the DB survives uninstall/reinstall.
export PGDATA="${PGDATA:-/data/pgdata}"
PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
mkdir -p "$PGDATA" /data/projects
chown -R postgres:postgres "$PGDATA" /data
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    # First boot: initialize the cluster as the postgres system user.
    su postgres -c "$PG_BIN/initdb -D '$PGDATA' --auth-local=trust --auth-host=trust"
fi
# Listen only on loopback (the backend is the only client) and start.
su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -o \"-c listen_addresses='127.0.0.1' -p 5432\" -w -t 60 start"
# Match the app's default admin URL (postgres:postgres@127.0.0.1/postgres);
# --auth-host=trust means the password is not actually checked, but set it so
# any future scram/md5 tightening keeps working.
su postgres -c "psql -v ON_ERROR_STOP=1 -c \"ALTER USER postgres PASSWORD 'postgres';\"" || true
until pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; do sleep 1; done

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
