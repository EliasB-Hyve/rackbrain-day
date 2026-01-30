# -*- coding: utf-8 -*-
"""
Merged ticket processor

This combines:
  - rackbrain/services/ticket_processor.py (core processing: required marker bypasses, precheck, cinder verification,
    timers/suppression, same-failure gating, SLT/TestView hooks, comment rendering, etc.)
  - your ticket_processor_main.py (REAL-mode workflow: assign->transition->comment, optional close, random reassign
    with repair-release + Tester Email routing, and silent timer stage1 behavior)

Drop-in usage:
  from rackbrain.services.ticket_processor_merged import process_ticket
"""

from __future__ import annotations

import random
import re
from copy import copy
from typing import Dict, Any, Optional, List, Tuple

from rackbrain.adapters.jira_client import JiraClient
from rackbrain.core.classification import classify_error
from rackbrain.core.context_builder import build_error_event, build_ticket
from rackbrain.core.models import Rule
from rackbrain.core.jira_extractors import extract_option_value

from rackbrain.services.command_steps import execute_command_steps
from rackbrain.services.comment_renderer import build_comment_body
from rackbrain.services.logger import get_logger, get_rule_match_history_logger
from rackbrain.services.timer_store import TimerStore
from rackbrain.services.testview_actions import (
    maybe_start_slt_for_action,
    populate_testview_log_for_action,
    select_testview_case_template,
)

from rackbrain.integrations.cinder_verification import (
    build_cinder_verification_report,
    CinderVerificationError,
)
from rackbrain.integrations.precheck import (
    populate_precheck_context,
    summary_has_precheck_marker,
)

# Default max SLT attempts gate.
DEFAULT_MAX_SLT_ATTEMPTS = 15


# -----------------------------------------------------------------------------
# Helpers (merged)
# -----------------------------------------------------------------------------
def _safe_int(value, default=None):
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return int(value)
    except Exception:
        return default


def _is_cinder_verification_ticket(issue: Dict[str, Any]) -> bool:
    fields = issue.get("fields", {}) or {}
    summary = str(fields.get("summary") or "").strip().lower()

    # These tickets are often created manually (not by EVE BOT), so they need to
    # bypass processing.required_combined_text_contains.
    if "cinder verification" not in summary or "refurb" not in summary:
        return False

    location = extract_option_value(fields.get("customfield_15143"))  # Location
    customer = extract_option_value(fields.get("customfield_15119"))  # Customer

    location_ok = str(location or "").strip().lower() == "fremont"
    customer_ok = "woody (outpost)" in str(customer or "").strip().lower()
    return bool(location_ok and customer_ok)


def _is_precheck_ticket(issue: Dict[str, Any]) -> bool:
    fields = issue.get("fields", {}) or {}
    summary = str(fields.get("summary") or "")
    return summary_has_precheck_marker(summary)


def _find_transition_id(
    transitions: List[Dict[str, Any]], target_name: str
) -> Tuple[Optional[str], Optional[str]]:
    want = (target_name or "").strip().lower()
    for t in transitions or []:
        name = (t.get("name") or "").strip()
        if name.lower() == want:
            return (t.get("id"), name)
    return (None, None)


def _try_transition_by_name(
    jira: JiraClient,
    issue_key: str,
    target_name: str,
    *,
    comment_body: Optional[str] = None,
    fields: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    transitions = jira.get_transitions(issue_key)
    tid, tname = _find_transition_id(transitions, target_name)
    if not tid:
        return (False, f"NOT_FOUND: {target_name}")
    jira.do_transition(issue_key, tid, comment_body=comment_body, fields=fields)
    return (True, tname or target_name)


def _maybe_close_issue(
    jira: JiraClient,
    issue_key: str,
    *,
    transition_comment: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Try to close the ticket using an available transition.

    IMPORTANT for this Jira instance:
      - Do NOT attempt to set fields.resolution in transition payload.
        (It fails with: "Field 'resolution' cannot be set...")
    """
    transitions = jira.get_transitions(issue_key) or []
    names = [((t or {}).get("name") or "").strip() for t in transitions]
    names = [n for n in names if n]

    print(f"[DEBUG] Available transitions for {issue_key}: {names}")

    prefer_order = []
    for n in ["Closed", "Close", "Resolve", "Resolved", "Done", "Complete", "Completed"]:
        if any(x.lower() == n.lower() for x in names):
            prefer_order.append(n)
    if not prefer_order:
        prefer_order = list(names)

    if not (transition_comment or "").strip():
        transition_comment = "RackBrain auto-close: SLT has been started; closing ticket."

    errors: List[str] = []

    # 1) Preferred transitions
    for name in prefer_order:
        try:
            ok, msg = _try_transition_by_name(
                jira, issue_key, name, comment_body=transition_comment, fields=None
            )
            if ok:
                return True, f"closed_via:{msg}"
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    # 2) Heuristic close-ish names
    for n in names:
        nl = n.lower()
        if any(k in nl for k in ["close", "closed", "resolve", "resolved", "done", "complete", "completed"]):
            if any(x.lower() == n.lower() for x in prefer_order):
                continue
            try:
                ok, msg = _try_transition_by_name(
                    jira, issue_key, n, comment_body=transition_comment, fields=None
                )
                if ok:
                    return True, f"closed_via:{msg}"
            except Exception as exc:
                errors.append(f"{n}: {exc}")

    if errors:
        return False, "CLOSE_FAILED; errors=" + " | ".join(errors[:5])
    return False, "NO_CLOSE_TRANSITION_FOUND_OR_FAILED"


def _should_force_to_repair_release_pool(text: str) -> bool:
    """
    Trigger condition (case-insensitive):
      - Any text indicates "repair release" / "release from repair" / "release and retest".
    """
    body = (text or "").strip()
    if not body:
        return False

    norm = re.sub(r"\s+", " ", body).lower()

    patterns = [
        r"\bplease\s+release\s+the\s+server\b",
        r"\brelease\s+from\s+repair\b",
        r"\bplease\s+release\s+from\s+repair\b",
        r"\brelease\b.*\brepair\b",
        r"\brelease\b.*\bretest\b",
    ]
    return any(re.search(p, norm, flags=re.IGNORECASE) for p in patterns)


_EMAIL_RE = re.compile(
    r"(?P<email>[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    re.IGNORECASE,
)


def _extract_tester_email_from_description(description: str) -> Optional[str]:
    """
    Parse Jira Description and extract the email after a line like:

      Tester Email: someone@hyvesolutions.com
    """
    text = (description or "")
    if not text.strip():
        return None

    m = re.search(r"(?im)^[\s>*\-]*tester\s*email\s*:\s*(.+?)\s*$", text)
    if not m:
        return None

    tail = (m.group(1) or "").strip()
    if not tail:
        return None

    em = _EMAIL_RE.search(tail)
    if not em:
        return None

    return em.group("email").strip().lower()


def _normalize_email(e: str) -> str:
    return (e or "").strip().lower()


def _pick_final_assignee(
    *,
    combined_text_for_force: str,
    ticket_description: str,
    myself: str,
    repair_release_assignees: List[str],
    random_assignees: List[str],
) -> Tuple[str, str]:
    """
    Decide final assignee and return (assignee_email, reason).

    Priority:
      1) If combined text triggers repair-release -> pick random from repair_release_assignees
      2) Else, if Description contains "Tester Email:" and that email is in random_assignees -> assign back to it
      3) Else random pick from random_assignees (excluding myself when possible)
    """
    if _should_force_to_repair_release_pool(combined_text_for_force):
        if repair_release_assignees:
            pool = [
                x for x in repair_release_assignees
                if _normalize_email(x) != _normalize_email(myself)
            ]
            pick = random.choice(pool) if pool else random.choice(repair_release_assignees)
            return pick, "forced_repair_release_random_pool"
        return myself, "forced_repair_release_pool_empty_fallback_myself"

    tester_email = _extract_tester_email_from_description(ticket_description)
    if tester_email:
        pool_norm = {_normalize_email(x): x for x in (random_assignees or [])}
        hit = pool_norm.get(_normalize_email(tester_email))
        if hit:
            return hit, "matched_tester_email_in_description"

    if not random_assignees:
        return myself, "fallback_no_random_pool"

    pool = [x for x in random_assignees if _normalize_email(x) != _normalize_email(myself)]
    if pool:
        return random.choice(pool), "random_pool_excluding_myself"
    return random.choice(random_assignees), "random_pool_including_myself"


def _build_comments_text_from_issue(issue: Dict[str, Any]) -> str:
    fields = (issue or {}).get("fields") or {}
    comment_field = fields.get("comment") or {}
    comments = []
    if isinstance(comment_field, dict):
        comments = comment_field.get("comments") or []

    bodies: List[str] = []
    for c in comments:
        body = (c or {}).get("body") or ""
        if body and str(body).strip():
            bodies.append(str(body))
    return "\n\n".join(bodies)


# -----------------------------------------------------------------------------
# Main entrypoint
# -----------------------------------------------------------------------------
def process_ticket(
    jira: JiraClient,
    rules: List[Rule],
    issue_key: str,
    dry_run: bool = True,
    skip_commands: bool = False,
    processing_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    DRY RUN:
      - does not modify Jira; prints suggested comment

    REAL MODE:
      - Workflow from your main:
          1) Assign to myself
          2) Transition to "In Progress"
          3) Post comment (unless silent_wait)
          4) If action.close == True: close ticket (only if SLT start succeeded when action.start_slt=True)
          5) Else final assign:
              - if action.reassign_to is set in rule: honor it
              - else if combined text indicates repair-release -> random from REPAIR_RELEASE_ASSIGNEES
              - else if Description has 'Tester Email:' matching random list -> assign back
              - else random from RANDOM_ASSIGNEES
        + silent_wait: if timer_seconds is set AND comment_body is empty/whitespace,
          do NOT comment and do NOT final reassign (hold ownership until stage2).
    """
    max_slt_attempts = DEFAULT_MAX_SLT_ATTEMPTS
    if processing_config and processing_config.get("max_slt_attempts") is not None:
        max_slt_attempts = int(processing_config["max_slt_attempts"])

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
            "attachment",
            "customfield_15119",  # Customer (EVE)
            "customfield_15143",  # Location (Fremont)
        ],
    )

    # Jira may truncate comments in the issue payload; fetch the last comment when needed
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
    is_precheck_ticket = _is_precheck_ticket(issue)

    # required marker filter (bypass for cinder & precheck tickets)
    required_text = None
    if processing_config:
        required_text = processing_config.get("required_combined_text_contains")

    if required_text and (not is_cinder_ticket) and (not is_precheck_ticket):
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

    # allowed statuses
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
        return {
            "issue_key": issue_key,
            "match": False,
            "actions_taken": {"action": "skipped_unprocessable_status", "status": status_name},
            "dry_run": dry_run,
            "edited": False,
        }

    # precheck enrichment (only for PRE* marker tickets in Open)
    if is_precheck_ticket and str(status_name).strip() == "Open":
        try:
            populate_precheck_context(error_event=error_event, jira=jira)
            if getattr(error_event, "precheck_latest_comment_is_pass", False):
                print(
                    f"[INFO] Ticket {issue_key} already has latest comment 'Pass'. "
                    "Skipping precheck processing."
                )
                return {
                    "issue_key": issue_key,
                    "match": False,
                    "actions_taken": {"action": "skipped_precheck_already_pass"},
                    "dry_run": dry_run,
                    "edited": False,
                }
        except Exception as exc:
            print(f"[WARN] Precheck enrichment failed for {issue_key}: {exc}")

    # timers + suppression
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

    # expose expired timers
    try:
        error_event.timer_expired_for = timer_store.list_expired_rule_ids(issue_key, current_rearm_key)
    except Exception:
        error_event.timer_expired_for = []

    eligible_rules = [
        r
        for r in rules
        if not timer_store.is_rule_suppressed(issue_key, r.id, current_rearm_key)
    ]

    # same-failure gating
    same_fail = getattr(error_event, "db_same_failure_count", 0) or 0
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

    # debug prints (keep)
    print("[DEBUG] testcase:", error_event.testcase)
    print("[DEBUG] failure_message:", error_event.failure_message)
    print("[DEBUG] failed_testset:", error_event.failed_testset)
    print("[DEBUG] model:", error_event.model)
    print("[DEBUG] jira_slt_attempts:", error_event.jira_slt_attempts)
    print("[DEBUG] jira_latest_comment_author:", getattr(error_event, "jira_latest_comment_author", None))
    latest_comment_preview = (getattr(error_event, "jira_latest_comment_text", "") or "")[:200]
    print("[DEBUG] jira_latest_comment_text (preview):", latest_comment_preview)

    # high SLT attempts gating
    slt_attempts = _safe_int(getattr(error_event, "jira_slt_attempts", None))
    if slt_attempts is not None and slt_attempts > max_slt_attempts:
        override_rules = [
            r for r in eligible_rules if getattr(r, "allow_high_slt_attempts", False)
        ]
        if not override_rules:
            print(f"[INFO] Skipping {issue_key}: jira_slt_attempts={slt_attempts} > {max_slt_attempts}")
            logger = get_logger()
            if logger:
                logger.log_processed(
                    issue_key=issue_key,
                    success=True,
                    dry_run=dry_run,
                    actions_taken={
                        "action": "skipped_high_slt_attempts",
                        "jira_slt_attempts": slt_attempts,
                        "max_slt_attempts": max_slt_attempts,
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
                    "max_slt_attempts": max_slt_attempts,
                },
            }

        eligible_rules = override_rules
        print(
            f"[INFO] jira_slt_attempts={slt_attempts} > {max_slt_attempts}; "
            f"restricting to {len(eligible_rules)} override rule(s)."
        )

    # classify
    match = classify_error(error_event, eligible_rules, min_confidence=0.75)
    if not match:
        print("[INFO] No matching rule for %s." % issue_key)
        logger = get_logger()
        if logger:
            logger.log_no_match(issue_key=issue_key, dry_run=dry_run)
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

    action = match.rule.action

    # cinder verification integration: build report used by the rule template
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

    # run command steps (diag/ilom/etc.)
    template_override, step_timer_seconds = execute_command_steps(
        error_event, action, skip_commands=skip_commands
    )

    # optional SLT start
    maybe_start_slt_for_action(error_event, action, dry_run=dry_run)

    # optional TestView log snippet
    populate_testview_log_for_action(error_event, action)

    # optional template selection based on TestView content
    tv_template_override = select_testview_case_template(error_event, action)
    if tv_template_override is not None:
        template_override = tv_template_override

    # build comment body
    comment_body = build_comment_body(match, error_event, template_override)

    timer_seconds = step_timer_seconds
    if timer_seconds is None:
        timer_seconds = getattr(action, "timer_after_seconds", None)

    # ----------------------------
    # DRY RUN
    # ----------------------------
    if dry_run:
        print("\n===== Suggested Jira comment (DRY RUN) =====\n")
        print(comment_body)
        if timer_seconds:
            print(f"\n[DRY RUN] timer_after_seconds: {int(timer_seconds)} (not persisted)")
        print("\n============================================\n")

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

    # ----------------------------
    # REAL MODE (workflow + timer)
    # ----------------------------
    # Defaults mirror your main script; override via processing_config if desired
    random_assignees_default = [
        "loit@hyvesolutions.com",
        "xiaox@hyvesolutions.com",
        "IrdeepB@hyvesolutions.com",
        "Mai.Tran@hyvesolutions.com",
        "Annchanyavy.Watcha@hyvesolutions.com",
        "octavio.lopez@hyvesolutions.com",
        "Leo.Tung@hyvesolutions.com",
        "Jocelyn.Flores@hyvesolutions.com",
        "aye.myint@hyvesolutions.com",
    ]
    repair_release_assignees_default = [
        "thaih@hyvesolutions.com",
        "Kao.Saeteurn@hyvesolutions.com",
        "JohnyS@hyvesolutions.com",
    ]
    myself_default = "austin.lin@hyvesolutions.com"

    RANDOM_ASSIGNEES = list((processing_config or {}).get("random_assignees") or random_assignees_default)
    REPAIR_RELEASE_ASSIGNEES = list((processing_config or {}).get("repair_release_assignees") or repair_release_assignees_default)
    MYSELF_ASSIGNEE = str((processing_config or {}).get("myself_assignee") or myself_default).strip()

    transition_target = str((processing_config or {}).get("transition_to") or "In Progress")

    actions_taken: Dict[str, Any] = {}

    # Build combined text for repair-release detection
    comments_text = _build_comments_text_from_issue(issue)
    combined_text_for_force = "\n\n".join([
        ticket.summary or "",
        ticket.description or "",
        comments_text or "",
        getattr(error_event, "jira_latest_comment_text", "") or "",
        comment_body or "",
    ])

    tester_email_dbg = _extract_tester_email_from_description(ticket.description or "")
    print(f"[DEBUG] tester_email_extracted: {tester_email_dbg}")

    # Silent stage1 mode: timer + empty comment => no comment + no final reassign
    silent_wait = bool(timer_seconds) and (not isinstance(comment_body, str) or not comment_body.strip())
    if silent_wait:
        print(
            f"[INFO] Silent wait mode enabled: timer_seconds={timer_seconds}, empty_comment=True "
            "(no comment, no reassign)."
        )

    # 1) Assign to myself FIRST
    try:
        jira.assign_issue(issue_key, MYSELF_ASSIGNEE)
        print(f"[OK] Assigned {issue_key} to {MYSELF_ASSIGNEE}.")
        actions_taken["assigned_to"] = MYSELF_ASSIGNEE
    except Exception as exc:
        print(f"[WARN] Failed to assign {issue_key} to {MYSELF_ASSIGNEE}: {exc}")
        actions_taken["assigned_to"] = f"FAILED: {exc}"

    # 2) Transition to target (default In Progress)
    try:
        ok, msg = _try_transition_by_name(jira, issue_key, transition_target)
        if ok:
            print(f"[OK] Transitioned {issue_key} to {msg}.")
            actions_taken["transitioned_to"] = msg
        else:
            print(f"[WARN] No '{transition_target}' transition found for {issue_key}.")
            actions_taken["transitioned_to"] = msg
    except Exception as exc:
        print(f"[WARN] Transition to '{transition_target}' failed for {issue_key}: {exc}")
        actions_taken["transitioned_to"] = f"FAILED: {exc}"

    # 3) Post comment (unless silent_wait)
    try:
        if silent_wait:
            print(f"[INFO] Silent wait: skip commenting on {issue_key}.")
            actions_taken["commented"] = False
        else:
            if isinstance(comment_body, str) and comment_body.strip():
                jira.add_comment(issue_key, comment_body)
                print(f"[OK] Comment posted to {issue_key}.")
                actions_taken["commented"] = True
            else:
                print(f"[WARN] Empty comment body; skipped commenting on {issue_key}.")
                actions_taken["commented"] = False
    except Exception as exc:
        print(f"[WARN] Failed to post comment to {issue_key}: {exc}")
        actions_taken["commented"] = False
        actions_taken["comment_error"] = str(exc)

    # 3.5) If rule requests close, close ONLY IF SLT started successfully (when action.start_slt=True)
    want_close = bool(getattr(action, "close", False))
    if want_close:
        start_status = getattr(error_event, "slt_start_status", None)
        start_ok = False
        try:
            start_ok = int(str(start_status).strip()) in (200, 201, 202)
        except Exception:
            start_ok = False

        if not start_ok and getattr(action, "start_slt", False):
            print(f"[WARN] Close requested but SLT start not successful: slt_start_status={start_status}")

            extra = (
                "RackBrain notice:\n"
                "- SLT was requested by rule but TestView start failed.\n"
                f"- slt_start_status={getattr(error_event,'slt_start_status',None)}\n"
                f"- slt_start_response={getattr(error_event,'slt_start_response','')}\n"
                "Please re-login/refresh TestView token on this host and retry.\n"
            )
            try:
                jira.add_comment(issue_key, extra)
                actions_taken["commented_slt_start_failed"] = True
            except Exception as exc:
                actions_taken["commented_slt_start_failed"] = f"FAILED: {exc}"
        else:
            try:
                closed_ok, detail = _maybe_close_issue(
                    jira,
                    issue_key,
                    transition_comment="RackBrain auto-close: SLT started per rule; closing ticket.",
                )
                actions_taken["closed"] = closed_ok
                actions_taken["closed_detail"] = detail

                if closed_ok:
                    print(f"[OK] Closed {issue_key}: {detail}")

                    # If closed, still start timer if configured (rare, but keep consistent)
                    if timer_seconds:
                        effective_assignee = MYSELF_ASSIGNEE
                        effective_status = status_name
                        transitioned = actions_taken.get("transitioned_to")
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

                    return {
                        "issue_key": issue_key,
                        "match": True,
                        "rule_id": match.rule.id,
                        "rule_name": match.rule.name,
                        "confidence": match.confidence,
                        "edited": True,
                        "dry_run": False,
                        "actions_taken": actions_taken,
                    }
                else:
                    print(f"[WARN] Close requested but failed for {issue_key}: {detail}")
            except Exception as exc:
                actions_taken["closed"] = False
                actions_taken["closed_detail"] = f"FAILED: {exc}"
                print(f"[WARN] Close requested but exception for {issue_key}: {exc}")

    # 4) Final assign LAST (unless silent_wait)
    if silent_wait:
        actions_taken["reassigned_to"] = None
        actions_taken["reassigned_to_reason"] = "skipped_reassign_silent_timer_wait"
        print(f"[INFO] Silent wait: skip final reassign for {issue_key}.")
    else:
        # If the rule explicitly requests reassign_to, honor it.
        explicit_reassign = getattr(action, "reassign_to", None)
        if isinstance(explicit_reassign, str):
            explicit_reassign = explicit_reassign.strip()

        try:
            if explicit_reassign is not None:
                # NOTE: explicit empty string means "do not reassign" (keep current)
                if explicit_reassign == "":
                    actions_taken["reassigned_to"] = ""
                    actions_taken["reassigned_to_reason"] = "rule_reassign_to_empty_keep_current"
                else:
                    jira.assign_issue(issue_key, explicit_reassign)
                    actions_taken["reassigned_to"] = explicit_reassign
                    actions_taken["reassigned_to_reason"] = "rule_reassign_to"
                    print(f"[OK] Re-assigned {issue_key} to {explicit_reassign} (rule reassign_to).")
            else:
                final_assignee, reason = _pick_final_assignee(
                    combined_text_for_force=combined_text_for_force,
                    ticket_description=(ticket.description or ""),
                    myself=MYSELF_ASSIGNEE,
                    repair_release_assignees=REPAIR_RELEASE_ASSIGNEES,
                    random_assignees=RANDOM_ASSIGNEES,
                )
                jira.assign_issue(issue_key, final_assignee)
                actions_taken["reassigned_to"] = final_assignee
                actions_taken["reassigned_to_reason"] = reason
                print(f"[OK] Re-assigned {issue_key} to {final_assignee} ({reason}).")

                if tester_email_dbg:
                    actions_taken["tester_email_in_description"] = tester_email_dbg

        except Exception as exc:
            print(f"[WARN] Failed to re-assign {issue_key}: {exc}")
            actions_taken["reassigned_to"] = f"FAILED: {exc}"

    # 5) Start timer if configured
    if timer_seconds:
        effective_assignee = getattr(error_event, "jira_assignee", None)
        effective_status = status_name

        reassigned = actions_taken.get("reassigned_to")
        assigned = actions_taken.get("assigned_to")
        transitioned = actions_taken.get("transitioned_to")

        if isinstance(reassigned, str) and reassigned and not reassigned.startswith("FAILED"):
            effective_assignee = reassigned
        elif isinstance(assigned, str) and assigned and not str(assigned).startswith("FAILED"):
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

    # log
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

    commented_ok = actions_taken.get("commented") is True
    edited = bool(
        commented_ok
        or actions_taken.get("closed")
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
