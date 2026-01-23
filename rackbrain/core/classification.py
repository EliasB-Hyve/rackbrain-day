# rackbrain/core/classification.py

import re
from typing import Any, Dict, List, Optional

from rackbrain.core.models import ErrorEvent, Rule, RuleMatchResult
from rackbrain.core.rules_engine import pattern_matches_text


def _normalize(value: Any) -> str:
    """
    Normalize values for case-insensitive comparisons.
    """
    return str(value).strip().lower()


def scope_matches(error_event: ErrorEvent, scope: Dict[str, Any]) -> bool:
    """
    Return True if the given ErrorEvent satisfies the rule's scope.

    Supported scope formats in YAML:

      scope:
        arch: "EVE"                # exact, case-insensitive
        failed_testset:            # one-of list
          - "AC_OFF_SP"
          - "AC_ON_SP"
        model:
          contains: "L40S"         # substring match
        failure_message:
          regex: "io.pcie.*ce"     # regex match

    Notes:
      - If scope is empty, it always matches.
      - If a scope key refers to an attribute that ErrorEvent does not have,
        that scope key is ignored (backwards compatible with old rules).
      - If the attribute exists but is None, the scope check fails.
    """
    if not scope:
        return True

    for field_name, expected in scope.items():
        # Ignore unknown fields so old rules don't break
        if not hasattr(error_event, field_name):
            continue

        value = getattr(error_event, field_name)
        if value is None:
            return False

        value_items: Optional[List[str]] = None
        if isinstance(value, (list, tuple, set)):
            value_items = [str(v) for v in value if v is not None]

        # Dict form: {contains: "..."} or {regex: "..."}
        if isinstance(expected, dict):
            val_str = str(value)
            val_norm = val_str.lower()

            # contains: substring MUST be present (case-insensitive)
            if "contains" in expected:
                needle = str(expected["contains"]).lower()
                if value_items is not None:
                    if not any(needle in str(item).lower() for item in value_items):
                        return False
                else:
                    if needle not in val_norm:
                        return False

            # not_contains: substring MUST NOT be present (case-insensitive)
            if "not_contains" in expected:
                banned = str(expected["not_contains"]).lower()
                if value_items is not None:
                    if any(banned in str(item).lower() for item in value_items):
                        return False
                else:
                    if banned in val_norm:
                        return False

            # regex: pattern MUST match (case-insensitive)
            if "regex" in expected:
                pattern = str(expected["regex"])
                if value_items is not None:
                    if not any(
                        re.search(pattern, str(item), flags=re.IGNORECASE) is not None
                        for item in value_items
                    ):
                        return False
                else:
                    if re.search(pattern, val_str, flags=re.IGNORECASE) is None:
                        return False

        # List / tuple / set: any-of exact matches
        elif isinstance(expected, (list, tuple, set)):
            if value_items is not None:
                expected_norm = {_normalize(item) for item in expected}
                if not any(_normalize(v) in expected_norm for v in value_items):
                    return False
            else:
                if not any(_normalize(value) == _normalize(item) for item in expected):
                    return False

        # Plain scalar: exact, case-insensitive
        else:
            if value_items is not None:
                if not any(_normalize(v) == _normalize(expected) for v in value_items):
                    return False
            else:
                if _normalize(value) != _normalize(expected):
                    return False

    return True


def classify_error(
    error_event: ErrorEvent,
    rules: List[Rule],
    min_confidence: float = 0.5,
) -> Optional[RuleMatchResult]:
    """
    Very simple classifier:
      - First filter rules by scope.
      - For each remaining rule, count how many patterns match the combined_text.
      - Confidence = matched_patterns_count / total_patterns_count.
      - Pick the rule with highest confidence >= min_confidence.
    """
    best_result: Optional[RuleMatchResult] = None

    for rule in rules:
        # Skip rules with no patterns at all
        if not rule.patterns:
            continue

        # NEW: enforce scope first
        if not scope_matches(error_event, rule.scope):
            continue

        matched: List[Any] = []
        for p in rule.patterns:
            if pattern_matches_text(p, error_event.combined_text):
                matched.append(p)

        if not matched:
            continue

        confidence = float(len(matched)) / float(len(rule.patterns))

        if confidence < min_confidence:
            continue

        candidate = RuleMatchResult(
            rule=rule,
            confidence=confidence,
            matched_patterns=matched,
        )

        # Selection logic:
        # 1. If no best_result yet, use this candidate
        # 2. Compare by priority first (higher priority wins)
        # 3. If priorities are equal, compare by confidence (higher confidence wins)
        # 4. If both are equal, keep the existing best_result (first match wins)
        if best_result is None:
            best_result = candidate
        else:
            candidate_priority = rule.priority
            best_priority = best_result.rule.priority
            
            if candidate_priority > best_priority:
                best_result = candidate
            elif candidate_priority == best_priority:
                # Same priority: prefer higher confidence
                if candidate.confidence > best_result.confidence:
                    best_result = candidate

    return best_result
