#!/usr/bin/env bash
# Starts services and the API for manual testing.
# Run from repo root: ./scripts/dev_up.sh   (Ctrl+C stops everything it started)
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/Scripts/python.exe
LOG_DIR=/tmp/real_project_dev
mkdir -p "$LOG_DIR"

echo "Starting Postgres and qdrant..."
# up -d, not start: creates the containers from docker-compose.yml if they don't
# exist yet, starts them if they do. Either way works.
docker compose up -d
echo "Waiting for Postgres to accept connections..."
until docker exec postgres_db pg_isready -U postgres -d app_db >/dev/null 2>&1; do
    sleep 1
done

echo "Applying migrations..."
.venv/Scripts/alembic.exe upgrade head

pids=()
cleanup() {
    echo
    echo "Stopping..."
    for pid in "${pids[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

echo "Starting API on :8000 (log: $LOG_DIR/api.log)"
"$PY" -m uvicorn app.main:app --port 8000 --no-access-log >"$LOG_DIR/api.log" 2>&1 &
pids+=("$!")

echo "Waiting for API readiness..."
for _ in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
        echo "Ready. Logs in $LOG_DIR. Ctrl+C to stop everything."
        wait
        exit 0
    fi
    sleep 1
done

echo "API never became ready; check $LOG_DIR/api.log" >&2
exit 1
