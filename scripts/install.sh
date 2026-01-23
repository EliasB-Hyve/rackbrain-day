#!/usr/bin/env bash
# Backwards-compatible entrypoint for the bootstrap process.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bootstrap.sh"
