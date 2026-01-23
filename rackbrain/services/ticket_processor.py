# -*- coding: utf-8 -*-
import random
import re
from typing import Dict, Any, Optional, List, Tuple

from rackbrain.adapters.jira_client import JiraClient
from rackbrain.core.classification import classify_error
from rackbrain.core.context_builder import build_error_event, build_ticket
from rackbrain.core.models import Rule
from rackbrain.services.command_steps import execute_command_steps
from rackbrain.services.comment_renderer import build_comment_body
from rackbrain.services.logger import get_logger, get_rule_match_history_logger
from rackbrain.services.timer_store import TimerStore
from rackbrain.services.testview_actions import (
    maybe_start_slt_for_action,
    populate_testview_log_for_action,
    select_testview_case_template,
)

MAX_SLT_ATTEMPTS = 7


# ----------------------------
# Helpers
# ----------------------------
def _safe_int(value, default=None):
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return int(value)
    except Exception:
        return default


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
    """
    Try to transition issue to target_name by looking up transitions list.
    Returns (success, transition_name_or_reason).
    """
    transitions = jira.get_transitions(issue_key)
    tid, tname = _find_transition_id(transitions, target_name)
    if not tid:
        return (False, f"NOT_FOUND: {target_name}")

    # IMPORTANT:
    # Use JiraClient.do_transition so we can include comment_body and/or fields
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

    Returns (closed_ok, detail_msg)
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

    errors: List[str] = []

    if not (transition_comment or "").strip():
        transition_comment = "RackBrain auto-close: SLT has been started; closing ticket."

    # 1) Try preferred transitions WITH required comment
    for name in prefer_order:
        try:
            ok, msg = _try_transition_by_name(
                jira,
                issue_key,
                name,
                comment_body=transition_comment,
                fields=None,
            )
            if ok:
                return True, f"closed_via:{msg}"
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    # 2) Heuristic: try anything that looks like closing (still with comment)
    for n in names:
        nl = n.lower()
        if any(k in nl for k in ["close", "closed", "resolve", "resolved", "done", "complete", "completed"]):
            if any(x.lower() == n.lower() for x in prefer_order):
                continue
            try:
                ok, msg = _try_transition_by_name(
                    jira,
                    issue_key,
                    n,
                    comment_body=transition_comment,
                    fields=None,
                )
                if ok:
                    return True, f"closed_via:{msg}"
            except Exception as exc:
                errors.append(f"{n}: {exc}")

    if errors:
        return False, "CLOSE_FAILED; errors=" + " | ".join(errors[:5])

    return False, "NO_CLOSE_TRANSITION_FOUND_OR_FAILED"


def _should_force_to_thaih(text: str) -> bool:
    """
    Trigger condition (case-insensitive):
      - Any text indicates "repair release" / "release from repair" / "release and retest".
    Examples that should match:
      - "please release the server"
      - "please release from repair and retest"
      - "release from repair"
      - "release ... repair"
      - "release ... retest"
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

    More tolerant:
      - "Tester Email :" (spaces)
      - leading bullets "*", "-", ">" etc
      - extra text after email
      - mixed casing
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
    force_thaih: str,
    random_assignees: List[str],
) -> Tuple[str, str]:
    """
    Decide final assignee and return (assignee_email, reason).

    Priority:
      1) If combined text triggers forced repair-release -> force_thaih
      2) Else, if Description contains "Tester Email:" and that email is in random_assignees -> assign back to it
      3) Else random pick from random_assignees (excluding myself when possible)
    """
    if _should_force_to_thaih(combined_text_for_force):
        return force_thaih, "forced_by_comment_release_server"

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
    """
    Build a single searchable text blob from ALL comments currently present in issue fields.
    (Jira may paginate/truncate; we at least include what we have + we already try to fetch latest.)
    """
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


# ----------------------------
# Main
# ----------------------------
def process_ticket(
    jira: JiraClient,
    rules: List[Rule],
    issue_key: str,
    dry_run: bool = True,
    skip_commands: bool = False,
    processing_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Live action order (REAL MODE):
      1) Assign to myself
      2) Transition to "In Progress" (warn if not found)
      3) Post comment (if non-empty)
      4) If action.close == True: close ticket NOW and return (NO final assign)
      5) Else final assign:
           - if combined text indicates repair-release -> thaih@...
           - else if Description has 'Tester Email:' matching an assignee in the random list -> assign back to that
           - else random assign from list
    """
    issue = jira.get_issue(
        issue_key,
        fields=[
            "summary",
            "description",
            "status",
            "assignee",
            "updated",
            "comment",
            "customfield_15119",  # Customer (EVE)
            "customfield_15143",  # Location (Fremont)
        ],
    )

    # Jira may truncate comments; fetch the last comment when needed
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

    # required marker filter
    required_text = None
    if processing_config:
        required_text = processing_config.get("required_combined_text_contains")

    if required_text:
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

    # timers
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
        r for r in rules
        if not timer_store.is_rule_suppressed(issue_key, r.id, current_rearm_key)
    ]

    # same failure gate
    if same_fail >= 2:
        override_rules = [r for r in eligible_rules if getattr(r, "allow_on_same_failure", False)]
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

    # debug prints (keep yours)
    print("[DEBUG] testcase:", error_event.testcase)
    print("[DEBUG] failure_message:", error_event.failure_message)
    print("[DEBUG] failed_testset:", error_event.failed_testset)
    print("[DEBUG] model:", error_event.model)
    print("[DEBUG] jira_slt_attempts:", error_event.jira_slt_attempts)
    print("[DEBUG] jira_latest_comment_author:", getattr(error_event, "jira_latest_comment_author", None))
    latest_comment_preview = (getattr(error_event, "jira_latest_comment_text", "") or "")[:200]
    print("[DEBUG] jira_latest_comment_text (preview):", latest_comment_preview)

    # high slt attempts gate
    slt_attempts = _safe_int(getattr(error_event, "jira_slt_attempts", None))
    if slt_attempts is not None and slt_attempts > MAX_SLT_ATTEMPTS:
        override_rules = [r for r in eligible_rules if getattr(r, "allow_high_slt_attempts", False)]
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

    # command steps
    template_override, step_timer_seconds = execute_command_steps(
        error_event, action, skip_commands=skip_commands
    )

    # optional SLT start
    maybe_start_slt_for_action(error_event, action, dry_run=dry_run)

    # optional TestView log snippet
    populate_testview_log_for_action(error_event, action)

    # optional template choose
    tv_template_override = select_testview_case_template(error_event, action)
    if tv_template_override is not None:
        template_override = tv_template_override

    # build comment
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
    # REAL MODE
    # ----------------------------
    RANDOM_ASSIGNEES = [
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

    MYSELF_ASSIGNEE = "austin.lin@hyvesolutions.com"
    FORCE_REPAIR_RELEASE_ASSIGNEE = "thaih@hyvesolutions.com"

    actions_taken: Dict[str, Any] = {}

    # Build combined text for force-to-thaih detection (NOT just RackBrain comment)
    comments_text = _build_comments_text_from_issue(issue)
    combined_text_for_force = "\n\n".join([
        ticket.summary or "",
        ticket.description or "",
        comments_text or "",
        getattr(error_event, "jira_latest_comment_text", "") or "",
        comment_body or "",
    ])

    # Debug for tester email routing
    tester_email_dbg = _extract_tester_email_from_description(ticket.description or "")
    print(f"[DEBUG] tester_email_extracted: {tester_email_dbg}")

    # 1) Assign to myself FIRST
    try:
        jira.assign_issue(issue_key, MYSELF_ASSIGNEE)
        print(f"[OK] Assigned {issue_key} to {MYSELF_ASSIGNEE}.")
        actions_taken["assigned_to"] = MYSELF_ASSIGNEE
    except Exception as exc:
        print(f"[WARN] Failed to assign {issue_key} to {MYSELF_ASSIGNEE}: {exc}")
        actions_taken["assigned_to"] = f"FAILED: {exc}"

    # 2) Try transition to 'In Progress'
    try:
        ok, msg = _try_transition_by_name(jira, issue_key, "In Progress")
        if ok:
            print(f"[OK] Transitioned {issue_key} to {msg}.")
            actions_taken["transitioned_to"] = msg
        else:
            print(f"[WARN] No 'In Progress' transition found for {issue_key}.")
            actions_taken["transitioned_to"] = msg
    except Exception as exc:
        print(f"[WARN] Transition to 'In Progress' failed for {issue_key}: {exc}")
        actions_taken["transitioned_to"] = f"FAILED: {exc}"

    # 3) Post comment
    try:
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

    # 3.5) If rule requests close, close NOW and STOP (no final assign)
    want_close = bool(getattr(action, "close", False))
    if want_close:
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

        # If close requested but failed, continue to final assign

    # 4) Final assign LAST (only if not closed)
    try:
        final_assignee, reason = _pick_final_assignee(
            combined_text_for_force=combined_text_for_force,
            ticket_description=(ticket.description or ""),
            myself=MYSELF_ASSIGNEE,
            force_thaih=FORCE_REPAIR_RELEASE_ASSIGNEE,
            random_assignees=RANDOM_ASSIGNEES,
        )

        jira.assign_issue(issue_key, final_assignee)

        if reason == "forced_by_comment_release_server":
            print(f"[OK] Re-assigned {issue_key} to {final_assignee} (forced by repair-release text).")
        elif reason == "matched_tester_email_in_description":
            print(f"[OK] Re-assigned {issue_key} to {final_assignee} (matched 'Tester Email:' in Description).")
        else:
            print(f"[OK] Re-assigned {issue_key} to {final_assignee}.")

        actions_taken["reassigned_to"] = final_assignee
        actions_taken["reassigned_to_reason"] = reason

        if tester_email_dbg:
            actions_taken["tester_email_in_description"] = tester_email_dbg

    except Exception as exc:
        print(f"[WARN] Failed to re-assign {issue_key}: {exc}")
        actions_taken["reassigned_to"] = f"FAILED: {exc}"

    # timer
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

    # edited?
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
