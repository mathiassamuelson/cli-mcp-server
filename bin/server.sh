#!/usr/bin/env bash
# Start the cli-mcp-server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

source .venv/bin/activate

exec uvicorn cli_mcp.server:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8100}" \
    "$@"
