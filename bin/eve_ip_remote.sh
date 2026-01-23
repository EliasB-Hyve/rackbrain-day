#!/bin/bash
set -euo pipefail

SN="${1:-}"

if [ -z "$SN" ]; then
    echo "Usage: eve_ip_remote.sh <SN>" >&2
    exit 1
fi

if [ -z "${RAMSES_TESTER_PASS:-}" ]; then
    echo "RAMSES_TESTER_PASS is not set" >&2
    exit 1
fi

sshpass -p "$RAMSES_TESTER_PASS" \
  ssh -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      tester@hyve-cmd101-ramses.hyvesolutions.org \
  "python3 /home/tester/WesleyH/eve_ip.pyc '$SN'"
