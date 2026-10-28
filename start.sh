#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PORT="${PORT:-8000}"
export DEMO_MODE="${DEMO_MODE:-1}"
echo "ZeroStep 零步银行 -> http://127.0.0.1:${PORT}/"
exec python3 backend/server.py
