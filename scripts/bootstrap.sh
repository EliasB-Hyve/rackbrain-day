#!/usr/bin/env bash
#
# Bootstrap a local RackBrain environment (per-user, portable).
#
# - Creates .venv/
# - Installs requirements.txt
# - Ensures a local config exists (copies config.example on first run)
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${RACKBRAIN_HOME:-}" ]]; then
  # Prefer the caller's chosen location, but still support running from a checkout.
  ROOT_DIR="$(cd "${RACKBRAIN_HOME}" && pwd)"
fi

cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: python3 not found (set PYTHON_BIN to a python executable)" >&2
  exit 1
fi

echo "=== RackBrain bootstrap ==="
echo "Root: $ROOT_DIR"
echo "Python: $($PYTHON_BIN --version 2>&1)"

VENV_DIR="${VENV_DIR:-.venv}"
if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating venv: $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [[ ! -f "config/config.yaml" ]]; then
  echo "Creating local config: config/config.yaml"
  cp "config/config.example.yaml" "config/config.yaml"
fi

chmod +x bin/*.sh 2>/dev/null || true
chmod +x eve_cmd_runner.sh 2>/dev/null || true
chmod +x scripts/*.sh 2>/dev/null || true

echo ""
echo "Done."
echo "Next:"
echo "  export RACKBRAIN_HOME=\"$ROOT_DIR\""
echo "  export RACKBRAIN_JIRA_PAT=\"...\""
echo "  $ROOT_DIR/scripts/rackbrain doctor"
