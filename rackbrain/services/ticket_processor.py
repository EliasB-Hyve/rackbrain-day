# rackbrain/services/ticket_processor.py

from typing import Dict, Any, Optional, List

from rackbrain.adapters.jira_client import JiraClient
from rackbrain.core.classification import classify_error
from rackbrain.core.context_builder import build_error_event, build_ticket
from rackbrain.core.models import Rule
from rackbrain.services.command_steps import execute_command_steps
from rackbrain.services.comment_renderer import build_comment_body
from rackbrain.services.jira_actions import apply_jira_actions
from rackbrain.services.logger import get_logger, get_rule_match_history_logger
from rackbrain.services.timer_store import TimerStore
from rackbrain.services.testview_actions import (
    maybe_start_slt_for_action,
    populate_testview_log_for_action,
    select_testview_case_template,
)
from rackbrain.core.jira_extractors import extract_option_value
from rackbrain.integrations.cinder_verification import (
    build_cinder_verification_report,
    CinderVerificationError,
)

MAX_SLT_ATTEMPTS = 7


def _is_cinder_verification_ticket(issue: Dict[str, Any]) -> bool:
    fields = issue.get("fields", {}) or {}
    summary = str(fields.get("summary") or "").strip().lower()
    want = "outpost refurb - cinder verification"
    if want not in summary:
        return False

    location = extract_option_value(fields.get("customfield_15143"))  # Location
    customer = extract_option_value(fields.get("customfield_15119"))  # Customer

    location_ok = str(location or "").strip().lower() == "fremont"
    customer_ok = "woody (outpost)" in str(customer or "").strip().lower()
    return bool(location_ok and customer_ok)


def _safe_int(value, default=None):
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return int(value)
    except Exception:
        return default


def process_ticket(
    jira: JiraClient,
    rules: List[Rule],
    issue_key: str,
    dry_run: bool = True,
    skip_commands: bool = False,
    processing_config: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Fetch a Jira ticket, classify it with rules, and either:
      - print the suggested comment (dry_run=True), or
      - post the comment to Jira (dry_run=False, MVP still comments only).

    For now we do NOT auto-close tickets. That will come later once you're
    comfortable with the behavior.
    """
    # 1. Fetch raw issue from Jira
    issue = jira.get_issue(
        issue_key,
        fields=[
            "summary",
            "description",
            "status",
            "assignee",
            "reporter",
            "updated",
            "comment",
            "customfield_15119",  # Customer (EVE)
            "customfield_15143",  # Location (Fremont)
        ],
    )

    # Jira may truncate comments in the issue payload; fetch the last comment
    # when needed so rules can reliably check "latest comment" fields.
    try:
        comment_field = (issue.get("fields", {}) or {}).get("comment") or {}
        if isinstance(comment_field, dict):
            total = comment_field.get("total")
            max_results = comment_field.get("maxResults")
            start_at = comment_field.get("startAt", 0) or 0
            comments = comment_field.get("comments") or []

            if (
                isinstance(total, int)
                and isinstance(max_results, int)
                and total > int(start_at) + max_results
            ):
                page = jira.get_issue_comments(
                    issue_key,
                    start_at=max(total - 1, 0),
                    max_results=1,
                )
                last_comments = (page or {}).get("comments") or []
                if last_comments:
                    last = last_comments[-1]
                    last_id = last.get("id")
                    if last_id:
                        if not any((c or {}).get("id") == last_id for c in comments):
                            comment_field["comments"] = list(comments) + [last]
                    else:
                        comment_field["comments"] = list(comments) + [last]
    except Exception as exc:
        print(f"[WARN] Failed to fetch latest Jira comment for {issue_key}: {exc}")


    ticket = build_ticket(issue)
    is_cinder_ticket = _is_cinder_verification_ticket(issue)

    required_text = None
    if processing_config:
        required_text = processing_config.get("required_combined_text_contains")

    if required_text and not is_cinder_ticket:
        combined_text = (ticket.summary or "") + "\n\n" + (ticket.description or "")
        if str(required_text).strip().lower() not in combined_text.lower():
            print(
                "[INFO] Ticket %s does not contain required marker text (%s). Skipping RackBrain processing."
                % (issue_key, required_text)
            )
            logger = get_logger()
            if logger:
                logger.log_processed(
                    issue_key=issue_key,
                    success=True,
                    dry_run=dry_run,
                    actions_taken={
                        "action": "skipped_missing_required_combined_text",
                        "required_combined_text_contains": required_text,
                    },
                )
            return {
                "issue_key": issue_key,
                "match": False,
                "rule_id": None,
                "rule_name": None,
                "confidence": None,
                "edited": False,
                "dry_run": dry_run,
                "actions_taken": {
                    "action": "skipped_missing_required_combined_text",
                    "required_combined_text_contains": required_text,
                },
            }

    error_event = build_error_event(ticket)

    same_fail = getattr(error_event, "db_same_failure_count", 0) or 0
    # NOTE: We intentionally do NOT decide "same failure" skipping here,
    # because allow_on_same_failure should only affect eligibility within the
    # current ruleset (after timers/suppression), not globally across all rules.




    # Get allowed statuses from config or use defaults
    if processing_config:
        allowed_statuses = tuple(processing_config.get("allowed_statuses", ["Open", "In Progress"]))
    else:
        allowed_statuses = ("Open", "In Progress")

    status_name = (
        ticket.raw.get("fields", {})
        .get("status", {})
        .get("name", "")
    )

    if status_name not in allowed_statuses:
        print(
            "[INFO] Ticket %s is not in a processable status (status=%s). Skipping RackBrain processing."
            % (issue_key, status_name)
        )
        logger = get_logger()
        if logger:
            logger.log_processed(
                issue_key=issue_key,
                success=False,
                error=f"Status '{status_name}' not in allowed statuses",
                dry_run=dry_run,
            )
        return



    timer_store = TimerStore(processing_config)
    current_rearm_key = TimerStore.build_rearm_key(
        status_name, getattr(error_event, "jira_assignee", None)
    )
    timer_store.cleanup_expired(issue_key, current_rearm_key)
    active_timer = timer_store.get_active_timer(issue_key)
    if active_timer:
        remaining_s = int(active_timer.seconds_remaining())
        print(
            f"[INFO] Skipping {issue_key}: timer active (rule_id={active_timer.rule_id}, remaining={remaining_s}s)"
        )
        logger = get_logger()
        if logger:
            logger.log_processed(
                issue_key=issue_key,
                success=True,
                dry_run=dry_run,
                actions_taken={
                    "action": "skipped_timer_active",
                    "timer_rule_id": active_timer.rule_id,
                    "timer_remaining_seconds": remaining_s,
                },
            )
        return {
            "issue_key": issue_key,
            "match": False,
            "rule_id": None,
            "rule_name": None,
            "confidence": None,
            "edited": False,
            "dry_run": dry_run,
            "actions_taken": {
                "action": "skipped_timer_active",
                "timer_rule_id": active_timer.rule_id,
                "timer_remaining_seconds": remaining_s,
            },
        }

    # Expose expired timers (for workflow follow-ups) under the current rearm_key.
    try:
        error_event.timer_expired_for = timer_store.list_expired_rule_ids(issue_key, current_rearm_key)
    except Exception:
        error_event.timer_expired_for = []

    eligible_rules = [
        r
        for r in rules
        if not timer_store.is_rule_suppressed(issue_key, r.id, current_rearm_key)
    ]

    # If this ticket has repeated identical failures, only allow rules that explicitly opt in.
    # This avoids one allow_on_same_failure rule affecting the entire ruleset.
    if same_fail >= 2:
        override_rules = [
            r for r in eligible_rules if getattr(r, "allow_on_same_failure", False)
        ]
        if not override_rules:
            print(
                f"[INFO] Skipping {issue_key}: db_same_failure_count={same_fail} (no allow_on_same_failure rules)."
            )
            logger = get_logger()
            if logger:
                logger.log_processed(
                    issue_key=issue_key,
                    success=True,
                    dry_run=dry_run,
                    actions_taken={
                        "action": "skipped_same_failure",
                        "db_same_failure_count": same_fail,
                    },
                )
            return {
                "issue_key": issue_key,
                "match": False,
                "rule_id": None,
                "rule_name": None,
                "confidence": None,
                "edited": False,
                "dry_run": dry_run,
                "actions_taken": {
                    "action": "skipped_same_failure",
                    "db_same_failure_count": same_fail,
                },
            }

        eligible_rules = override_rules
        print(
            f"[INFO] db_same_failure_count={same_fail} >= 2; "
            f"restricting to {len(eligible_rules)} allow_on_same_failure rule(s)."
        )

    print("[DEBUG] testcase:", error_event.testcase)
    print("[DEBUG] failure_message:", error_event.failure_message)
    print("[DEBUG] failed_testset:", error_event.failed_testset)
    print("[DEBUG] model:", error_event.model)
    print("[DEBUG] jira_slt_attempts:", error_event.jira_slt_attempts)
    print("[DEBUG] jira_latest_comment_author:", getattr(error_event, "jira_latest_comment_author", None))
    latest_comment_preview = (getattr(error_event, "jira_latest_comment_text", "") or "")[:200]
    print("[DEBUG] jira_latest_comment_text (preview):", latest_comment_preview)

    slt_attempts = _safe_int(getattr(error_event, "jira_slt_attempts", None))
    if slt_attempts is not None and slt_attempts > MAX_SLT_ATTEMPTS:
        override_rules = [
            r for r in eligible_rules if getattr(r, "allow_high_slt_attempts", False)
        ]
        if not override_rules:
            print(f"[INFO] Skipping {issue_key}: jira_slt_attempts={slt_attempts} > {MAX_SLT_ATTEMPTS}")
            logger = get_logger()
            if logger:
                logger.log_processed(
                    issue_key=issue_key,
                    success=True,
                    dry_run=dry_run,
                    actions_taken={
                        "action": "skipped_high_slt_attempts",
                        "jira_slt_attempts": slt_attempts,
                        "max_slt_attempts": MAX_SLT_ATTEMPTS,
                    },
                )
            return {
                "issue_key": issue_key,
                "match": False,
                "rule_id": None,
                "rule_name": None,
                "confidence": None,
                "edited": False,
                "dry_run": dry_run,
                "actions_taken": {
                    "action": "skipped_high_slt_attempts",
                    "jira_slt_attempts": slt_attempts,
                    "max_slt_attempts": MAX_SLT_ATTEMPTS,
                },
            }

        eligible_rules = override_rules
        print(
            f"[INFO] jira_slt_attempts={slt_attempts} > {MAX_SLT_ATTEMPTS}; "
            f"restricting to {len(eligible_rules)} override rule(s)."
        )



    # 2. Classify based on combined text
    match = classify_error(error_event, eligible_rules, min_confidence=0.75)

    if not match:
        print("[INFO] No matching rule for %s." % issue_key)
        logger = get_logger()
        if logger:
            logger.log_no_match(issue_key=issue_key, dry_run=dry_run)
        # Let poller know processing succeeded but no rule matched
        return {
            "issue_key": issue_key,
            "match": False,
            "rule_id": None,
            "rule_name": None,
            "confidence": None,
            "edited": False,
            "dry_run": dry_run,
            "actions_taken": {},
        }

    print(
        "[INFO] Matched rule: %s (%s) with confidence=%.2f"
        % (match.rule.id, match.rule.name, match.confidence)
    )

    history_logger = get_rule_match_history_logger()
    if history_logger:
        history_logger.log_match(rule_id=match.rule.id, issue_key=issue_key, dry_run=dry_run)

    # NEW: if this rule has command_steps, run them via eve_command_runner.
    # This will:
    #   - run diagnostics on the EVE server (diag/ilom/hostnic/etc.)
    #   - populate error_event.last_cmd_* for use in templates
    #   - optionally provide an override comment template
    action = match.rule.action

    # Cinder verification integration: build report used by the rule template.
    # If report generation fails, do not touch the ticket.
    if match.rule.id == "cinder_verification_close":
        if not (error_event.sn or "").strip():
            msg = "Missing SN for Cinder Verification ticket."
            print(f"[WARN] {issue_key}: {msg}")
            logger = get_logger()
            if logger:
                logger.log_processed(
                    issue_key=issue_key,
                    rule_id=match.rule.id,
                    rule_name=match.rule.name,
                    confidence=match.confidence,
                    success=False,
                    error=msg,
                    dry_run=dry_run,
                    actions_taken={"action": "cinder_report_failed", "reason": "missing_sn"},
                )
            return {
                "issue_key": issue_key,
                "match": True,
                "rule_id": match.rule.id,
                "rule_name": match.rule.name,
                "confidence": match.confidence,
                "edited": False,
                "dry_run": dry_run,
                "actions_taken": {"action": "cinder_report_failed", "reason": "missing_sn"},
            }

        try:
            error_event.cinder_report = build_cinder_verification_report(error_event.sn)
        except CinderVerificationError as exc:
            msg = str(exc)
            print(f"[WARN] {issue_key}: Cinder report build failed: {msg}")
            logger = get_logger()
            if logger:
                logger.log_processed(
                    issue_key=issue_key,
                    rule_id=match.rule.id,
                    rule_name=match.rule.name,
                    confidence=match.confidence,
                    success=False,
                    error=msg,
                    dry_run=dry_run,
                    actions_taken={"action": "cinder_report_failed"},
                )
            return {
                "issue_key": issue_key,
                "match": True,
                "rule_id": match.rule.id,
                "rule_name": match.rule.name,
                "confidence": match.confidence,
                "edited": False,
                "dry_run": dry_run,
                "actions_taken": {"action": "cinder_report_failed"},
            }

    # 3. Run any configured EVE commands (diag/ilom/etc.)
    template_override, step_timer_seconds = execute_command_steps(
        error_event, action, skip_commands=skip_commands
    )
    
    # 3b. Optionally start SLT from cmd101-sr1 via TestView API

    maybe_start_slt_for_action(error_event, action, dry_run=dry_run)


    # 4. Optionally fetch a TestView log snippet for this rule
    populate_testview_log_for_action(error_event, action)

    # 4b. Optional: choose comment template based on TestView content (first match wins).
    tv_template_override = select_testview_case_template(error_event, action)
    if tv_template_override is not None:
        template_override = tv_template_override

    # 5. Build Jira comment body, allowing command_steps to override template
    comment_body = build_comment_body(match, error_event, template_override)

    timer_seconds = step_timer_seconds
    if timer_seconds is None:
        timer_seconds = getattr(action, "timer_after_seconds", None)



    if dry_run:
        print("\n===== Suggested Jira comment (DRY RUN) =====\n")
        print(comment_body)
        if timer_seconds:
            print(f"\n[DRY RUN] timer_after_seconds: {int(timer_seconds)} (not persisted)")
        print("\n============================================\n")
        # Log dry-run
        logger = get_logger()
        if logger:
            logger.log_processed(
                issue_key=issue_key,
                rule_id=match.rule.id,
                rule_name=match.rule.name,
                confidence=match.confidence,
                success=True,
                dry_run=True,
                actions_taken={"action": "dry_run", "comment_preview": "generated"},
            )

        # Outcome for callers (poller); no real edits in dry-run
        return {
            "issue_key": issue_key,
            "match": True,
            "rule_id": match.rule.id,
            "rule_name": match.rule.name,
            "confidence": match.confidence,
            "edited": False,
            "dry_run": True,
            "actions_taken": {"action": "dry_run"},
        }


        # Let callers know this was a dry-run (no edits made)
        return {
            "issue_key": issue_key,
            "rule_id": match.rule.id,
            "edited": False,
            "dry_run": True,
            "actions_taken": {"action": "dry_run", "comment_preview": "generated"},
        }


    # --- REAL MODE BELOW: assign -> transition -> comment -> reassign ---
    # If a rule is using a timer with no comment (workflow staging), keep the ticket
    # assigned to the current user so it will be eligible for the next poll cycle.
    action_for_jira = action
    if timer_seconds and isinstance(comment_body, str) and not comment_body.strip():
        if getattr(action, "reassign_to", None) is None:
            from copy import copy

            action_for_jira = copy(action)
            action_for_jira.reassign_to = ""

    actions_taken = apply_jira_actions(
        jira=jira,
        issue_key=issue_key,
        comment_body=comment_body,
        processing_config=processing_config,
        action=action_for_jira,
        error_event=error_event,
    )

    if timer_seconds:
        effective_assignee = getattr(error_event, "jira_assignee", None)
        effective_status = status_name

        reassigned = actions_taken.get("reassigned_to")
        assigned = actions_taken.get("assigned_to")
        transitioned = actions_taken.get("transitioned_to")

        if isinstance(reassigned, str) and reassigned and not reassigned.startswith("FAILED"):
            effective_assignee = reassigned
        elif isinstance(assigned, str) and assigned and not assigned.startswith("FAILED"):
            effective_assignee = assigned

        if isinstance(transitioned, str) and transitioned and not (
            transitioned.startswith("FAILED") or transitioned.startswith("NOT_FOUND")
        ):
            effective_status = transitioned

        final_rearm_key = TimerStore.build_rearm_key(effective_status, effective_assignee)
        rec = timer_store.start_timer(
            issue_key=issue_key,
            rule_id=match.rule.id,
            seconds=int(timer_seconds),
            rearm_key=final_rearm_key,
        )
        actions_taken["timer_started_seconds"] = int(timer_seconds)
        actions_taken["timer_remaining_seconds"] = int(rec.seconds_remaining())

    # Log successful processing
    logger = get_logger()
    if logger:
        logger.log_processed(
            issue_key=issue_key,
            rule_id=match.rule.id,
            rule_name=match.rule.name,
            confidence=match.confidence,
            success=True,
            dry_run=False,
            actions_taken=actions_taken,
        )


    # Tell callers whether this ticket was actually edited in Jira
    commented_ok = actions_taken.get("commented") is True
    edited = bool(
        commented_ok
        or actions_taken.get("transitioned_to")
        or actions_taken.get("assigned_to")
        or actions_taken.get("reassigned_to")
    )

    return {
        "issue_key": issue_key,
        "match": True,
        "rule_id": match.rule.id,
        "rule_name": match.rule.name,
        "confidence": match.confidence,
        "edited": edited,
        "dry_run": False,
        "actions_taken": actions_taken,
    }
