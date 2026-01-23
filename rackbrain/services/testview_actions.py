import re
from typing import Any, Dict, Optional, Tuple

from Testviewlog import get_log_segment_for_sn, validate_and_start_slt, select_log_segment


def maybe_start_slt_for_action(error_event, action, dry_run: bool) -> None:
    """
    Start TestView SLT/PRETEST if requested.

    Priority:
      1) Conditional trigger set by a command_step (ErrorEvent.testview_start_requested)
      2) Unconditional action.start_slt (existing behavior)
    """
    sn = getattr(error_event, "sn", None)
    if not sn:
        return

    # 1) Command-step conditional trigger
    if getattr(error_event, "testview_start_requested", False):
        operation = getattr(error_event, "testview_start_operation", None) or "SLT"
        use_validate = getattr(error_event, "testview_start_use_validate", True)

    # 2) Fallback: action-level unconditional start
    elif getattr(action, "start_slt", False):
        operation = getattr(action, "slt_operation", "SLT")
        use_validate = getattr(action, "slt_use_validate", True)

    else:
        return

    if dry_run:
        print(f"[DRY-RUN] Would start TestView operation={operation} for SN={sn}")
        # Optional: fill fields so templates aren't blank in dry runs
        error_event.slt_start_status = "DRY_RUN"
        error_event.slt_start_response = f"DRY RUN - would start {operation} for {sn}"
        return

    try:
        res = validate_and_start_slt(
            sn=sn,
            operation=operation,
            do_validate=use_validate,
        )
    except Exception as exc:
        print(f"[WARN] Failed to start TestView {operation} for {sn}: {exc}")
        error_event.slt_start_status = None
        error_event.slt_start_response = f"Exception: {exc}"
        return

    # Persist for templates
    error_event.slt_validate_status = res.get("validate_status")
    error_event.slt_validate_response = res.get("validate_text")
    error_event.slt_start_status = res.get("start_status")
    error_event.slt_start_response = res.get("start_text")


def populate_testview_log_for_action(error_event, action):
    """
    If this action requests a TestView log snippet (via testview_* settings),
    fetch it and store it on the ErrorEvent for use in templates.

    This is intentionally a no-op unless testview_testcase_contains is set
    in the rule's action YAML.
    """
    testcase_sub, testset_override, select_config = _resolve_testview_request(action)
    if not testcase_sub:
        return

    sn = getattr(error_event, "sn", None)
    if not sn:
        return

    # Clear any stale state so templates don't leak old values.
    error_event.testview_log_error = None
    error_event.testview_log_snippet = None

    # Optional override of testset from YAML; otherwise use DB/Jira view
    testset = (
        testset_override
        or getattr(error_event, "db_latest_failed_testset", None)
        or getattr(error_event, "failed_testset", None)
    )

    try:
        run_info, full_log, snippet = get_log_segment_for_sn(
            sn=sn,
            testcase_contains=testcase_sub,
            select_config=select_config,
            testset=testset,
        )
    except Exception as exc:
        msg = f"TestView log/snippet fetch failed: {exc}"
        error_event.testview_log_error = msg
        print(f"[WARN] Failed to fetch TestView log for {sn}: {exc}")
        return

    if not run_info:
        error_event.testview_log_error = (
            f"TestView run not found for sn={sn} testcase_contains={testcase_sub!r} testset={testset!r}"
        )
        return

    if full_log:
        error_event.testview_log_text = full_log
    else:
        chosen_tc = run_info.get("chosen_testcase") or testcase_sub
        error_event.testview_log_error = (
            f"TestView log download returned empty for sn={sn} slt_id={run_info.get('slt_id')} "
            f"testset={run_info.get('failed_testset')!r} testcase={chosen_tc!r}"
        )
        return

    selector_present = any(
        [
            select_config.get("line_contains"),
            select_config.get("line_between_start_contains"),
            select_config.get("line_between_end_contains"),
            select_config.get("line_after_contains"),
            select_config.get("between_start_contains"),
            select_config.get("between_end_contains"),
        ]
    )
    if snippet is None and selector_present:
        chosen_tc = run_info.get("chosen_testcase") or testcase_sub
        error_event.testview_log_error = (
            f"TestView snippet selector did not match for sn={sn} slt_id={run_info.get('slt_id')} "
            f"testset={run_info.get('failed_testset')!r} testcase={chosen_tc!r} "
            f"line_contains={select_config.get('line_contains')!r}"
        )
        return

    if snippet is not None:
        error_event.testview_log_snippet = snippet


def select_testview_case_template(error_event, action) -> Optional[str]:
    """
    Evaluate ordered TestView cases (first match wins) and return the comment template.

    Return values:
      - None: no cases configured
      - " " : cases configured but no case matched (forces 'no comment' behavior)
      - "<template>": a matching case template
    """
    cfg = getattr(action, "testview", None)
    if not isinstance(cfg, dict):
        return None

    cases = cfg.get("cases")
    if not isinstance(cases, list) or not cases:
        return None

    full_text = getattr(error_event, "testview_log_text", "") or ""
    default_snippet = getattr(error_event, "testview_log_snippet", "") or ""

    def _match_contains(haystack: str, needle: str) -> bool:
        return str(needle).lower() in str(haystack).lower()

    def _match_regex(haystack: str, pattern: str) -> bool:
        try:
            return re.search(str(pattern), str(haystack), flags=re.IGNORECASE) is not None
        except Exception:
            return False

    for case in cases:
        if not isinstance(case, dict):
            continue

        when = case.get("when")
        if not isinstance(when, dict):
            when = {}

        comment_template = case.get("comment_template")
        if not isinstance(comment_template, str):
            comment_template = ""

        # Optional per-case snippet selection overrides action.testview.select
        case_select = case.get("select")
        case_select_present = isinstance(case_select, dict)
        case_snippet: Optional[str] = None
        if case_select_present and full_text:
            sel_cfg = _resolve_testview_select_config(case_select)
            try:
                case_snippet = select_log_segment(full_text, **sel_cfg)
            except Exception:
                case_snippet = None

        snippet_for_case = (
            (case_snippet or "") if case_select_present else (default_snippet or "")
        )

        source = str(when.get("source") or "auto").strip().lower()
        if source in ("log_text", "text", "full", "full_log"):
            haystack = full_text
        elif source in ("log_snippet", "snippet"):
            haystack = snippet_for_case
        else:
            # Default: prefer snippet if available, otherwise fall back to full log text.
            haystack = snippet_for_case or full_text

        if not haystack:
            continue

        matched = False

        # Support either:
        #   when: { contains: "..." }
        #   when: { regex: "..." }
        #   when: { type: "contains", value: "..." } (pattern-like)
        if "contains" in when:
            matched = _match_contains(haystack, when.get("contains"))

        elif "regex" in when:
            matched = _match_regex(haystack, when.get("regex"))

        else:
            ptype = str(when.get("type") or "").strip().lower()
            pvalue = when.get("value")
            if ptype == "contains":
                matched = _match_contains(haystack, pvalue)
            elif ptype == "regex":
                matched = _match_regex(haystack, pvalue)
            else:
                matched = False

        if matched:
            if case_select_present:
                # Ensure {testview_log_snippet*} reflects this case's selection
                # (and avoid leaking a different case's snippet).
                error_event.testview_log_snippet = case_snippet or ""
            return comment_template

    # Cases configured but none matched: suppress comment.
    return " "


def _resolve_testview_request(action) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """
    Resolve TestView fetch + selection settings from either:
      - action.testview (new nested shape), or
      - legacy action.testview_* fields (backwards compatible).
    """
    cfg = getattr(action, "testview", None)
    if isinstance(cfg, dict):
        testcase_contains = None
        testcase = cfg.get("testcase")
        if isinstance(testcase, str) and testcase.strip():
            testcase_contains = testcase.strip()
        elif isinstance(testcase, dict):
            val = testcase.get("contains") or testcase.get("value")
            if isinstance(val, str) and val.strip():
                testcase_contains = val.strip()

        # Allow an alternate key for convenience.
        if not testcase_contains:
            alt = cfg.get("testcase_contains")
            if isinstance(alt, str) and alt.strip():
                testcase_contains = alt.strip()

        testset = cfg.get("testset")
        testset = testset.strip() if isinstance(testset, str) and testset.strip() else None

        select = cfg.get("select")
        select = select if isinstance(select, dict) else {}

        select_config = _resolve_testview_select_config(select)
        return testcase_contains, testset, select_config

    # Legacy (existing rules)
    testcase_sub = getattr(action, "testview_testcase_contains", None)
    if isinstance(testcase_sub, str):
        testcase_sub = testcase_sub.strip() or None
    else:
        testcase_sub = None

    testset_override = getattr(action, "testview_testset", None)
    if isinstance(testset_override, str):
        testset_override = testset_override.strip() or None
    else:
        testset_override = None

    select_config = {
        "line_contains": getattr(action, "testview_line_contains", None),
        "line_before": getattr(action, "testview_line_before", 0),
        "line_after": getattr(action, "testview_line_after", 0),
        "line_between_start_contains": getattr(
            action, "testview_line_between_start_contains", None
        ),
        "line_between_end_contains": getattr(
            action, "testview_line_between_end_contains", None
        ),
        "line_after_contains": getattr(action, "testview_line_after_contains", None),
        "line_after_chars": getattr(action, "testview_line_after_chars", 0),
        "between_start_contains": getattr(action, "testview_between_start_contains", None),
        "between_end_contains": getattr(action, "testview_between_end_contains", None),
        "filter_line_contains": getattr(action, "testview_filter_line_contains", None),
    }
    return testcase_sub, testset_override, select_config


def _resolve_testview_select_config(select: Dict[str, Any]) -> Dict[str, Any]:
    def _int(v, default=0):
        try:
            return int(v)
        except Exception:
            return default

    return {
        "line_contains": select.get("line_contains"),
        "line_before": _int(select.get("line_before"), 0),
        "line_after": _int(select.get("line_after"), 0),
        "line_between_start_contains": select.get("line_between_start_contains"),
        "line_between_end_contains": select.get("line_between_end_contains"),
        "line_after_contains": select.get("line_after_contains"),
        "line_after_chars": _int(select.get("line_after_chars"), 0),
        "between_start_contains": select.get("between_start_contains"),
        "between_end_contains": select.get("between_end_contains"),
        "filter_line_contains": select.get("filter_line_contains"),
    }
