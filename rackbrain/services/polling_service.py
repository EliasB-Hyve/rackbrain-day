# rackbrain/services/polling_service.py
"""Polling service for RackBrain.

This version is the simple / verbose variant that:
- Does NOT try to suppress or redirect stdout from process_ticket
- Prints a per-ticket line when each ticket is processed
- Prints a short summary at the end of each poll cycle

It is intentionally straightforward so that any hangs or errors during
ticket processing are visible on the console.
"""

import time
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import os
import json
from datetime import datetime


from rackbrain.adapters.jira_client import JiraClient
from rackbrain.core.models import Rule
from rackbrain.services.ticket_processor import process_ticket


def build_default_jql(
    project_key: str = "MFGS",
    allowed_statuses: List[str] = None,
    lookback_hours: int = 1,
) -> str:
    """Build a simple default JQL string.

    This is only used when no custom JQL is provided via config or CLI.
    """
    if allowed_statuses is None:
        allowed_statuses = ["Open", "In Progress"]

    status_clause = " OR ".join([f'status = "{s}"' for s in allowed_statuses])
    jql = (
        f"project = {project_key} "
        f"AND ({status_clause}) "
        f"AND updated >= -{lookback_hours}h "
        f"ORDER BY updated DESC"
    )
    return jql


def process_ticket_safe(
    jira: JiraClient,
    rules: List[Rule],
    issue_key: str,
    dry_run: bool,
    skip_commands: bool,
    processing_config: Optional[Dict[str, Any]] = None,
):
    try:
        # process_ticket already returns an outcome dict with "edited"
        outcome = process_ticket(
            jira=jira,
            rules=rules,
            issue_key=issue_key,
            dry_run=dry_run,
            skip_commands=skip_commands,
            processing_config=processing_config,
        )

        return {
            "success": True,
            "issue_key": issue_key,
            "error": None,
            "edited": outcome.get("edited") if isinstance(outcome, dict) else False,
        }
    except Exception as exc:
        return {
            "success": False,
            "issue_key": issue_key,
            "error": str(exc),
            "edited": False,
        }


def poll_and_process(
    jira: JiraClient,
    rules: List[Rule],
    jql: str,
    dry_run: bool,
    skip_commands: bool,
    max_workers: int,
    max_results: int,
    *,
    query_name: Optional[str] = None,
    skip_issue_keys: Optional[set] = None,
    processing_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a single poll cycle.

    - Queries Jira for issues matching the given JQL
    - Processes each ticket (in parallel) via process_ticket_safe
    - Prints a per-ticket status line
    - Returns aggregate stats
    """
    edited_issue_keys = []
    name_suffix = f" ({query_name})" if query_name else ""
    print(f"[INFO] Searching for tickets{name_suffix} with JQL: {jql}")
    issues = jira.search_issues(
        jql=jql,
        fields=["key", "summary", "status"],
        max_results=max_results,
    )

    issue_keys_found = [i.get("key") for i in issues if isinstance(i, dict)]
    if skip_issue_keys:
        issues = [
            i
            for i in issues
            if isinstance(i, dict) and i.get("key") and i.get("key") not in skip_issue_keys
        ]

    total_found = len(issues)
    skipped = max(len(issue_keys_found) - total_found, 0)
    skipped_msg = f" (skipped {skipped} already-queued)" if skipped else ""
    print(f"[INFO] Found {total_found} ticket(s) matching query{skipped_msg}")

    if total_found == 0:
        return {
            "total_found": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "edited": [],
            "found_issue_keys": issue_keys_found,
            "processed_issue_keys": [],
        }

    processed = 0
    succeeded = 0
    failed = 0
    processed_issue_keys: List[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                process_ticket_safe,
                jira,
                rules,
                issue.get("key"),
                dry_run,
                skip_commands,
                processing_config,
            ): issue.get("key")
            for issue in issues
        }

        for future in as_completed(futures):
            issue_key = futures[future]
            try:
                result = future.result()
                processed += 1
                if issue_key:
                    processed_issue_keys.append(issue_key)
                if result["success"]:
                    succeeded += 1
                    print(f"[OK] Processed {issue_key}")

                    if result.get("edited"):
                        edited_issue_keys.append(issue_key)
                else:
                    failed += 1
                    print(f"[FAIL] {issue_key}: {result['error']}")
            except Exception as exc:
                failed += 1
                processed += 1
                print(f"[ERROR] Exception processing {issue_key}: {exc}")

    return {
        "total_found": total_found,
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "edited": edited_issue_keys,
        "found_issue_keys": issue_keys_found,
        "processed_issue_keys": processed_issue_keys,
    }



def _is_edited_from_actions(actions: dict) -> bool:
    if not isinstance(actions, dict):
        return False
    return bool(
        actions.get("commented") is True
        or actions.get("transitioned_to")
        or actions.get("assigned_to")
        or actions.get("reassigned_to")
    )


def _append_unique_recent(items: List[str], key: str) -> None:
    """
    Move key to the end of the list, keeping uniqueness.
    """
    if not key:
        return
    try:
        items.remove(key)
    except ValueError:
        pass
    items.append(key)


def _load_edited_today_from_log(config: dict, *, since_iso: Optional[str] = None) -> List[str]:
    """
    Best-effort: parse today's processing log and return a de-duplicated list
    of issue keys that were actually edited (comment/assign/transition) in LIVE
    mode, in the order they were last edited (most recent at end).

    Safe: if file missing or format unexpected, returns empty list.
    """
    logging_cfg = (config or {}).get("logging", {})
    enabled = logging_cfg.get("enabled", True)
    if not enabled:
        return []

    log_dir = logging_cfg.get("log_dir", "logs")
    log_file = logging_cfg.get("log_file", "rackbrain_processed.log")
    log_format = logging_cfg.get("log_format", "json")
    rotate_daily = logging_cfg.get("rotate_daily", True)

    # Resolve today's rotated filename (matches ProcessingLogger behavior)
    if rotate_daily:
        date_str = datetime.now().strftime("%Y-%m-%d")
        name, ext = os.path.splitext(log_file)
        filename = f"{name}_{date_str}{ext}"
    else:
        filename = log_file

    path = os.path.join(log_dir, filename)
    if not os.path.exists(path):
        return []

    # Track per-issue last edit timestamp so we can sort deterministically even
    # when log lines arrive out-of-order due to concurrent processing.
    last_edit_ts_by_issue: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                if log_format == "json":
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue

                    # only count LIVE successful edits
                    if entry.get("dry_run") is True:
                        continue
                    if entry.get("success") is not True:
                        continue

                    issue_key = entry.get("issue_key")
                    actions = entry.get("actions_taken") or {}
                    if issue_key and _is_edited_from_actions(actions):
                        ts = entry.get("timestamp") or ""
                        if ts and since_iso and ts < since_iso:
                            continue
                        prev = last_edit_ts_by_issue.get(issue_key)
                        if prev is None or ts >= prev:
                            last_edit_ts_by_issue[issue_key] = ts

                else:
                    # Text logs are harder to parse reliably; skip aggregation.
                    # (You can switch to json logs for this feature.)
                    pass
    except Exception:
        return []

    # ISO timestamps from ProcessingLogger are lexicographically sortable.
    return [
        issue_key
        for issue_key, _ts in sorted(
            last_edit_ts_by_issue.items(), key=lambda kv: kv[1]
        )
    ]


def _load_or_reset_edited_today_window_start(config: dict) -> str:
    """
    Track an "edited today" window start timestamp, with a 12h inactivity reset.

    Requirement:
      - If RackBrain hasn't been run for >12 hours, reset the Edited Today list.
      - Otherwise, keep using the existing window start.

    This is stored locally under state/ so it persists across restarts.
    """
    now = datetime.now()
    default_window_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    state_dir = None
    try:
        paths_cfg = (config or {}).get("paths", {}) if isinstance(config, dict) else {}
        if isinstance(paths_cfg, dict):
            state_dir = paths_cfg.get("state_dir")
    except Exception:
        state_dir = None

    if not isinstance(state_dir, str) or not state_dir.strip():
        state_dir = os.environ.get("RACKBRAIN_STATE_DIR", "").strip()

    if not state_dir:
        home = os.environ.get("RACKBRAIN_HOME", "").strip()
        if home:
            state_dir = os.path.join(home, "state")
        else:
            state_dir = "state"

    state_path = os.path.join(state_dir, "edited_today_state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    state = {}
    try:
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f) or {}
    except Exception:
        state = {}

    last_seen_iso = str(state.get("last_seen_iso") or "").strip()
    window_start_iso = str(state.get("window_start_iso") or "").strip() or default_window_start

    inactivity_reset_seconds = 12 * 60 * 60
    try:
        if last_seen_iso:
            last_seen = datetime.fromisoformat(last_seen_iso)
            if (now - last_seen).total_seconds() > inactivity_reset_seconds:
                window_start_iso = now.isoformat()
    except Exception:
        window_start_iso = default_window_start

    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "window_start_iso": window_start_iso,
                    "last_seen_iso": now.isoformat(),
                },
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass

    return window_start_iso


def _print_edited_ticket_list(label: str, keys: List[str], *, color: str, reset: str) -> None:
    if keys:
        print("")
        print(f"{color}[{label}]{reset}")
        for k in keys:
            print(f"{color}{k}{reset}")
        print("")
    else:
        print("")
        print(f"{color}[{label}] None{reset}")
        print("")



def run_polling_loop(
    jira: JiraClient,
    rules: List[Rule],
    jql: str,
    poll_interval_seconds: int,
    dry_run: bool,
    skip_commands: bool,
    max_workers: int,
    max_results: int,
    run_once: bool = False,
    *,
    app_config: Optional[Dict[str, Any]] = None,
    processing_config: Optional[Dict[str, Any]] = None,
) -> None:
    """Main loop for `rackbrain poll`.

    This function repeatedly calls poll_and_process and sleeps in between
    cycles (unless run_once is True).
    """
    mode_label = "LIVE MODE" if not dry_run else "DRY RUN"
    print(f"=== RackBrain Auto-Polling — {mode_label} ===")
    print(f"[INFO] JQL: {jql}")
    print(f"[INFO] Poll interval: {poll_interval_seconds} seconds")
    print(f"[INFO] Max workers: {max_workers}")
    print(f"[INFO] Max results per poll: {max_results}")
    print(
        "[INFO] Running continuously (Ctrl+C to stop)"
        if not run_once
        else "[INFO] Run-once mode"
    )

    cycle = 0
    try:
        while True:
            cycle += 1
            print(f"\n[INFO] === Poll cycle #{cycle} ===")
            print(f"[INFO] Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            stats_list: List[Dict[str, Any]] = []

            # 1) Primary query: runs the full ruleset
            processed_keys_this_cycle: set = set()
            primary_stats = poll_and_process(
                jira=jira,
                rules=rules,
                jql=jql,
                dry_run=dry_run,
                skip_commands=skip_commands,
                max_workers=max_workers,
                max_results=max_results,
                query_name="primary",
                processing_config=processing_config,
            )
            stats_list.append(primary_stats)
            for k in primary_stats.get("processed_issue_keys") or []:
                processed_keys_this_cycle.add(k)

            # 2) Optional extra queries: restricted subsets of rules
            polling_cfg = (app_config or {}).get("polling", {}) if isinstance(app_config, dict) else {}
            extra_queries = polling_cfg.get("extra_queries") or []
            if isinstance(extra_queries, dict):
                extra_queries = [extra_queries]

            if isinstance(extra_queries, list):
                rules_by_id = {r.id: r for r in rules}
                for idx, q in enumerate(extra_queries, start=1):
                    if not isinstance(q, dict):
                        continue
                    if q.get("enabled") is False:
                        continue

                    extra_jql = q.get("jql")
                    only_rule_ids = q.get("only_rule_ids") or q.get("only_rule_id") or []
                    if isinstance(only_rule_ids, str):
                        only_rule_ids = [only_rule_ids]

                    query_max_results = q.get("max_results", max_results)
                    query_name = q.get("name") or f"extra_{idx}"

                    if not extra_jql or not isinstance(extra_jql, str):
                        print(f"[WARN] Skipping extra query {query_name}: missing polling.extra_queries[].jql")
                        continue

                    if not only_rule_ids:
                        print(
                            f"[WARN] Skipping extra query {query_name}: missing polling.extra_queries[].only_rule_ids"
                        )
                        continue

                    subset_rules: List[Rule] = []
                    missing = []
                    for rid in only_rule_ids:
                        r = rules_by_id.get(rid)
                        if r is None:
                            missing.append(rid)
                        else:
                            subset_rules.append(r)
                    if missing:
                        print(
                            f"[WARN] Extra query {query_name}: unknown rule id(s): {', '.join(missing)}"
                        )
                    if not subset_rules:
                        continue

                    extra_stats = poll_and_process(
                        jira=jira,
                        rules=subset_rules,
                        jql=extra_jql,
                        dry_run=dry_run,
                        skip_commands=skip_commands,
                        max_workers=max_workers,
                        max_results=query_max_results,
                        query_name=query_name,
                        skip_issue_keys=processed_keys_this_cycle,
                        processing_config=processing_config,
                    )
                    stats_list.append(extra_stats)
                    for k in extra_stats.get("processed_issue_keys") or []:
                        processed_keys_this_cycle.add(k)

            # Aggregate per-cycle stats across queries
            stats = {
                "total_found": sum(int(s.get("total_found") or 0) for s in stats_list),
                "processed": sum(int(s.get("processed") or 0) for s in stats_list),
                "succeeded": sum(int(s.get("succeeded") or 0) for s in stats_list),
                "failed": sum(int(s.get("failed") or 0) for s in stats_list),
                "edited": [
                    k
                    for s in stats_list
                    for k in (s.get("edited") or [])
                ],
            }

            print(
                f"[INFO] Cycle #{cycle} complete: "
                f"{stats['total_found']} found, "
                f"{stats['processed']} processed, "
                f"{stats['succeeded']} succeeded, "
                f"{stats['failed']} failed"
            )
            # Highlight edited tickets in turquoise
            TURQUOISE = "\033[38;2;64;224;208m"
            RESET = "\033[0m"
            if run_once:
                print("[INFO] Run-once mode: exiting after one cycle")
                break


            cycle_edited = stats.get("edited") or []

            window_start_iso = _load_or_reset_edited_today_window_start(app_config or {})
            edited_today = _load_edited_today_from_log(app_config or {}, since_iso=window_start_iso)

            # Include the current cycle edits in the aggregate list (move to end)
            # so the most recently edited ticket is visible immediately.
            for k in cycle_edited:
                _append_unique_recent(edited_today, k)

            # 1) Aggregate (today)
            _print_edited_ticket_list("EDITED-TODAY", edited_today, color=TURQUOISE, reset=RESET)

            # 2) Newly edited THIS cycle
            _print_edited_ticket_list("EDITED-CYCLE", cycle_edited, color=TURQUOISE, reset=RESET)



            print(f"[INFO] Sleeping {poll_interval_seconds} seconds until next poll...")
            time.sleep(poll_interval_seconds)

    except KeyboardInterrupt:
        print("\n[INFO] Polling stopped by user (Ctrl+C)")
    except Exception as exc:
        print(f"[ERROR] Unexpected error in polling loop: {exc}")
        raise
