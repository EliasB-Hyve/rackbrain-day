from typing import Dict, List, Optional, Tuple


def _to_jira_code_block(text: str) -> str:
    """
    Wrap text in Jira wiki {code} blocks.
    Returns empty string if there's no content.
    """
    if not text:
        return ""
    text = str(text).rstrip()
    if not text:
        return ""
    return "{code}\n" + text + "\n{code}"


def _select_inline_fragments(
    lines, between_start, between_end, after_contains, after_chars
):
    fragments = []

    if between_start and between_end:
        for line in lines:
            start_idx = line.find(between_start)
            if start_idx == -1:
                continue
            start_idx += len(between_start)
            end_idx = line.find(between_end, start_idx)
            if end_idx == -1:
                continue
            fragment = line[start_idx:end_idx].strip()
            if fragment:
                fragments.append(fragment)

    if after_contains:
        take = int(after_chars or 0)
        for line in lines:
            start_idx = line.find(after_contains)
            if start_idx == -1:
                continue
            start_idx += len(after_contains)
            fragment = line[start_idx:] if take <= 0 else line[start_idx:start_idx + take]
            fragment = fragment.strip()
            if fragment:
                fragments.append(fragment)

    return fragments


def _resolve_source_text(error_event, source: str) -> str:
    if not source:
        return ""

    current = error_event
    for part in str(source).split("."):
        if current is None:
            return ""
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return str(current) if current is not None else ""


def _select_smallest_between(lines: List[str], start_sub: str, end_sub: str) -> List[str]:
    if not start_sub or not end_sub:
        return []

    start_positions = [i for i, l in enumerate(lines) if start_sub in l]
    end_positions = [i for i, l in enumerate(lines) if end_sub in l]
    if not start_positions or not end_positions:
        return []

    best_pair: Optional[Tuple[int, int, int]] = None  # (start, end, length)
    for e in end_positions:
        candidates = [s for s in start_positions if s < e]
        if not candidates:
            continue
        s = candidates[-1]
        length = e - s
        if best_pair is None or length < best_pair[2]:
            best_pair = (s, e, length)

    if best_pair is None:
        return []
    s, e, _ = best_pair
    return lines[s:e + 1]


def _extract_value_from_text(text: str, spec: dict) -> str:
    if not text:
        return ""

    lines = text.splitlines()

    between_start = spec.get("between_start_contains")
    between_end = spec.get("between_end_contains")
    if between_start and between_end:
        lines = _select_smallest_between(lines, str(between_start), str(between_end))

    if not lines:
        return ""

    line_filter = spec.get("line_contains")
    if line_filter:
        token = str(line_filter)
        lines = [l for l in lines if token in l]
        if not lines:
            return ""

    inline_between_start = spec.get("line_between_start_contains")
    inline_between_end = spec.get("line_between_end_contains")
    inline_after = spec.get("line_after_contains")
    inline_after_chars = spec.get("line_after_chars", 0)
    has_inline = bool((inline_between_start and inline_between_end) or inline_after)
    if has_inline:
        fragments = _select_inline_fragments(
            lines,
            str(inline_between_start) if inline_between_start else None,
            str(inline_between_end) if inline_between_end else None,
            str(inline_after) if inline_after else None,
            int(inline_after_chars or 0),
        )
        take = str(spec.get("take") or "first").lower()
        if not fragments:
            return ""
        if take == "last":
            return fragments[-1]
        if take == "all":
            return "\n".join(fragments)
        return fragments[0]

    # Default: return selected block
    return "\n".join(lines).strip()


def _apply_text_extracts(error_event, action) -> Dict[str, str]:
    extracts = getattr(action, "text_extracts", None) or []
    out: Dict[str, str] = {}

    for spec in extracts:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if not name:
            continue

        source = spec.get("source") or "failure_message"
        text = _resolve_source_text(error_event, source)
        value = _extract_value_from_text(text or "", spec)

        if not value:
            default_val = spec.get("default", "")
            value = str(default_val) if default_val is not None else ""

        out[str(name)] = str(value)

    return out


def _select_failure_message_lines(error_event, action) -> str:
    """
    Select a subset of error_event.failure_message based on the action's
    failure_message_* settings, similar to _select_lines for command stdout.

    Supports:
      - failure_message_line_between_start_contains + _end_contains
      - failure_message_line_after_contains + failure_message_line_after_chars
      - failure_message_between_start_contains + _end_contains
      - failure_message_line_contains + line_before/line_after
    """
    msg = error_event.failure_message or ""
    if not msg:
        return ""

    lines = msg.splitlines()
    inline_start = getattr(action, "failure_message_line_between_start_contains", None)
    inline_end = getattr(action, "failure_message_line_between_end_contains", None)
    inline_after = getattr(action, "failure_message_line_after_contains", None)
    inline_after_chars = getattr(action, "failure_message_line_after_chars", 0)
    has_inline = bool((inline_start and inline_end) or inline_after)
    if has_inline:
        fragments = _select_inline_fragments(
            lines,
            inline_start,
            inline_end,
            inline_after,
            inline_after_chars,
        )
        return "\n".join(fragments) if fragments else ""

    selected_idx = set()

    # 1) Span selection between two markers
    start_sub = getattr(action, "failure_message_between_start_contains", None)
    end_sub = getattr(action, "failure_message_between_end_contains", None)

    if start_sub and end_sub:
        start_idx = None
        end_idx = None
        for i, line in enumerate(lines):
            if start_idx is None and start_sub in line:
                start_idx = i
            if start_idx is not None and end_sub in line:
                end_idx = i
                break
        if start_idx is not None and end_idx is not None and end_idx >= start_idx:
            for j in range(start_idx, end_idx + 1):
                selected_idx.add(j)

    # 2) Per-line contains + context
    line_sub = getattr(action, "failure_message_line_contains", None)
    before = getattr(action, "failure_message_line_before", 0) or 0
    after = getattr(action, "failure_message_line_after", 0) or 0

    if line_sub:
        for i, line in enumerate(lines):
            if line_sub in line:
                start = max(0, i - before)
                end = min(len(lines), i + after + 1)
                for j in range(start, end):
                    selected_idx.add(j)

    if not selected_idx:
        return ""

    ordered_lines = [lines[i] for i in sorted(selected_idx)]
    return "\n".join(ordered_lines)


def _format_command_history(command_history) -> str:
    """
    Format all commands in history as a Jira code block.
    """
    if not command_history:
        return ""

    lines = []
    for i, cmd in enumerate(command_history, start=1):
        lines.append(f"--- Command {i}: {cmd.context} {cmd.cmd} (status={cmd.status}) ---")
        if cmd.selected_lines:
            lines.append(cmd.selected_lines)
        elif cmd.stdout:
            # Show first 200 chars if no selection
            preview = cmd.stdout[:200]
            if len(cmd.stdout) > 200:
                preview += "... (truncated)"
            lines.append(preview)
        lines.append("")  # blank line between commands

    return _to_jira_code_block("\n".join(lines).strip())


def _format_commands_summary(command_history) -> str:
    """
    Format a summary table of all commands executed.
    """
    if not command_history:
        return "No commands executed."

    lines = ["ID | Context | Status | Output Length"]
    lines.append("---|---------|-------|---------------")
    for cmd in command_history:
        output_len = len(cmd.stdout or "")
        lines.append(f"{cmd.cmd_id} | {cmd.context} | {cmd.status} | {output_len} chars")

    return "\n".join(lines)


def _normalize_ws(s: str) -> str:
    # Collapse all whitespace (spaces, tabs, newlines) into single spaces
    return " ".join((s or "").split())


def _select_ilom_components(error_event, action) -> str:
    problems = getattr(error_event, "ilom_problems", []) or []
    filters = getattr(action, "ilom_filter_contains", None)

    if not problems:
        return ""

    # Normalize and drop empty filters
    norm_filters = []
    if filters:
        for f in filters:
            norm = _normalize_ws(str(f).lower())
            if norm:  # skip empty strings like ""
                norm_filters.append(norm)

    # If no usable filters, include all unique components
    if not norm_filters:
        seen = set()
        comps = []
        for p in problems:
            if p.component not in seen:
                seen.add(p.component)
                comps.append(p.component)
        return ", ".join(comps)

    # With filters: include components whose description OR component name
    # contains any of the filter phrases (after whitespace normalization).
    seen = set()
    comps = []

    for p in problems:
        norm_desc = _normalize_ws((p.description or "").lower())
        norm_comp = _normalize_ws((p.component or "").lower())

        if any(f in norm_desc or f in norm_comp for f in norm_filters):
            if p.component not in seen:
                seen.add(p.component)
                comps.append(p.component)
    return ", ".join(comps)


def build_comment_body(rule_match, error_event, template_override: str = None) -> str:
    """
    Build the Jira comment text from the rule's template and the error event.
    """
    rule = rule_match.rule
    action = rule.action
    template = template_override or (action.comment_template or "")

    ticket_key = error_event.ticket.key
    sn = error_event.sn or "UNKNOWN_SN"

    # Uses ilom_problems + action.ilom_filter_contains (if present)
    ilom_components = _select_ilom_components(error_event, action)

    context = {
        "ticket_key": ticket_key,
        "sn": sn,
        "rule_id": getattr(rule, "id", rule.name),
        "rule_name": rule.name,
        "confidence": "%.2f" % rule_match.confidence,
        # extra fields for templates
        "arch": error_event.arch or "UNKNOWN_ARCH",
        "testcase": error_event.testcase or "UNKNOWN_TESTCASE",
        "error_details": (error_event.error_details or "").strip(),
        "ilom_components": ilom_components,

        "evbot_version": error_event.evbot_version or "",
        "jira_model": error_event.jira_model or "",
        "jira_customer_ipn": error_event.jira_customer_ipn or "",
        "jira_slt_rack_sn": error_event.jira_slt_rack_sn or "",
        "jira_tester_email": error_event.jira_tester_email or "",
        "jira_test_started": error_event.jira_test_started or "",
        "jira_test_finished": error_event.jira_test_finished or "",
        "jira_test_duration_minutes": (
            "%.1f" % error_event.jira_test_duration_minutes
            if error_event.jira_test_duration_minutes is not None
            else ""
        ),

        "last_cmd_context": error_event.last_cmd_context or "",
        "last_cmd": error_event.last_cmd or "",
        "last_cmd_status": error_event.last_cmd_status
        if error_event.last_cmd_status is not None else "",
        "last_cmd_stdout": error_event.last_cmd_stdout or "",
        "last_cmd_selected_lines": error_event.last_cmd_selected_lines or "",

        "last_cmd_stdout_code": _to_jira_code_block(error_event.last_cmd_stdout),
        "last_cmd_selected_lines_code": _to_jira_code_block(
            error_event.last_cmd_selected_lines
        ),
        # NEW: raw ILOM Open_Problems output + Jira {code} wrapper
        "ilom_open_problems_raw": error_event.ilom_open_problems_raw or "",
        "ilom_open_problems_code": _to_jira_code_block(
            error_event.ilom_open_problems_raw
        ),

        "failure_message_selected": _select_failure_message_lines(error_event, action),
        "failure_message_selected_code": _to_jira_code_block(
            _select_failure_message_lines(error_event, action)
        ),

        # NEW: DB / TestView metadata for templates
        "db_failed_testcase": getattr(error_event, "db_failed_testcase", "") or "",
        "db_failed_testcase_list": ", ".join(
            getattr(error_event, "db_failed_testcase_list", []) or []
        ),
        "db_same_failure_count": getattr(error_event, "db_same_failure_count", "")
        or "",
        "db_latest_failed_testset": getattr(
            error_event, "db_latest_failed_testset", ""
        ) or "",

        # NEW: SLT start info (TestView API from cmd101-sr1)
        "slt_validate_status": getattr(error_event, "slt_validate_status", "") or "",
        "slt_validate_response": getattr(error_event, "slt_validate_response", "") or "",
        "slt_validate_response_code": _to_jira_code_block(
            getattr(error_event, "slt_validate_response", "")
        ),
        "slt_start_status": getattr(error_event, "slt_start_status", "") or "",
        "slt_start_response": getattr(error_event, "slt_start_response", "") or "",
        "slt_start_response_code": _to_jira_code_block(
            getattr(error_event, "slt_start_response", "")
        ),

        # NEW: TestView log snippet (fallback to error string if unavailable)
        "testview_log_snippet": (
            getattr(error_event, "testview_log_snippet", "") or ""
        )
        or (getattr(error_event, "testview_log_error", "") or ""),
        "testview_log_snippet_code": _to_jira_code_block(
            (getattr(error_event, "testview_log_snippet", "") or "")
            or (getattr(error_event, "testview_log_error", "") or "")
        ),
        "testview_log_error": getattr(error_event, "testview_log_error", "") or "",
        "testview_log_error_code": _to_jira_code_block(
            getattr(error_event, "testview_log_error", "") or ""
        ),

        # NEW: DB latest SLT ID
        "db_latest_slt_id": str(getattr(error_event, "db_latest_slt_id", "") or ""),

        # NEW: Command history (all commands executed)
        "all_commands_code": _format_command_history(error_event.command_history),
        "commands_summary": _format_commands_summary(error_event.command_history),
        "command_count": str(len(error_event.command_history)),

        "jira_location": getattr(error_event, "jira_location", "") or "",
        "jira_customer": getattr(error_event, "jira_customer", "") or "",
        "jira_slt_attempts": error_event.jira_slt_attempts or "",
        "telnet_cmd": getattr(error_event, "telnet_cmd", "") or "",
        "jira_latest_comment_text": getattr(error_event, "jira_latest_comment_text", "") or "",
        "jira_latest_comment_author": getattr(error_event, "jira_latest_comment_author", "") or "",
        "jira_latest_comment_author_display_name": getattr(error_event, "jira_latest_comment_author_display_name", "") or "",
        "jira_latest_comment_author_email": getattr(error_event, "jira_latest_comment_author_email", "") or "",
        "jira_reporter": getattr(error_event, "jira_reporter", "") or "",
        "cinder_report": getattr(error_event, "cinder_report", "") or "",
        "cinder_report_code": _to_jira_code_block(getattr(error_event, "cinder_report", "") or ""),
    }

    extracted = _apply_text_extracts(error_event, action)
    for k, v in extracted.items():
        if k not in context:
            context[k] = v

    # Add per-command placeholders (e.g., {command_cmd_1_stdout}, {command_ilom_check_selected_lines})
    for cmd_result in error_event.command_history:
        cmd_id = cmd_result.cmd_id
        prefix = f"command_{cmd_id}"

        # Full stdout
        context[f"{prefix}_stdout"] = cmd_result.stdout or ""
        context[f"{prefix}_stdout_code"] = _to_jira_code_block(cmd_result.stdout)

        # Full stderr (often contains wrapper/ssh errors)
        context[f"{prefix}_stderr"] = cmd_result.stderr or ""
        context[f"{prefix}_stderr_code"] = _to_jira_code_block(cmd_result.stderr)

        # Selected lines
        context[f"{prefix}_selected_lines"] = cmd_result.selected_lines or ""
        context[f"{prefix}_selected_lines_code"] = _to_jira_code_block(cmd_result.selected_lines)

        # Command details
        context[f"{prefix}_context"] = cmd_result.context or ""
        context[f"{prefix}_cmd"] = cmd_result.cmd or ""
        context[f"{prefix}_status"] = str(cmd_result.status) if cmd_result.status is not None else ""

        # Combined info
        context[f"{prefix}_info"] = f"{cmd_result.context} {cmd_result.cmd} (status={cmd_result.status})"

    print("[DEBUG] action.start:", getattr(action, "failure_message_between_start_contains", None))
    print("[DEBUG] action.end:", getattr(action, "failure_message_between_end_contains", None))

    print("[DEBUG] failure_message_selected:\n", context["failure_message_selected"])

    try:
        body = template.format(**context)
    except KeyError as exc:
        # Fail safe: if template is misconfigured, don't crash the whole run.
        body = (
            "[rackbrain] Template formatting error (%s).\n\n"
            "Rule: %s\nTicket: %s\nSN: %s\n"
            % (exc, getattr(rule, "id", rule.name), ticket_key, sn)
        )

    # Append a consistent "RackBrain" signature to every comment (once).
    # Exceptions: some comments should be "pure payload" without any footer.
    if getattr(rule, "id", None) in ("approval_request_ack", "cinder_verification_close"):
        return body
    signature = "🤖"
    stripped = (body or "").rstrip()
    if stripped and not stripped.endswith(signature):
        body = stripped + "\n\n" + signature

    return body
