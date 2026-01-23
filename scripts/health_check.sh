#!/usr/bin/env bash
# RackBrain Health Check Script
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${RACKBRAIN_HOME:-}" ]]; then
  ROOT_DIR="$(cd "${RACKBRAIN_HOME}" && pwd)"
fi

echo "=== RackBrain health check ==="
echo "Timestamp: $(date)"
echo "Root: $ROOT_DIR"

if [[ ! -f "$ROOT_DIR/config/config.yaml" ]]; then
  echo "FAIL: missing config: $ROOT_DIR/config/config.yaml" >&2
  echo "Tip: cp $ROOT_DIR/config/config.example.yaml $ROOT_DIR/config/config.yaml" >&2
  exit 1
fi

if [[ -z "${RACKBRAIN_JIRA_PAT:-}" ]]; then
  echo "WARN: RACKBRAIN_JIRA_PAT is not set (Jira calls will fail)"
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  "$ROOT_DIR/.venv/bin/python" -m rackbrain --config "${RACKBRAIN_CONFIG:-$ROOT_DIR/config/config.yaml}" doctor --check-db
else
  if command -v python3 >/dev/null 2>&1; then
    python3 -m rackbrain --config "${RACKBRAIN_CONFIG:-$ROOT_DIR/config/config.yaml}" doctor --check-db
  else
    echo "FAIL: python3 not found and no $ROOT_DIR/.venv present" >&2
    exit 1
  fi
fi

echo "=== OK ==="

