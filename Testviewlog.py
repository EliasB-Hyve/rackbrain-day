#!/usr/bin/env python3
"""
TestView / hyvetest log helper.

Goals:
- For a given server SN, find the latest failing SLT run (optionally
  filtered by testcase substring and/or testset).
- Fetch the corresponding TestView log for a chosen testcase.
- Extract selected lines/snippets from the log (for use in RackBrain comments).

This file is designed to be:
- Importable by RackBrain (library functions).
- Runnable standalone for quick manual triage.
"""

import os
from typing import Optional, List, Dict, Any, Tuple

import pymysql
import requests
import urllib3


# ========================= CONFIG =========================

# TestView base
BASE_URL = os.environ.get(
    "HYVE_TESTVIEW_BASE_URL",
    "https://testview-eve-fmt.hyvesolutions.org",
)

# Cookie for TestView:
# Recommended: export HYVE_TESTVIEW_COOKIE='request_id=...; access_token=...'
COOKIE_ENV_VAR = "HYVE_TESTVIEW_COOKIE"
COOKIE_FALLBACK = ""  # Keep empty; set HYVE_TESTVIEW_COOKIE in your shell.


# hyvetest DB config
DB_HOST = os.environ.get("RACKBRAIN_DB_HOST", os.environ.get("HYVETEST_DB_HOST", "")).strip()
DB_PORT = int(os.environ.get("HYVETEST_DB_PORT", "3306"))
DB_USER = os.environ.get("RACKBRAIN_DB_USER", os.environ.get("HYVETEST_DB_USER", "")).strip()
DB_PASSWORD = os.environ.get("RACKBRAIN_DB_PASS", os.environ.get("HYVETEST_DB_PASSWORD", "")).strip()
DB_NAME = os.environ.get("RACKBRAIN_DB_NAME", os.environ.get("HYVETEST_DB_NAME", "hyvetest")).strip()


# ========================= CORE HELPERS =========================

def _get_cookie_header() -> str:
    """Return a Cookie header string for TestView."""
    env_val = os.environ.get(COOKIE_ENV_VAR, "").strip()
    if env_val:
        return env_val
    if COOKIE_FALLBACK.strip():
        return COOKIE_FALLBACK.strip()
    raise RuntimeError(
        "No TestView cookie configured. Set "
        f"{COOKIE_ENV_VAR} in your shell."
    )


def _make_testview_session(cookie_header: Optional[str] = None) -> requests.Session:
    """Create a requests.Session for talking to TestView."""
    if cookie_header is None:
        cookie_header = _get_cookie_header()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    sess = requests.Session()
    sess.verify = False  # internal TLS, OK to skip in this context

    sess.headers["Cookie"] = cookie_header
    sess.headers["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
    return sess


def validate_and_start_slt(
    sn: str,
    operation: str = "SLT",
    cookie_header: Optional[str] = None,
    base_url: str = BASE_URL,
    do_validate: bool = True,
) -> Dict[str, Any]:
    """
    Validate and start an SLT (or PRETEST) run for the given SN in TestView.

    Returns a dict with:
      {
        "validate_status": int or None,
        "validate_text": str or None,
        "start_status": int,
        "start_text": str,
      }
    """
    sess = _make_testview_session(cookie_header=cookie_header)
    base = base_url.rstrip("/") + "/api/v1/server_level_tests/start"

    result: Dict[str, Any] = {
        "validate_status": None,
        "validate_text": None,
        "start_status": None,
        "start_text": None,
    }

    # Optional validate_server
    if do_validate:
        v = sess.post(f"{base}/validate_server/{sn}?operation={operation}")
        result["validate_status"] = v.status_code
        result["validate_text"] = v.text

    # start_test MUST have a JSON body ({}); otherwise TestView returns 422.
    s = sess.post(
        f"{base}/start_test/{sn}?operation={operation}",
        json={},
    )
    result["start_status"] = s.status_code
    result["start_text"] = s.text

    return result


def build_log_url(
    sn: str,
    slt_id: int,
    testset: str,
    testcase: str,
    filename: str = "log.raw",
    base_url: str = BASE_URL,
) -> str:
    """
    Build the TestView download URL for a given testcase log.
    """
    return (
        "{base}/api/v1/download/{sn}/{slt_id}/{testset}/{testcase}/{filename}?inline=true"
        .format(
            base=base_url.rstrip("/"),
            sn=sn,
            slt_id=slt_id,
            testset=testset,
            testcase=testcase,
            filename=filename,
        )
    )


def fetch_log_text(
    sn: str,
    slt_id: int,
    testset: str,
    testcase: str,
    filename: str = "log.raw",
    cookie_header: Optional[str] = None,
    base_url: str = BASE_URL,
) -> str:
    """
    Download a log from TestView and return it as text.

    Errors:
    - Raises requests.HTTPError if HTTP status is not 2xx.
    - Raises other exceptions for network issues, cookie issues, etc.
    """
    sess = _make_testview_session(cookie_header=cookie_header)
    url = build_log_url(sn, slt_id, testset, testcase, filename, base_url=base_url)
    resp = sess.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def _get_db_conn():
    """Open a hyvetest DB connection."""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _parse_testcases(failed_testcase: Optional[str]) -> List[str]:
    """Split '3_PROGRAM,...,5_CHECK_ROT_FRU' into ['3_PROGRAM', '5_CHECK_ROT_FRU']."""  # noqa: D401
    if not failed_testcase:
        return []
    return [tc.strip() for tc in failed_testcase.split(",") if tc.strip()]


def get_runs_for_sn(sn: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Fetch recent ServerStatus runs for a given server SN (newest first).

    Returns rows with:
      sn, slt_id, ss_ok, started, finished,
      failed_testset, failed_testcase, failure_message, associated_testset_guti
    """
    sql = """
        SELECT
          s.sn_tag AS sn,
          ss.id    AS slt_id,
          ss.ok    AS ss_ok,
          ss.started,
          ss.finished,
          JSON_UNQUOTE(ss.states->'$.jar_deliver."associatedTestSetName"')
            AS failed_testset,
          JSON_UNQUOTE(ss.states->'$.jar_deliver."testErrorCode"')
            AS failed_testcase,
          JSON_UNQUOTE(ss.states->'$.jar_deliver."failureMessage"')
            AS failure_message,
          JSON_UNQUOTE(ss.states->'$.jar_deliver."associatedTestSetGuti"')
            AS associated_testset_guti
        FROM Server s
        JOIN ServerStatus ss ON s.id = ss.server_id
        WHERE s.sn_tag = %s
        ORDER BY ss.finished DESC
        LIMIT %s
    """
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (sn, limit))
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows


def get_run_by_slt_id(sn: str, slt_id: int) -> Dict[str, Any]:
    """
    Fetch a specific ServerStatus run for a given SN + SLT ID.

    Returns a dict with:
      sn, slt_id, ss_ok, started, finished,
      failed_testset, failed_testcase, failure_message,
      associated_testset_guti, operation
    """
    if not sn:
        raise RuntimeError("SN is required for get_run_by_slt_id.")
    if slt_id is None:
        raise RuntimeError("slt_id is required for get_run_by_slt_id.")

    sql = """
        SELECT
          s.sn_tag AS sn,
          ss.id    AS slt_id,
          ss.ok    AS ss_ok,
          ss.started,
          ss.finished,
          COALESCE(
            JSON_UNQUOTE(ss.states->'$.jar_deliver."associatedTestSetName"'),
            JSON_UNQUOTE(ss.states->'$.operation_records[0]."operation_name"'),
            JSON_UNQUOTE(ss.states->'$.operation_records[0]."operation"')
          ) AS failed_testset,
          JSON_UNQUOTE(ss.states->'$.jar_deliver."testErrorCode"')
            AS failed_testcase,
          JSON_UNQUOTE(ss.states->'$.jar_deliver."failureMessage"')
            AS failure_message,
          JSON_UNQUOTE(ss.states->'$.jar_deliver."associatedTestSetGuti"')
            AS associated_testset_guti,
          COALESCE(
            JSON_UNQUOTE(ss.states->'$.operation_records[0]."operation_name"'),
            JSON_UNQUOTE(ss.states->'$.jar_deliver."associatedTestSetName"')
          ) AS operation
        FROM Server s
        JOIN ServerStatus ss ON s.id = ss.server_id
        WHERE s.sn_tag = %s AND ss.id = %s
        LIMIT 1
    """
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (sn, slt_id))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise RuntimeError(f"No ServerStatus row found for sn={sn} slt_id={slt_id}.")

    testset_value = row.get("failed_testset") or row.get("operation")
    if not (isinstance(testset_value, str) and testset_value.strip()):
        raise RuntimeError(
            f"Missing failed_testset/operation for sn={sn} slt_id={slt_id}."
        )
    row["failed_testset"] = testset_value

    failed_testcase = row.get("failed_testcase")
    if not (isinstance(failed_testcase, str) and failed_testcase.strip()):
        raise RuntimeError(
            f"Missing failed_testcase for sn={sn} slt_id={slt_id}."
        )

    return row


def compute_same_failure_count(runs: List[Dict[str, Any]]) -> int:
    """
    Given runs sorted newest-first, return how many consecutive runs (starting
    from index 0) have the same failing testset+testcase and ss_ok = 0.

    Returns 0 if latest run is PASS or has no failed_testset/testcase.
    """
    if not runs:
        return 0

    first = runs[0]
    if first["ss_ok"] != 0 or not first["failed_testset"] or not first["failed_testcase"]:
        return 0

    key = (first["failed_testset"], first["failed_testcase"])
    count = 1

    for r in runs[1:]:
        if r["ss_ok"] != 0:
            break
        if (r["failed_testset"], r["failed_testcase"]) != key:
            break
        count += 1

    return count


def get_latest_failed_run(
    sn: str,
    testcase_contains: Optional[str] = None,
    testset: Optional[str] = None,
    limit: int = 20,
) -> Optional[Dict[str, Any]]:
    """
    Return the latest failing SLT run for a server, optionally restricted by:
      - testcase_contains: substring to match in failed_testcase
      - testset: exact match for failed_testset

    Returns a dict with:
      sn, slt_id, ss_ok, started, finished,
      failed_testset, failed_testcase, failure_message,
      same_failure_count, testcases, all_runs
    or None if nothing matches.
    """
    runs = get_runs_for_sn(sn, limit=limit)
    if not runs:
        return None

    testset_norm = testset.strip().lower() if isinstance(testset, str) and testset.strip() else None
    tc_norm = (
        testcase_contains.strip().lower()
        if isinstance(testcase_contains, str) and testcase_contains.strip()
        else None
    )

    # Filter for failing runs and optional constraints
    for r in runs:
        if r["ss_ok"] != 0:
            continue
        if testset_norm and (r.get("failed_testset") or "").strip().lower() != testset_norm:
            continue
        if tc_norm and tc_norm not in (r.get("failed_testcase") or "").lower():
            continue

        # This is our candidate latest failing run
        same_fail = compute_same_failure_count(runs)
        out = r.copy()
        out["same_failure_count"] = same_fail
        out["testcases"] = _parse_testcases(out["failed_testcase"])
        out["all_runs"] = runs
        return out

    return None


# ========================= LOG SNIPPET HELPERS =========================

def _find_ci(haystack: str, needle: str, start: int = 0) -> int:
    """
    Case-insensitive substring search.
    Returns the index in the original string (or -1).
    """
    if haystack is None or needle is None:
        return -1
    try:
        return str(haystack).lower().find(str(needle).lower(), int(start or 0))
    except Exception:
        return -1


def apply_line_filter(lines, filter_text):
    if not filter_text:
        return lines

    ft = filter_text.lower()
    filtered = [l for l in lines if ft in l.lower()]

    if not filtered:
        return [f"[RackBrain] No lines containing '{filter_text}' found in selected TestView section."]

    return filtered


def select_log_segment(
    log_text: str,
    line_contains: Optional[str] = None,
    line_before: int = 0,
    line_after: int = 0,
    line_between_start_contains: Optional[str] = None,
    line_between_end_contains: Optional[str] = None,
    line_after_contains: Optional[str] = None,
    line_after_chars: int = 0,
    between_start_contains: Optional[str] = None,
    between_end_contains: Optional[str] = None,
    filter_line_contains: Optional[str] = None,
) -> Optional[str]:
    """
    Extract a segment from log_text.

    Modes:
    - If line_between_start_contains + line_between_end_contains:
        return the substring between the two markers on the same line(s).
    - Else if line_after_contains:
        return N characters after the marker on the same line(s).
    - If between_start_contains and between_end_contains are provided:
        return the lines between the first line containing start and the first
        line containing end after that.
    - Else if line_contains is provided:
        find first line containing it, and return that line plus N lines
        before and M lines after.
    - If nothing matches, return None.
    """
    lines = log_text.splitlines()

    # Inline extraction on a single line
    if (line_between_start_contains and line_between_end_contains) or line_after_contains:
        fragments = []

        if line_between_start_contains and line_between_end_contains:
            for line in lines:
                start_idx = _find_ci(line, line_between_start_contains)
                if start_idx == -1:
                    continue
                start_idx += len(line_between_start_contains)
                end_rel = _find_ci(line[start_idx:], line_between_end_contains)
                if end_rel == -1:
                    continue
                end_idx = start_idx + end_rel
                fragment = line[start_idx:end_idx].strip()
                if fragment:
                    fragments.append(fragment)

        if line_after_contains:
            take = int(line_after_chars or 0)
            for line in lines:
                start_idx = _find_ci(line, line_after_contains)
                if start_idx == -1:
                    continue
                start_idx += len(line_after_contains)
                fragment = line[start_idx:] if take <= 0 else line[start_idx:start_idx + take]
                fragment = fragment.strip()
                if fragment:
                    fragments.append(fragment)

        fragments = apply_line_filter(fragments, filter_line_contains)
        return "\n".join(fragments) if fragments else None

    # Between markers mode
    if between_start_contains and between_end_contains:
        start_tok = between_start_contains.lower()
        end_tok = between_end_contains.lower()

        start_positions = [i for i, l in enumerate(lines) if start_tok in l.lower()]
        end_positions = [i for i, l in enumerate(lines) if end_tok in l.lower()]

        if not start_positions or not end_positions:
            return None

        # Choose the smallest possible snippet by pairing an end marker with the closest start marker before it.
        best_pair = None  # (start_idx, end_idx, length)

        for e in end_positions:
            candidates = [s for s in start_positions if s < e]
            if not candidates:
                continue
            s = candidates[-1]
            length = e - s
            if best_pair is None or length < best_pair[2]:
                best_pair = (s, e, length)

        if best_pair is None:
            return None

        start_idx, end_idx, _ = best_pair
        seg_lines = lines[start_idx:end_idx + 1]

        seg_lines = apply_line_filter(seg_lines, filter_line_contains)
        return "\n".join(seg_lines)

    # Single anchor line mode
    if line_contains:
        needle = str(line_contains).lower()
        for i, line in enumerate(lines):
            if needle in str(line).lower():
                start = max(0, i - max(0, line_before))
                end = min(len(lines), i + max(0, line_after) + 1)
                seg_lines = lines[start:end]
                seg_lines = apply_line_filter(seg_lines, filter_line_contains)
                return "\n".join(seg_lines)

    return None


def get_log_segment_for_sn(
    sn: str,
    testcase_contains: str,
    select_config: Dict[str, Any],
    testset: Optional[str] = None,
    cookie_header: Optional[str] = None,
    base_url: str = BASE_URL,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """
    Convenience helper for RackBrain:

    1. Find the latest failing run for SN where failed_testcase contains
       testcase_contains (and optional testset).
    2. Fetch the corresponding TestView log for the matching testcase.
    3. Extract a segment using select_config (see select_log_segment).

    Returns (run_info, log_text, snippet).
    """
    run = get_latest_failed_run(
        sn=sn,
        testcase_contains=testcase_contains,
        testset=testset,
        limit=20,
    )
    if not run:
        return None, None, None

    # Choose a specific testcase; if multiple, prefer one containing testcase_contains
    testcases = run.get("testcases") or []
    chosen_tc = None
    for tc in testcases:
        if testcase_contains in tc:
            chosen_tc = tc
            break
    if not chosen_tc and testcases:
        chosen_tc = testcases[0]

    if not chosen_tc:
        return run, None, None

    run["chosen_testcase"] = chosen_tc
    log_text = fetch_log_text(
        sn=run["sn"],
        slt_id=int(run["slt_id"]),
        testset=run["failed_testset"],
        testcase=chosen_tc,
        base_url=base_url,
        cookie_header=cookie_header,
    )

    snippet = select_log_segment(log_text, **select_config)
    return run, log_text, snippet


# ========================= SIMPLE CLI FOR TESTING =========================

def _prompt_sn() -> Optional[str]:
    sn = input("Enter server SN (blank to quit): ").strip()
    return sn or None


def _print_runs_summary(runs: List[Dict[str, Any]]) -> None:
    if not runs:
        print("No runs found.")
        return

    print("\nRecent SLT runs (newest first):")
    print("Run  SLT_ID   Finished              Status  TestSet         Failed Testcase(s)")
    print("---- -------- -------------------- ------- --------------- -------------------")
    for idx, r in enumerate(runs, start=1):
        status = "FAIL" if r["ss_ok"] == 0 else "PASS"
        finished = r["finished"] or r["started"]
        failed_set = r["failed_testset"] or "-"
        failed_case = r["failed_testcase"] or "-"
        print(
            f"{idx:<4} {str(r['slt_id']):<8} {str(finished):<20} {status:<7} "
            f"{failed_set:<15} {failed_case}"
        )


def _standalone_main() -> None:
    print("TestView / hyvetest SLT log helper")
    print("===================================")
    sn = _prompt_sn()
    if not sn:
        print("Exiting.")
        return

    try:
        runs = get_runs_for_sn(sn, limit=20)
    except Exception as e:
        print("[ERROR] Failed to query DB:", e)
        return

    if not runs:
        print(f"No runs found in hyvetest for SN {sn}.")
        return

    _print_runs_summary(runs)

    testcase_filter = input(
        "\nOptional testcase substring filter (e.g. 'PROGRAM_SYSTEM_RECORD', "
        "blank for no filter): "
    ).strip() or None

    print("\nFinding latest failing run matching filters...")
    run = get_latest_failed_run(sn, testcase_contains=testcase_filter)
    if not run:
        print("No matching failing run found.")
        return

    print("\nLatest matching failing run:")
    print(f"  SN           : {run['sn']}")
    print(f"  SLT_ID       : {run['slt_id']}")
    print(f"  Status       : {'FAIL' if run['ss_ok'] == 0 else 'PASS'}")
    print(f"  TestSet      : {run['failed_testset']}")
    print(f"  Failed cases : {run['failed_testcase']}")
    print(f"  same_failcnt : {run['same_failure_count']}")

    # Choose selection mode interactively
    print("\nLog selection mode:")
    print("  [1] Anchor + context (line_contains + before/after)")
    print("  [2] Between two markers (between_start_contains / between_end_contains)")
    mode = input("Select mode (1/2, default 1): ").strip() or "1"

    select_config: Dict[str, Any] = {}
    if mode == "2":
        select_config["between_start_contains"] = input(
            "Start marker substring: "
        ).strip()
        select_config["between_end_contains"] = input(
            "End marker substring: "
        ).strip()
    else:
        select_config["line_contains"] = input(
            "Anchor substring (line_contains): "
        ).strip()
        try:
            select_config["line_before"] = int(input("Lines before (int, default 0): ") or "0")
            select_config["line_after"] = int(input("Lines after (int, default 0): ") or "0")
        except ValueError:
            select_config["line_before"] = 0
            select_config["line_after"] = 0

    print("\nFetching log + snippet...")
    run_info, log_text, snippet = get_log_segment_for_sn(
        sn=sn,
        testcase_contains=testcase_filter or "",
        select_config=select_config,
    )

    if log_text is None:
        print("[ERROR] Could not fetch log (check cookie or connectivity).")
        return

    out_name = f"log_{sn}_{run['slt_id']}_{run['failed_testset']}.txt"
    with open(out_name, "w", encoding="utf-8", errors="replace") as f:
        f.write(log_text)
    print(f"\n[INFO] Full log saved to: {out_name}")

    print("\n[INFO] Selected snippet:")
    print("------------------------------------------------------------")
    if snippet:
        print(snippet)
    else:
        print("(No snippet matched the selection criteria.)")


if __name__ == "__main__":
    _standalone_main()
