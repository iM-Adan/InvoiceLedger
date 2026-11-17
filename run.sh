#!/usr/bin/env sh
# Local start: loads .env if present, then serves on http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --reload
