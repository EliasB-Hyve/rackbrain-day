# rackbrain/core/rules_engine.py

import os
import re
from pathlib import Path
from typing import List

import yaml

from rackbrain.core.models import Rule, RuleAction, RulePattern, RuleCommandStep, RuleIssueLinkAction

def _load_rule_from_dict(data: dict) -> Rule:
    patterns = [
        RulePattern(type=p["type"], value=p["value"])
        for p in data.get("patterns", [])
    ]

    action_data = data.get("action", {}) or {}
    link_issue_data = action_data.get("link_issue") or None
    link_issue = None
    if link_issue_data is not None:
        link_type = str(link_issue_data.get("type") or "").strip()
        link_target = str(link_issue_data.get("target") or "").strip()
        if not link_type or not link_target:
            raise ValueError(
                f"Rule {data.get('id', '<unknown>')}: action.link_issue requires both 'type' and 'target'"
            )
        link_issue = RuleIssueLinkAction(type=link_type, target=link_target)
    steps_data = action_data.get("command_steps") or []
    command_steps = []
    for idx, step in enumerate(steps_data, start=1):
        # Auto-generate ID if not provided
        cmd_id = step.get("id") or f"cmd_{idx}"
        command_steps.append(
            RuleCommandStep(
                id=cmd_id,
                cmd=step["cmd"],
                timer_after_seconds=step.get("timer_after_seconds"),
                for_each_extract=step.get("for_each_extract"),
                expect_status=step.get("expect_status"),
                expect_contains=step.get("expect_contains"),
                expect_not_contains=step.get("expect_not_contains"),
                on_expect_fail_comment=step.get("on_expect_fail_comment"),
                on_expect_pass_comment=step.get("on_expect_pass_comment"),
                stop_on_decision=step.get("stop_on_decision", True),

                line_contains=step.get("line_contains"),
                line_not_contains=step.get("line_not_contains"),
                line_before=step.get("line_before", 0),
                line_after=step.get("line_after", 0),
                line_only=step.get("line_only", False),
                line_between_start_contains=step.get("line_between_start_contains"),
                line_between_end_contains=step.get("line_between_end_contains"),
                line_after_contains=step.get("line_after_contains"),
                line_after_chars=step.get("line_after_chars", 0),
                between_start_contains=step.get("between_start_contains"),
                between_end_contains=step.get("between_end_contains"),
                if_previous_contains=step.get("if_previous_contains"),
                start_testview_on_pass=step.get("start_testview_on_pass", False),
                start_testview_on_fail=step.get("start_testview_on_fail", False),
                testview_operation_on_pass=step.get("testview_operation_on_pass", "SLT"),
                testview_operation_on_fail=step.get("testview_operation_on_fail", "SLT"),
                testview_use_validate_on_pass=step.get("testview_use_validate_on_pass", True),
                testview_use_validate_on_fail=step.get("testview_use_validate_on_fail", True),

            )
        )
    
    action = RuleAction(
        type=action_data.get("type", "comment_only"),
        close=action_data.get("close", False),
        comment_template=action_data.get("comment_template", ""),
        ilom_filter_contains=action_data.get("ilom_filter_contains"),
        timer_after_seconds=action_data.get("timer_after_seconds"),
        command_steps=command_steps or None,
        text_extracts=action_data.get("text_extracts"),
        assign_to=action_data.get("assign_to"),
        reassign_to=action_data.get("reassign_to"),
        transition_to=action_data.get("transition_to"),
        link_issue=link_issue,
        testview=action_data.get("testview"),

        # NEW: plumb failure_message_* from YAML into RuleAction
        failure_message_line_contains=action_data.get("failure_message_line_contains"),
        failure_message_line_before=action_data.get("failure_message_line_before", 0),
        failure_message_line_after=action_data.get("failure_message_line_after", 0),
        failure_message_line_between_start_contains=action_data.get(
            "failure_message_line_between_start_contains"
        ),
        failure_message_line_between_end_contains=action_data.get(
            "failure_message_line_between_end_contains"
        ),
        failure_message_line_after_contains=action_data.get(
            "failure_message_line_after_contains"
        ),
        failure_message_line_after_chars=action_data.get(
            "failure_message_line_after_chars", 0
        ),
        failure_message_between_start_contains=action_data.get(
            "failure_message_between_start_contains"
        ),
        failure_message_between_end_contains=action_data.get(
            "failure_message_between_end_contains"
        ),

         # NEW: TestView log selectors
        testview_testcase_contains=action_data.get("testview_testcase_contains"),
        testview_testset=action_data.get("testview_testset"),
        testview_line_contains=action_data.get("testview_line_contains"),
        testview_line_before=action_data.get("testview_line_before", 0),
        testview_line_after=action_data.get("testview_line_after", 0),
        testview_line_between_start_contains=action_data.get(
            "testview_line_between_start_contains"
        ),
        testview_line_between_end_contains=action_data.get(
            "testview_line_between_end_contains"
        ),
        testview_line_after_contains=action_data.get("testview_line_after_contains"),
        testview_line_after_chars=action_data.get("testview_line_after_chars", 0),
        testview_between_start_contains=action_data.get(
            "testview_between_start_contains"
        ),
        testview_between_end_contains=action_data.get(
            "testview_between_end_contains"
        ),
         testview_filter_line_contains=action_data.get(
            "testview_filter_line_contains"
        ),

                # NEW: TestView start controls
        start_slt=action_data.get("start_slt", False),
        slt_operation=action_data.get("slt_operation", "SLT"),
        slt_use_validate=action_data.get("slt_use_validate", True),

    )


    return Rule(
        id=data["id"],
        name=data.get("name", data["id"]),
        description=data.get("description", ""),
        scope=data.get("scope", {}) or {},
        patterns=patterns,
        action=action,
        priority=data.get("priority", 0),  # Default priority is 0
        allow_on_same_failure=bool(data.get("allow_on_same_failure", False)),  # NEW
        allow_high_slt_attempts=bool(data.get("allow_high_slt_attempts", False)),

    )



def load_rules_from_files(rule_files: List[str]) -> List[Rule]:
    """
    Load all rules from the given YAML files.

    Each file must contain a YAML list of rule dicts.
    Paths can be relative to the current working directory.
    """
    rules: List[Rule] = []

    for path_str in rule_files:
        path = Path(path_str)
        if not path.is_absolute():
            path = Path(os.getcwd()) / path

        if not path.exists():
            raise FileNotFoundError(f"Rule file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []

        if not isinstance(data, list):
            raise ValueError(f"Rule file {path} must contain a YAML list of rules.")

        for rule_dict in data:
            rules.append(_load_rule_from_dict(rule_dict))

    return rules


def pattern_matches_text(pattern: RulePattern, text: str) -> bool:
    """
    Check whether a single pattern matches the given text.
    """
    haystack = text or ""

    if pattern.type == "contains":
        return pattern.value.lower() in haystack.lower()

    if pattern.type == "not_contains":
        return pattern.value.lower() not in haystack.lower()

    if pattern.type == "regex":
        return re.search(pattern.value, haystack, flags=re.IGNORECASE) is not None

    # Unknown pattern type: treat as no match
    return False
