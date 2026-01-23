#!/bin/bash
# Usage:
#   eve_cmd_runner_remote.sh SN "{diag} hwdiag io config"

if [ -z "$RAMSES_TESTER_PASS" ]; then
    echo "RAMSES_TESTER_PASS is not set" >&2
    exit 1
fi

if [ $# -lt 2 ]; then
    echo "Usage: eve_cmd_runner_remote.sh SN \"{context} actual command\"" >&2
    exit 1
fi

SN="$1"
shift
CMD="$*"

# Path to the eve_cmd_runner.sh we want to execute on RAMSES.
# Prefer env override so SR1 can always run its local copy (no RAMSES checkout drift).
RUNNER_PATH="${EVE_CMD_RUNNER_PATH:-}"
if [ -z "$RUNNER_PATH" ]; then
    RUNNER_PATH="$HOME/rackbrain/eve_cmd_runner.sh"
fi

if [ ! -f "$RUNNER_PATH" ]; then
    echo "eve_cmd_runner.sh not found at '$RUNNER_PATH' (set EVE_CMD_RUNNER_PATH)" >&2
    exit 1
fi

# Safely embed arbitrary strings (including single quotes) into a remote
# single-quoted shell string.
escape_for_single_quotes() {
    # Turns:  foo'bar  ->  foo'\''bar
    printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

SN_ESCAPED="$(escape_for_single_quotes "$SN")"
CMD_ESCAPED="$(escape_for_single_quotes "$CMD")"

# Stream the runner script to RAMSES and execute it there. This avoids needing
# a separate `~/rackbrain` checkout on RAMSES to stay in sync with SR1.
tr -d '\r' < "$RUNNER_PATH" | sshpass -p "$RAMSES_TESTER_PASS" \
  ssh -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      tester@hyve-cmd101-ramses.hyvesolutions.org \
  "bash -s -- --sn '$SN_ESCAPED' --cmd '$CMD_ESCAPED'"
