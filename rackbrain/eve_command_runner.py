import os
import subprocess

# Path to eve_cmd_runner.sh if we ever run directly on a TE box (like RAMSES)
HERE = os.path.dirname(os.path.abspath(__file__))
EVE_CMD_RUNNER = os.path.join(HERE, "eve_cmd_runner.sh")

# Special status for "no IP found" from bash script
NO_IP_CODE = 10

# Try to import the remote helper (SR1 → RAMSES).
# If it isn't available, we'll fall back to local eve_cmd_runner.sh.
try:
    from rackbrain.eve_remote import run_eve_remote, find_remote_wrapper_path  # type: ignore
except ImportError:  # running in an older layout / on RAMSES directly
    run_eve_remote = None  # type: ignore
    find_remote_wrapper_path = None  # type: ignore


class EveCommandResult(object):
    def __init__(self, serial, context, cmd, status, stdout, stderr, executed=True):
        self.serial = serial
        self.context = context  # "ilom", "hostnic", "rot", "diag", "local"
        self.cmd = cmd          # the inner command after {context}
        self.status = status    # integer exit code
        self.stdout = stdout    # full stdout text
        self.stderr = stderr    # stderr text (runner summary, errors)
        self.executed = bool(executed)

    @property
    def ok(self):
        """True when command ran and exited 0."""
        return self.status == 0

    @property
    def no_ip(self):
        """True when HOSTNIC/ILOM/ROT had no IP (exit 10)."""
        return self.status == NO_IP_CODE


def _parse_context(cmd_with_context):
    """
    Extract context (ilom/diag/...) and the inner command string.
    """
    context = "unknown"
    inner_cmd = cmd_with_context
    if cmd_with_context.startswith("{"):
        try:
            ctx, rest = cmd_with_context.split("}", 1)
            context = ctx.strip("{}")
            inner_cmd = rest.strip()
        except ValueError:
            pass
    return context, inner_cmd


def run_eve_command(serial, cmd_with_context):
    """
    Run a command like "{diag} hwdiag io config" or "{ilom} show SYS"
    using the best available path:

      - On SR1 (or anywhere with eve_cmd_runner_remote.sh + sshpass):
            rackbrain.eve_remote.run_eve_remote()
        which goes: SR1 → RAMSES → eve_cmd_runner.sh → ILOM.

      - On a TE box (like RAMSES) without the remote helper/module:
            local eve_cmd_runner.sh

    Returns an EveCommandResult.
    """
    context, inner_cmd = _parse_context(cmd_with_context)

    stdout = ""
    stderr = ""
    status = 1

    # Prefer remote path when helper is available and the wrapper script exists.
    wrapper_path = find_remote_wrapper_path() if find_remote_wrapper_path is not None else None
    use_remote = run_eve_remote is not None and bool(wrapper_path)

    if use_remote:
        # Use the SR1 → RAMSES path (already tested by you)
        result = run_eve_remote(serial, cmd_with_context)
        # diag_status is the true exit code from eve_cmd_runner.sh
        diag_status = result.get("diag_status")
        executed = diag_status is not None
        status = diag_status if diag_status is not None else result.get("returncode", 1)
        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
    else:
        # Legacy / direct mode: run eve_cmd_runner.sh on this host
        proc = subprocess.run(
            [EVE_CMD_RUNNER, "--sn", serial, "--cmd", cmd_with_context],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,  # Python 3.6-friendly text mode
        )
        status = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
        executed = True

    return EveCommandResult(
        serial=serial,
        context=context,
        cmd=inner_cmd,
        status=status,
        stdout=stdout,
        stderr=stderr,
        executed=executed,
    )
