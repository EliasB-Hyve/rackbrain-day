from typing import List, Optional

from rackbrain.core.models import RuleCommandStep, CommandResult
from rackbrain.eve_command_runner import run_eve_command, EveCommandResult
from rackbrain.services.comment_renderer import _apply_text_extracts


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


def _select_lines(stdout: str, step: RuleCommandStep) -> str:
    """
    Return a subset of stdout based on the step's line_* and between_* settings.

    - line_contains + line_before/line_after:
        pick any line containing the substring, plus N lines of context.

    - line_between_start_contains + line_between_end_contains:
        extract text between two markers on the same line(s).

    - line_after_contains + line_after_chars:
        extract N characters after the marker on the same line(s).

    - between_start_contains + between_end_contains:
        pick all lines from the first line containing 'start' through
        the first line containing 'end' (inclusive).

    Both selectors can be used together; we de-duplicate and keep order.
    """
    if not stdout:
        return ""

    lines = stdout.splitlines()
    not_contains = getattr(step, "line_not_contains", None)

    inline_start = getattr(step, "line_between_start_contains", None)
    inline_end = getattr(step, "line_between_end_contains", None)
    inline_after = getattr(step, "line_after_contains", None)
    inline_after_chars = getattr(step, "line_after_chars", 0)
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
    if step.between_start_contains and step.between_end_contains:
        start_idx = None
        end_idx = None
        for i, line in enumerate(lines):
            if start_idx is None and step.between_start_contains in line:
                start_idx = i
            if start_idx is not None and step.between_end_contains in line:
                end_idx = i
                break
        if start_idx is not None and end_idx is not None and end_idx >= start_idx:
            for j in range(start_idx, end_idx + 1):
                selected_idx.add(j)

    # 2) Per-line contains + context (or only matching lines)
    if step.line_contains:
        if step.line_only:
            # Only matching lines, no context
            for i, line in enumerate(lines):
                if step.line_contains in line and (not not_contains or not_contains not in line):
                    selected_idx.add(i)
        else:
            # Matching lines with context
            before = step.line_before or 0
            after = step.line_after or 0
            for i, line in enumerate(lines):
                if step.line_contains in line and (not not_contains or not_contains not in line):
                    start = max(0, i - before)
                    end = min(len(lines), i + after + 1)
                    for j in range(start, end):
                        selected_idx.add(j)

    # 3) Negative-only selection: lines that do NOT contain a substring
    if not selected_idx and not_contains and not step.line_contains:
        for i, line in enumerate(lines):
            if not_contains not in line:
                selected_idx.add(i)

    if not selected_idx:
        return ""

    ordered_lines = [lines[i] for i in sorted(selected_idx)]
    if not_contains:
        ordered_lines = [line for line in ordered_lines if not_contains not in line]
    return "\n".join(ordered_lines)


def execute_command_steps(error_event, action, skip_commands: bool = False):
    """
    Run the optional command_steps for this action.

    Args:
        error_event: The ErrorEvent to update
        action: The RuleAction containing command_steps
        skip_commands: If True, skip all command execution (for faster dry-runs)

    Returns:
      - (override_comment_template (str) or None, timer_after_seconds (int) or None)
    and updates error_event.last_cmd_* with the last run command,
    and error_event.command_history with all executed commands.
    """
    steps = getattr(action, "command_steps", None) or []
    sn = error_event.sn
    ticket_key = getattr(getattr(error_event, "ticket", None), "key", "") or ""

    if not sn or not steps or skip_commands:
        return None, None

    override_comment = None
    timer_request_seconds = None
    previous_stdout = ""  # Track previous command's stdout for if_previous_contains

    extracts = _apply_text_extracts(error_event, action) if getattr(action, "text_extracts", None) else {}

    def _first_nonempty_line(value: object) -> str:
        if value is None:
            return ""
        for line in str(value).splitlines():
            candidate = line.strip()
            if candidate:
                return candidate
        return str(value).strip()

    def _items_from_extract(extract_name: str) -> List[str]:
        raw = extracts.get(extract_name) or ""
        items = []
        seen = set()
        for line in str(raw).splitlines():
            item = line.strip()
            if not item:
                continue
            if item in seen:
                continue
            seen.add(item)
            items.append(item)
        return items

    def _record_result(
        step: RuleCommandStep,
        result: EveCommandResult,
        selected: str,
        cmd_id_suffix: Optional[str] = None,
    ) -> None:
        cmd_id = getattr(step, "id", None) or f"cmd_{len(error_event.command_history) + 1}"
        if cmd_id_suffix:
            cmd_id = f"{cmd_id}_{cmd_id_suffix}"
        cmd_result = CommandResult(
            cmd_id=cmd_id,
            context=result.context,
            cmd=result.cmd,
            status=result.status,
            stdout=(result.stdout or "")[:4000],  # Truncate for storage
            stderr=result.stderr or "",
            selected_lines=selected,
        )
        error_event.command_history.append(cmd_result)

        # Update ErrorEvent with last command info for templates (backward compatibility)
        error_event.last_cmd_context = result.context
        error_event.last_cmd = result.cmd
        error_event.last_cmd_status = result.status
        error_event.last_cmd_stdout = (result.stdout or "")[:4000]
        error_event.last_cmd_selected_lines = selected or None

    def _trigger_testview(step: RuleCommandStep, on_pass: bool) -> None:
        if getattr(error_event, "testview_start_requested", False):
            return
        if on_pass and getattr(step, "start_testview_on_pass", False):
            error_event.testview_start_requested = True
            error_event.testview_start_operation = getattr(step, "testview_operation_on_pass", "SLT")
            error_event.testview_start_use_validate = getattr(step, "testview_use_validate_on_pass", True)
        if (not on_pass) and getattr(step, "start_testview_on_fail", False):
            error_event.testview_start_requested = True
            error_event.testview_start_operation = getattr(step, "testview_operation_on_fail", "SLT")
            error_event.testview_start_use_validate = getattr(step, "testview_use_validate_on_fail", True)

    for step in steps:
        # Check if_previous_contains condition
        if step.if_previous_contains:
            if step.if_previous_contains not in previous_stdout:
                print(f"[INFO] Skipping command step (if_previous_contains not met): {step.cmd}")
                continue

        cmd_str = step.cmd

        # Basic placeholders for rule authors (keep YAML readable).
        # Note: we intentionally do not use `.format()` because command strings
        # also contain context markers like `{ilom}` / `{diag}`.
        if "{sn}" in cmd_str:
            cmd_str = cmd_str.replace("{sn}", sn)
        if ticket_key and "{ticket_key}" in cmd_str:
            cmd_str = cmd_str.replace("{ticket_key}", ticket_key)

        telnet_cmd = getattr(error_event, "telnet_cmd", None)
        if "{telnet_cmd}" in cmd_str:
            if telnet_cmd:
                cmd_str = cmd_str.replace("{telnet_cmd}", telnet_cmd)
            else:
                # Fallback: attempt to extract from failure_message / combined_text.
                try:
                    from rackbrain.core.jira_extractors import extract_telnet_cmd

                    telnet_cmd = extract_telnet_cmd(getattr(error_event, "failure_message", None)) or extract_telnet_cmd(
                        getattr(error_event, "combined_text", "")
                    )
                except Exception:
                    telnet_cmd = None

                if telnet_cmd:
                    setattr(error_event, "telnet_cmd", telnet_cmd)
                    cmd_str = cmd_str.replace("{telnet_cmd}", telnet_cmd)
                else:
                    print("[INFO] Skipping telnet step - no telnet_cmd extracted")
                    continue

        # Allow using action-level `text_extracts` variables inside command strings.
        # Example: cmd: "{ilom} {ilom_failed_cmd}"
        for extract_name, extract_value in extracts.items():
            placeholder = "{" + str(extract_name) + "}"
            if placeholder in cmd_str:
                cmd_str = cmd_str.replace(placeholder, _first_nonempty_line(extract_value))

        def _as_tokens(value: object) -> List[str]:
            if value is None:
                return []
            if isinstance(value, list):
                return [str(v) for v in value if v is not None and str(v)]
            return [str(value)] if str(value) else []

        def _evaluate_expectations(result: EveCommandResult) -> bool:
            stdout = result.stdout or ""
            status_ok = step.expect_status is None or result.status == step.expect_status

            contains_tokens = _as_tokens(step.expect_contains)
            contains_ok = (not contains_tokens) or all(token in stdout for token in contains_tokens)

            not_contains_tokens = _as_tokens(step.expect_not_contains)
            not_contains_ok = (not not_contains_tokens) or all(
                token not in stdout for token in not_contains_tokens
            )

            return status_ok and contains_ok and not_contains_ok

        # Loop expansion: run this step once per extracted item.
        for_each_name = getattr(step, "for_each_extract", None)
        if for_each_name:
            items = _items_from_extract(for_each_name)
            if not items:
                fake = EveCommandResult(
                    serial=sn,
                    context="unknown",
                    cmd=cmd_str,
                    status=1,
                    stdout="",
                    stderr=f"for_each_extract '{for_each_name}' produced 0 items",
                )
                _record_result(step, fake, selected="", cmd_id_suffix="empty")

                if step.on_expect_fail_comment:
                    override_comment = step.on_expect_fail_comment
                _trigger_testview(step, on_pass=False)

                if override_comment and step.stop_on_decision:
                    break
                continue

            any_fail = False
            runner_failed = False
            for idx, item in enumerate(items, start=1):
                item_cmd = cmd_str.replace("[item]", item)
                result = run_eve_command(sn, item_cmd)
                previous_stdout = result.stdout or ""

                # If the runner didn't execute (wrapper failure), do not branch/comment.
                if not getattr(result, "executed", True):
                    selected = _select_lines(result.stdout or "", step)
                    _record_result(step, result, selected, cmd_id_suffix=str(idx))
                    print(f"[WARN] Runner did not execute for step {getattr(step, 'id', '')}: {result.stderr}")
                    any_fail = True
                    runner_failed = True
                    break

                selected = _select_lines(result.stdout or "", step)
                _record_result(step, result, selected, cmd_id_suffix=str(idx))

                if not _evaluate_expectations(result):
                    any_fail = True

            if runner_failed:
                # Infrastructure failure: don't pick pass/fail comment, just stop processing steps.
                break

            all_ok = (not any_fail) and bool(items)

            step_timer = getattr(step, "timer_after_seconds", None)
            if step_timer is not None:
                has_expectations = any(
                    x is not None
                    for x in (
                        step.expect_status,
                        step.expect_contains,
                        step.expect_not_contains,
                    )
                )
                if (not has_expectations and items) or (has_expectations and all_ok):
                    timer_request_seconds = (
                        int(step_timer)
                        if timer_request_seconds is None
                        else max(int(timer_request_seconds), int(step_timer))
                    )

            if all_ok:
                if step.on_expect_pass_comment:
                    override_comment = step.on_expect_pass_comment
                _trigger_testview(step, on_pass=True)
            else:
                if step.on_expect_fail_comment:
                    override_comment = step.on_expect_fail_comment
                _trigger_testview(step, on_pass=False)

            if override_comment and step.stop_on_decision:
                break

            continue

        # Normal single execution
        result = run_eve_command(sn, cmd_str)
        previous_stdout = result.stdout or ""

        # If the runner didn't execute (wrapper failure), do not branch/comment.
        if not getattr(result, "executed", True):
            selected = _select_lines(result.stdout or "", step)
            _record_result(step, result, selected)
            print(f"[WARN] Runner did not execute for step {getattr(step, 'id', '')}: {result.stderr}")
            break

        selected = _select_lines(result.stdout or "", step)
        _record_result(step, result, selected)

        all_ok = _evaluate_expectations(result)

        step_timer = getattr(step, "timer_after_seconds", None)
        if step_timer is not None:
            has_expectations = any(
                x is not None
                for x in (
                    step.expect_status,
                    step.expect_contains,
                    step.expect_not_contains,
                )
            )
            if (not has_expectations) or all_ok:
                timer_request_seconds = (
                    int(step_timer)
                    if timer_request_seconds is None
                    else max(int(timer_request_seconds), int(step_timer))
                )

        if all_ok:
            if step.on_expect_pass_comment:
                override_comment = step.on_expect_pass_comment
            _trigger_testview(step, on_pass=True)
        else:
            if step.on_expect_fail_comment:
                override_comment = step.on_expect_fail_comment
            _trigger_testview(step, on_pass=False)

        if override_comment and step.stop_on_decision:
            break

    return override_comment, timer_request_seconds
