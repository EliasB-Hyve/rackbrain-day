import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests


class CinderVerificationError(RuntimeError):
    pass


@dataclass
class CinderConfig:
    seizo_base: str
    list_path: str = "/execution/list"
    exec_path: str = "/execution"
    http_timeout_seconds: int = 20
    max_report_chars: int = 8000
    mysql_host: str = "10.0.254.101"
    mysql_user: str = "qc_read_only"
    mysql_db: str = "hyvetest"
    mysql_password_env: str = "RACKBRAIN_CINDER_DB_PASS"


def _default_config() -> CinderConfig:
    return CinderConfig(
        seizo_base=os.environ.get(
            "RACKBRAIN_SEIZO_BASE",
            "http://prod-Seizo-SNX-FRE-Base-albConst-1814362187.us-west-2.elb.amazonaws.com",
        ).strip(),
        http_timeout_seconds=int(os.environ.get("RACKBRAIN_SEIZO_TIMEOUT_SECONDS", "20")),
        max_report_chars=int(os.environ.get("RACKBRAIN_CINDER_REPORT_MAX_CHARS", "8000")),
        mysql_host=os.environ.get("RACKBRAIN_CINDER_DB_HOST", "10.0.254.101").strip(),
        mysql_user=os.environ.get("RACKBRAIN_CINDER_DB_USER", "qc_read_only").strip(),
        mysql_db=os.environ.get("RACKBRAIN_CINDER_DB_NAME", "hyvetest").strip(),
        mysql_password_env=os.environ.get("RACKBRAIN_CINDER_DB_PASS_ENV", "RACKBRAIN_CINDER_DB_PASS").strip(),
    )


def _require_mysql_password(cfg: CinderConfig) -> str:
    # Keep secrets out of repo: require password from env (no prompting).
    pwd = os.environ.get(cfg.mysql_password_env, "").strip()
    if not pwd:
        # Fallback to RackBrain's normal DB pass if you want to reuse it.
        pwd = os.environ.get("RACKBRAIN_DB_PASS", "").strip()
    if not pwd:
        raise CinderVerificationError(
            f"Missing MySQL password env var ({cfg.mysql_password_env} or RACKBRAIN_DB_PASS)."
        )
    return pwd


def _pretty_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=False)


def _http_get_json(url: str, timeout_s: int) -> Tuple[Any, str]:
    resp = requests.get(url, timeout=timeout_s)
    if resp.status_code != 200:
        raise CinderVerificationError(f"HTTP {resp.status_code} from {url}: {resp.text}")
    raw = resp.text or ""
    try:
        return resp.json(), raw
    except Exception as exc:
        raise CinderVerificationError(f"Non-JSON response from {url}: {exc}\n{raw}")


def _mysql_outpost_fru_table(sn: str, cfg: CinderConfig) -> str:
    pwd = _require_mysql_password(cfg)
    safe_sn = sn.replace("'", "''")
    sql = (
        "SELECT id, sn_tag, hex(test_passed) AS test_passed, test_finished "
        f"FROM outpost_fru WHERE sn_tag = '{safe_sn}';"
    )
    cmd = [
        "mysql",
        "-h", cfg.mysql_host,
        "-u", cfg.mysql_user,
        "-D", cfg.mysql_db,
        f"--password={pwd}",
        "-t",
        "-e", sql,
    ]
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True,
        )
    except FileNotFoundError:
        raise CinderVerificationError("mysql client not found in PATH.")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise CinderVerificationError(f"mysql query failed: {stderr or exc}")

    out = (p.stdout or "").rstrip()
    if not out.strip():
        raise CinderVerificationError(f"No outpost_fru row found for sn_tag={sn}")
    return out


def build_cinder_verification_report(sn: str, config: Optional[CinderConfig] = None) -> str:
    """
    Build the Cinder verification report text (no Jira writes).

    Data sources:
      1) hyvetest DB: outpost_fru by sn_tag
      2) Seizo API:
         - GET /execution/list/SNX.<SN>
         - GET /execution/<execution_id> (first execution)
    """
    sn = (sn or "").strip()
    if not sn:
        raise CinderVerificationError("Missing SN.")

    cfg = config or _default_config()
    if not cfg.seizo_base:
        raise CinderVerificationError("Missing Seizo base URL (RACKBRAIN_SEIZO_BASE).")

    db_block = _mysql_outpost_fru_table(sn, cfg)

    base = cfg.seizo_base.rstrip("/")
    list_url = f"{base}/{cfg.list_path.strip('/')}/SNX.{sn}"
    list_obj, _ = _http_get_json(list_url, cfg.http_timeout_seconds)
    list_pretty = _pretty_json(list_obj)

    execution_id = None
    try:
        executions_list = list_obj.get("executions_list") or []
        if executions_list and isinstance(executions_list, list):
            execution_id = (executions_list[0] or {}).get("execution_id")
    except Exception:
        execution_id = None

    if not execution_id:
        raise CinderVerificationError("No execution_id found.")

    exec_url = f"{base}/{cfg.exec_path.strip('/')}/{execution_id}"
    exec_obj, _ = _http_get_json(exec_url, cfg.http_timeout_seconds)
    details_block = _pretty_json(exec_obj)

    body = "\n\n".join([db_block, list_pretty, details_block]).rstrip()
    if not body.strip():
        raise CinderVerificationError(f"Empty report for SN {sn}.")

    if len(body) > cfg.max_report_chars:
        body = body[: cfg.max_report_chars]

    return body
