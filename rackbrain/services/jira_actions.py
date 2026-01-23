from typing import Any, Dict, List, Optional, Tuple

from rackbrain.adapters.jira_client import JiraClient


def _build_action_context(error_event) -> Dict[str, str]:
    if error_event is None:
        return {}

    ticket = getattr(error_event, "ticket", None)
    return {
        "ticket_key": getattr(ticket, "key", "") if ticket else "",
        "sn": getattr(error_event, "sn", "") or "",
        "jira_latest_comment_author": getattr(error_event, "jira_latest_comment_author", "") or "",
        "jira_latest_comment_author_display_name": getattr(
            error_event, "jira_latest_comment_author_display_name", ""
        ) or "",
        "jira_latest_comment_author_email": getattr(
            error_event, "jira_latest_comment_author_email", ""
        ) or "",
    }


def _resolve_action_value(value: Optional[str], context: Dict[str, str]) -> Optional[str]:
    if value is None:
        return None
    if "{" not in value:
        return value
    try:
        return value.format(**context)
    except Exception:
        return value


def _resolve_issue_link_request(
    *,
    current_issue_key: str,
    link_type: str,
    target_issue_key: str,
) -> Tuple[str, str, str]:
    """
    Convert a human-facing link type into Jira's API type name + direction.

    Returns (link_type_name, inward_issue_key, outward_issue_key).
    """
    normalized = " ".join((link_type or "").strip().lower().split())

    if normalized in ("is blocked by", "blocked by"):
        # Current issue is blocked by target issue:
        # inward = current, outward = blocker/target
        return ("Blocks", current_issue_key, target_issue_key)

    if normalized in ("relates to", "relates"):
        # Relates is symmetric; we keep a consistent direction.
        return ("Relates", current_issue_key, target_issue_key)

    raise ValueError(
        "Unsupported link_issue.type: %r (supported: 'is blocked by', 'relates to')"
        % (link_type,)
    )


def apply_jira_actions(
    jira: JiraClient,
    issue_key: str,
    comment_body: str,
    processing_config: Optional[Dict[str, Any]] = None,
    action: Optional[Any] = None,
    error_event: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Apply live actions: assign -> transition -> comment -> reassign.
    """
    actions_taken: Dict[str, Any] = {}

    # Get processing config or use defaults
    assign_to = None
    reassign_to = None
    transition_to = "In Progress"
    if processing_config:
        assign_to = processing_config.get("assign_to")
        reassign_to = processing_config.get("reassign_to")
        transition_to = processing_config.get("transition_to", "In Progress")

    if action is not None:
        assign_to = action.assign_to if action.assign_to is not None else assign_to
        reassign_to = action.reassign_to if action.reassign_to is not None else reassign_to
        transition_to = action.transition_to if action.transition_to is not None else transition_to

    context = _build_action_context(error_event)
    assign_to = _resolve_action_value(assign_to, context)
    reassign_to = _resolve_action_value(reassign_to, context)
    transition_to = _resolve_action_value(transition_to, context)

    link_action = getattr(action, "link_issue", None) if action is not None else None
    link_failed = False
    if link_action is not None:
        link_type = _resolve_action_value(getattr(link_action, "type", None), context) or ""
        link_target = _resolve_action_value(getattr(link_action, "target", None), context) or ""

        if not link_type or not link_target:
            link_failed = True
            actions_taken["linked_to"] = "FAILED: link_issue requires both type and target"

    # 1) Assign to initial user (if configured)
    if assign_to:
        try:
            jira.assign_issue(issue_key, assign_to)
            print("[OK] Assigned %s to %s." % (issue_key, assign_to))
            actions_taken["assigned_to"] = assign_to
        except Exception as exc:
            print("[WARN] Failed to assign %s to %s: %s" % (issue_key, assign_to, exc))
            actions_taken["assigned_to"] = f"FAILED: {exc}"

    # 2) Transition to target status (if configured)
    if transition_to:
        try:
            def _find_transition_id(transitions: List[Dict[str, Any]], target: str) -> Tuple[Optional[str], Optional[str]]:
                want = (target or "").strip().lower()
                for t in transitions:
                    name = (t.get("name") or "").strip()
                    if name.lower() == want:
                        return t.get("id"), name
                return None, None

            transitions = jira.get_transitions(issue_key)
            transition_id, transition_name = _find_transition_id(transitions, transition_to)

            if transition_id:
                jira.transition_issue(issue_key, transition_id)
                print("[OK] Transitioned %s to %s." % (issue_key, transition_name))
                actions_taken["transitioned_to"] = transition_name
            else:
                # Some Jira workflows require an intermediate transition (commonly Open -> In Progress)
                # before certain statuses become available (e.g., "Pending Escalation").
                if transition_to.strip().lower() != "in progress":
                    in_progress_id, in_progress_name = _find_transition_id(transitions, "In Progress")
                    if in_progress_id:
                        jira.transition_issue(issue_key, in_progress_id)
                        print("[OK] Transitioned %s to %s (prereq)." % (issue_key, in_progress_name))

                        transitions = jira.get_transitions(issue_key)
                        transition_id, transition_name = _find_transition_id(transitions, transition_to)
                        if transition_id:
                            jira.transition_issue(issue_key, transition_id)
                            print("[OK] Transitioned %s to %s." % (issue_key, transition_name))
                            actions_taken["transitioned_to"] = f"{in_progress_name} -> {transition_name}"
                        else:
                            print(
                                "[WARN] No '%s' transition found for %s (even after '%s')."
                                % (transition_to, issue_key, in_progress_name)
                            )
                            actions_taken["transitioned_to"] = f"NOT_FOUND: {transition_to}"
                    else:
                        print("[WARN] No '%s' transition found for %s." % (transition_to, issue_key))
                        actions_taken["transitioned_to"] = f"NOT_FOUND: {transition_to}"
                else:
                    print("[WARN] No '%s' transition found for %s." % (transition_to, issue_key))
                    actions_taken["transitioned_to"] = f"NOT_FOUND: {transition_to}"
        except Exception as exc:
            print("[WARN] Failed to transition %s to %s: %s" % (issue_key, transition_to, exc))
            actions_taken["transitioned_to"] = f"FAILED: {exc}"

    # 2b) Create an issue link (optional)
    if link_action is not None and not link_failed:
        try:
            link_type = _resolve_action_value(getattr(link_action, "type", None), context) or ""
            link_target = _resolve_action_value(getattr(link_action, "target", None), context) or ""
            link_type_name, inward_key, outward_key = _resolve_issue_link_request(
                current_issue_key=issue_key,
                link_type=link_type,
                target_issue_key=link_target,
            )

            jira.create_issue_link(
                link_type_name=link_type_name,
                inward_issue_key=inward_key,
                outward_issue_key=outward_key,
            )
            print("[OK] Linked %s (%s) to %s." % (issue_key, link_type, link_target))
            actions_taken["linked_to"] = link_target
            actions_taken["link_type"] = link_type
        except Exception as exc:
            # Keep it non-noisy: don't post a misleading comment and don't reassign.
            print("[WARN] Failed to link %s: %s" % (issue_key, exc))
            actions_taken["linked_to"] = f"FAILED: {exc}"
            actions_taken["link_type"] = getattr(link_action, "type", "")
            link_failed = True

    # 3) Post the RackBrain comment (skip empty body)
    # If linking failed, skip comment so the rule can retry next cycle.
    if (not link_failed) and isinstance(comment_body, str) and comment_body.strip():
        try:
            jira.add_comment(issue_key, comment_body)
            print("[OK] Comment posted to %s." % issue_key)
            actions_taken["commented"] = True
        except Exception as exc:
            print("[WARN] Failed to post comment to %s: %s" % (issue_key, exc))
            actions_taken["commented"] = f"FAILED: {exc}"
    else:
        actions_taken["commented"] = False

    # 4) Reassign to final user (if configured)
    # If linking failed, keep the ticket visible to the current user for retry/debug.
    if (not link_failed) and reassign_to:
        try:
            jira.assign_issue(issue_key, reassign_to)
            print("[OK] Re-assigned %s to %s." % (issue_key, reassign_to))
            actions_taken["reassigned_to"] = reassign_to
        except Exception as exc:
            print("[WARN] Failed to re-assign %s to %s: %s" % (issue_key, reassign_to, exc))
            actions_taken["reassigned_to"] = f"FAILED: {exc}"

    return actions_taken
