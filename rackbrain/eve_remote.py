import subprocess
import re
import os
from pathlib import Path
from typing import Dict, Optional, List


class EveRemoteError(Exception):
    """Raised when we can't successfully run eve_cmd_runner_remote.sh."""


def find_remote_wrapper_path() -> Optional[str]:
    """
    Locate eve_cmd_runner_remote.sh.

    Preference order:
      1) $EVE_CMD_RUNNER_REMOTE_PATH
      2) repo-local ./bin/eve_cmd_runner_remote.sh (relative to this package)
      3) CWD bin/eve_cmd_runner_remote.sh
      4) ~/bin/eve_cmd_runner_remote.sh (legacy)

    Returns absolute path if found, else None.
    """
    candidates: List[Optional[str]] = [
        os.environ.get("EVE_CMD_RUNNER_REMOTE_PATH"),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin", "eve_cmd_runner_remote.sh"),
        os.path.join(os.getcwd(), "bin", "eve_cmd_runner_remote.sh"),
        os.path.join(os.path.expanduser("~"), "bin", "eve_cmd_runner_remote.sh"),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.strip()
        if not candidate:
            continue
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    return None


def run_eve_remote(sn: str, cmd: str, timeout: int = 600) -> Dict[str, Optional[str]]:
    """
    Run an EVE command remotely via RAMSES and return a structured result.

    Returns dict with:
      - serial
      - context
      - diag_status (int or None)
      - returncode (ssh wrapper exit code)
      - stdout
      - stderr
    """
    script_path = find_remote_wrapper_path()
    if not script_path:
        raise EveRemoteError(
            "eve_cmd_runner_remote.sh not found. Tried $EVE_CMD_RUNNER_REMOTE_PATH, "
            "repo-local bin/, CWD bin/, and ~/bin/."
        )

    try:
        script_text = Path(script_path).read_text(encoding="utf-8", errors="replace")
        # Normalize line endings so copying from Windows -> Linux doesn't break bash.
        script_text = script_text.replace("\r\n", "\n").replace("\r", "\n")
    except OSError as e:
        raise EveRemoteError(f"Failed to read eve_cmd_runner_remote.sh at {script_path!r}") from e

    repo_root = os.path.dirname(os.path.dirname(__file__))
    runner_path_candidates = [
        os.environ.get("EVE_CMD_RUNNER_PATH"),
        os.path.join(repo_root, "eve_cmd_runner.sh"),
        os.path.join(os.getcwd(), "eve_cmd_runner.sh"),
    ]
    runner_path = None
    for candidate in runner_path_candidates:
        if not candidate:
            continue
        candidate = candidate.strip()
        if not candidate:
            continue
        if os.path.exists(candidate):
            runner_path = os.path.abspath(candidate)
            break

    try:
        proc = subprocess.run(
            ["/bin/bash", "-s", "--", sn, cmd],
            input=script_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,  # Python 3.6 equivalent of text=True
            env={
                **os.environ,
                # Tell the wrapper which eve_cmd_runner.sh to execute on RAMSES.
                # The wrapper will stream this file to RAMSES and run it via `bash -s`,
                # so RAMSES doesn't need a separate checkout of rackbrain.
                "EVE_CMD_RUNNER_PATH": runner_path or "",
            },
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise EveRemoteError(
            f"eve_cmd_runner_remote.sh timed out after {timeout}s "
            f"for SN={sn}, cmd={cmd!r}"
        ) from e

    stdout = proc.stdout
    stderr = proc.stderr

    serial: Optional[str] = None
    context: Optional[str] = None
    diag_status: Optional[int] = None

    # Parse the status line, e.g.:
    # [eve_cmd_runner] serial=2547YW117F context=diag status=0
    combined = "{}\n{}".format(stdout or "", stderr or "")
    for line in combined.splitlines():
        m = re.search(
            r"\[eve_cmd_runner\]\s+serial=(\S+)\s+context=(\S+)\s+status=(\d+)",
            line,
        )
        if m:
            serial = m.group(1)
            context = m.group(2)
            diag_status = int(m.group(3))
            break


    # If the wrapper failed before emitting a runner status line, do not raise:
    # treat it as a failed command so RackBrain can keep processing the ticket
    # (e.g., produce a dry-run comment that includes the stderr).
    #
    # Callers should interpret diag_status=None as "runner did not execute".

    return {
        "serial": serial,
        "context": context,
        "diag_status": diag_status,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
