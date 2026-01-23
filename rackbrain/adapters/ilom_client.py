# rackbrain/adapters/ilom_client.py

from typing import Optional

from rackbrain.eve_command_runner import run_eve_command, EveCommandResult, NO_IP_CODE  # path as appropriate


class IlomError(Exception):
    pass


def get_open_problems_output(sn: str, timeout: int = 60) -> str:
    """
    Use eve_cmd_runner.sh (via eve_command_runner.run_eve_command) to run:
        {ilom} show System/Open_Problems
    on the given SN and return stdout.
    """
    cmd = "{ilom} show System/Open_Problems"
    result: EveCommandResult = run_eve_command(sn, cmd)

    if result.no_ip:
        raise IlomError(
            "ILOM IP not found for SN %s when running %r" % (sn, cmd)
        )

    if not result.ok:
        raise IlomError(
            "ILOM command failed for SN %s (rc=%s)\ncmd: %s\nstderr:\n%s"
            % (sn, result.status, result.cmd, result.stderr)
        )

    return result.stdout
