#!/bin/bash
#
# eve_cmd_runner.sh
# ------------------
# Run a command against an EVE server in a given "context":
#   {ilom}       -> ssh root@ILOM_IP
#   {hostnic}    -> ssh root@HOSTNIC_IP
#   {sunservice} -> ssh sunservice@ILOM_IP
#   {diag}       -> run inside ILOM diag shell (/SP/diag/shell)
#   {faultmgmt}  -> run inside ILOM faultmgmt shell (/SP/faultmgmt/shell)
#   {local}      -> run locally on this TE box
#
# Usage examples:
#   eve_cmd_runner.sh --sn ATL17N0... --cmd "{ilom} show /System/Open_problems"
#   eve_cmd_runner.sh --sn ATL17N0... --cmd "{diag} hwdiag io config"
#   eve_cmd_runner.sh --sn ATL17N0... --cmd "{faultmgmt} fmdump -e -V"
#   eve_cmd_runner.sh --sn ATL17N0... --cmd "{hostnic} ipmitool sdr"
#   eve_cmd_runner.sh --sn ATL17N0... --cmd "{local} ./some_helper.sh arg1"
#
# Exit code = underlying command/diag exit code (or special non-zero if no IP).
# Stdout    = remote/local command output.
# Stderr    = minimal debug summary & error messages.
#

set -euo pipefail

# Always emit a status line for the Python caller to parse, even if we exit early
# due to `set -e` or validation failures.
SERIAL="${SERIAL:-}"
CONTEXT="${CONTEXT:-unknown}"
_emit_runner_status() {
    local exit_code=$?
    echo "[eve_cmd_runner] serial=${SERIAL:-} context=${CONTEXT:-unknown} status=${exit_code}" >&2
}
trap _emit_runner_status EXIT

# --- config (tweak passwords as needed) ---

PASS_ILOM="${PASS_ILOM:-changeme}"
PASS_HOSTNIC="${PASS_HOSTNIC:-123456}"
PASS_SUNSERVICE="${PASS_SUNSERVICE:-changeme}"


EXPECT_TIMEOUT="${EXPECT_TIMEOUT:-60}"

# Where eve_ip actually lives on RAMSES
EVE_IP_CMD="${EVE_IP_CMD:-python3 /home/tester/WesleyH/eve_ip.pyc}"


# custom exit codes for "infra" errors
NO_IP_CODE=10

usage() {
    cat <<USAGE
Usage: $0 --sn SERIAL --cmd "{context} actual command"

  --sn SERIAL      EVE server serial (e.g. ATL17N0...)
  --cmd STRING     Command with context, e.g. "{diag} hwdiag io config"

Contexts:
  {ilom}       run on ILOM over SSH
  {hostnic}    run on HOSTNIC over SSH
  {sunservice} run on ILOM over SSH as sunservice
  {diag}       run inside ILOM diag shell (/SP/diag/shell)
  {faultmgmt}  run inside ILOM faultmgmt shell (/SP/faultmgmt/shell)
  {local}      run locally on this TE box

Examples:
  $0 --sn ATL17N0... --cmd "{ilom} show SYS"
  $0 --sn ATL17N0... --cmd "{diag} hwdiag io config"
  $0 --sn ATL17N0... --cmd "{faultmgmt} fmdump -e -V"
  $0 --sn ATL17N0... --cmd "{hostnic} ipmitool sdr"
  $0 --sn ATL17N0... --cmd "{local} ./my_local_script.sh arg1"
USAGE
}

SERIAL=""
RAW_CMD=""

# --- parse CLI args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sn)
            SERIAL="$2"; shift 2;;
        --cmd)
            RAW_CMD="$2"; shift 2;;
        -h|--help)
            usage; exit 0;;
        *)
            echo "Unknown argument: $1" >&2
            usage; exit 1;;
    esac
done

if [[ -z "$SERIAL" || -z "$RAW_CMD" ]]; then
    echo "Missing required --sn or --cmd" >&2
    usage
    exit 1
fi

# --- parse RAW_CMD into CONTEXT + CMD_STRING ---
# Expected format: {context} rest of command
if [[ "$RAW_CMD" =~ ^\{([a-zA-Z0-9_]+)\}[[:space:]]+(.+)$ ]]; then
    CONTEXT="${BASH_REMATCH[1]}"
    CMD_STRING="${BASH_REMATCH[2]}"
else
    echo "Invalid --cmd format. Expected: {context} actual command" >&2
    echo "Got: $RAW_CMD" >&2
    exit 1
fi

STATUS_FILE="/tmp/${SERIAL}_eve_ip_status.log"

# --- one-shot IP status updater (replaces Spencer's script) ---

update_ip_status() {
    local serial="$1"
    local status_file="/tmp/${serial}_eve_ip_status.log"
    local tmp_file

    tmp_file="$(mktemp)"

    # Call eve_ip using the hard-coded command path
    # stderr goes to a per-SN log so we can debug failures
    if ! $EVE_IP_CMD "$serial" > "$tmp_file" 2>"/tmp/eve_ip_err_${serial}.log"; then
        echo "[eve_cmd_runner] eve_ip failed for $serial (see /tmp/eve_ip_err_${serial}.log)" >&2
        rm -f "$tmp_file"
        return 1
    fi

    # Optional: filter only HOSTNIC / ILOM / ROT lines.
    # If your eve_ip output already is clean, you can skip this grep and just mv.
    grep -E 'HOSTNIC|ILOM|ROT' "$tmp_file" > "$status_file" || true

    # If grep didn't match anything, fall back to saving full output (for debug)
    if [[ ! -s "$status_file" ]]; then
        echo "[eve_cmd_runner] WARNING: no HOSTNIC/ILOM/ROT lines found in eve_ip output for $serial" >&2
        mv "$tmp_file" "$status_file"
    else
        rm -f "$tmp_file"
    fi

    return 0
}


# --- resolve IPs unless context is local ---
if [[ "$CONTEXT" != "local" ]]; then
    # Generate the status file once using eve_ip (no background loop)
    if ! update_ip_status "$SERIAL"; then
        echo "Failed to update IP status for $SERIAL (eve_ip issue)" >&2
        exit 1
    fi

    if [[ ! -s "$STATUS_FILE" ]]; then
        echo "Status file did not appear or is empty: $STATUS_FILE" >&2
        exit 1
    fi

    ipdata="$(cat "$STATUS_FILE")"
    HOSTNIC_IP="$(echo "$ipdata" | awk '/HOSTNIC/ {print $(NF-1)}' | head -n1)"
    ILOM_IP="$(echo  "$ipdata" | awk '/ILOM/    {print $(NF-1)}' | head -n1)"
    ROT_IP="$(echo   "$ipdata" | awk '/ROT/     {print $(NF-1)}' | head -n1)"  # unused but harmless
else
    HOSTNIC_IP=""
    ILOM_IP=""
    ROT_IP=""
fi

run_ssh() {
    local ip="$1"
    local pass="$2"
    local cmd="$3"

    sshpass -p "$pass" ssh \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        root@"$ip" "$cmd"
}

# --- DIAG shell helper (no duplicate output) ---
run_diag_cmd() {
    local target_ip="$1"
    local diag_cmd="$2"
    local shell="/SP/diag/shell"
    local expect_pattern="diag> *"

    local expect_status=0
    EXPECT_PWD="$PASS_ILOM" EXPECT_TIMEOUT="$EXPECT_TIMEOUT" \
    expect <<EOF || expect_status=\$?
        set timeout \$env(EXPECT_TIMEOUT)
        log_user 1

        spawn sshpass -p \$env(EXPECT_PWD) ssh \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            root@$target_ip

        expect -exact "-> "
        send "start -script $shell\r"
        expect -re "$expect_pattern"

        send "$diag_cmd\r"
        expect -re "$expect_pattern"

        send "exit\r"
        expect -exact "-> "
        send "exit\r"
EOF

    return "$expect_status"
}


# --- FAULTMGMT shell helper (fmdump, fmadm, etc.) ---
run_faultmgmt_cmd() {
    local target_ip="$1"
    local fault_cmd="$2"
    local shell="/SP/faultmgmt/shell"
    local expect_pattern="faultmgmtsp> *"

    local expect_status=0
    EXPECT_PWD="$PASS_ILOM" EXPECT_TIMEOUT="$EXPECT_TIMEOUT" \
    expect <<EOF || expect_status=\$?
        set timeout \$env(EXPECT_TIMEOUT)
        log_user 1

        spawn sshpass -p \$env(EXPECT_PWD) ssh \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            root@$target_ip

        expect -exact "-> "
        send "start -script $shell\r"
        expect -re "$expect_pattern"

        send "$fault_cmd\r"
        expect -re "$expect_pattern"

        send "exit\r"
        expect -exact "-> "
        send "exit\r"
EOF

    return "$expect_status"
}

STATUS=0

case "$CONTEXT" in
 	hostnic)
        if [[ -z "${HOSTNIC_IP:-}" ]]; then
            echo "HOSTNIC IP not found for $SERIAL (maybe HOSTNIC is down or not discovered)" >&2
            exit "$NO_IP_CODE"
        fi
        run_ssh "$HOSTNIC_IP" "$PASS_HOSTNIC" "$CMD_STRING" || STATUS=$?
        ;;
    ilom)
        if [[ -z "${ILOM_IP:-}" ]]; then
            echo "ILOM IP not found for $SERIAL (maybe ILOM is down or not discovered)" >&2
            exit "$NO_IP_CODE"
        fi
        run_ssh "$ILOM_IP" "$PASS_ILOM" "$CMD_STRING" || STATUS=$?
        ;;
    sunservice)
        if [[ -z "${ILOM_IP:-}" ]]; then
            echo "ILOM IP not found for sunservice on $SERIAL" >&2
            exit "$NO_IP_CODE"
        fi
        sshpass -p "$PASS_SUNSERVICE" ssh \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            sunservice@"$ILOM_IP" "$CMD_STRING" || STATUS=$?
        ;;
    diag)
        if [[ -z "${ILOM_IP:-}" ]]; then
            echo "ILOM IP not found for diag on $SERIAL" >&2
            exit "$NO_IP_CODE"
        fi
        run_diag_cmd "$ILOM_IP" "$CMD_STRING" || STATUS=$?
        ;;
    faultmgmt)
        if [[ -z "${ILOM_IP:-}" ]]; then
            echo "ILOM IP not found for faultmgmt on $SERIAL" >&2
            exit "$NO_IP_CODE"
        fi
        run_faultmgmt_cmd "$ILOM_IP" "$CMD_STRING" || STATUS=$?
        ;;
    local)
        # run on this TE box (not on the server)
        # use bash -ic so ~/.bashrc is loaded (eve, eve_ip, aliases, etc.)
        bash -ic "$CMD_STRING" || STATUS=$?
        ;;
    *)
        echo "Unknown context: $CONTEXT" >&2
        exit 1
        ;;
esac

exit "$STATUS"
