# rackbrain/core/models.py

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
from dataclasses import field


@dataclass
class Ticket:
    """
    Clean representation of a Jira issue we care about.
    """
    key: str
    summary: str
    description: str
    raw: Dict[str, Any]  # full Jira JSON if we need extra fields later

@dataclass
class IlomProblem:
    component: str
    description: str  # full multi-line description

@dataclass
class CommandResult:
    """
    Result of a single EVE command execution.
    Used for command history tracking.
    """
    cmd_id: str  # Unique identifier for this command (e.g., "cmd_1", "ilom_check")
    context: str  # "diag", "ilom", etc.
    cmd: str  # actual command string after context
    status: int  # exit code
    stdout: str  # full stdout (may be truncated)
    stderr: str  # stderr
    selected_lines: Optional[str] = None  # selected subset of stdout
    
@dataclass
class ErrorEvent:
    """
    Combined view of the ticket + all other info we know (DB/SFCS/etc).
    """
    ticket: Ticket
    sn: Optional[str]
    combined_text: str

    # Derived from Jira summary/description
    arch: Optional[str] = None          # "EVE", "HOPPER", etc.
    testcase: Optional[str] = None      # e.g. "4_CHECK_ILOM_FAULT"
    error_details: Optional[str] = None # Failure Message block parsed from description

    # From hyvetest DB (via hyvetest_client)
    model: Optional[str] = None
    customer_ipn: Optional[str] = None
    rack_sn: Optional[str] = None
    slt_rack_sn: Optional[str] = None
    server_status_id: Optional[int] = None
    failure_message: Optional[str] = None
    failed_testset: Optional[str] = None
    server_error_detail: Optional[str] = None
    tester_email: Optional[str] = None
    ilom_problems: List[IlomProblem] = field(default_factory=list)
    ilom_open_problems_raw: Optional[str] = None  # NEW: full show System/Open_Problems output

    evbot_version: Optional[str] = None
    jira_server_status_id: Optional[str] = None
    jira_server_ok: Optional[str] = None
    jira_slt_attempts: Optional[str] = None
    jira_model: Optional[str] = None
    jira_customer_ipn: Optional[str] = None
    jira_slt_rack_sn: Optional[str] = None
    jira_tm2_version: Optional[str] = None
    jira_tester_email: Optional[str] = None
    jira_test_started: Optional[str] = None         # raw text
    jira_test_finished: Optional[str] = None        # raw text
    jira_test_duration_minutes: Optional[float] = None
    jira_status: Optional[str] = None
    jira_updated: Optional[str] = None
    jira_assignee: Optional[str] = None

    last_cmd_context: Optional[str] = None   # "diag", "ilom", etc.
    last_cmd: Optional[str] = None           # actual command string after context
    last_cmd_status: Optional[int] = None
    last_cmd_stdout: Optional[str] = None    # maybe truncated when set
    last_cmd_selected_lines: Optional[str] = None

    # Command history: track all commands executed (not just the last one)
    command_history: List[CommandResult] = field(default_factory=list)

    jira_customer: Optional[str] = None
    jira_location: Optional[str] = None

        # ==== NEW: TestView / SLT helpers (from hyvetest + TestViewlog) ====
    # These are derived from hyvetest/TestView, not Jira text.
    db_failed_testcase: Optional[str] = None          # raw testErrorCode string
    db_failed_testcase_list: List[str] = field(default_factory=list)  # split list
    db_same_failure_count: Optional[int] = None       # how many same failures in a row
    db_latest_slt_id: Optional[int] = None            # latest ServerStatus.id (slt_id)
    db_latest_failed_testset: Optional[str] = None    # latest associatedTestSetName

    testview_log_text: Optional[str] = None
    testview_log_snippet: Optional[str] = None
    testview_log_error: Optional[str] = None
    telnet_cmd: Optional[str] = None
    jira_comments_text: Optional[str] = None
    jira_latest_comment_text: Optional[str] = None
    jira_latest_comment_author: Optional[str] = None
    jira_latest_comment_author_display_name: Optional[str] = None
    jira_latest_comment_author_email: Optional[str] = None

    # Internal timers (local state, not Jira):
    # Rule ids whose timers have expired for this ticket under the current rearm_key.
    timer_expired_for: List[str] = field(default_factory=list)

    slt_validate_status: Optional[int] = None
    slt_validate_response: Optional[str] = None
    slt_start_status: Optional[int] = None
    slt_start_response: Optional[str] = None

        # NEW: Conditional TestView start request (set by command step pass/fail)
    testview_start_requested: bool = False
    testview_start_operation: Optional[str] = None
    testview_start_use_validate: bool = True



@dataclass
class RulePattern:
    """
    A single pattern inside a rule.
    type: "contains" or "regex"
    value: the string or regex pattern to match.
    """
    type: str
    value: str


@dataclass
class RuleCommandStep:
    """
    One EVE command to run as part of an action.

    Fields map directly from YAML.
    """
    cmd: str  # e.g. "{diag} hwdiag io config"
    # Optional expectations:
    expect_status: Optional[int] = None        # e.g. 0s
    expect_contains: Optional[Union[str, List[str]]] = None      # string(s) must appear in stdout
    expect_not_contains: Optional[Union[str, List[str]]] = None  # string(s) must NOT appear

    # Comment templates / branching:
    on_expect_fail_comment: Optional[str] = None  # used if any expectation fails
    on_expect_pass_comment: Optional[str] = None  # optional immediate success comment

    stop_on_decision: bool = True  # if a comment is selected here, stop further steps

    # Optional unique identifier for this command (for template placeholders)
    # If not provided, auto-generated as "cmd_1", "cmd_2", etc.
    id: Optional[str] = None
    timer_after_seconds: Optional[int] = None
    for_each_extract: Optional[str] = None

    line_contains: Optional[str] = None          # substring to match on a line
    line_not_contains: Optional[str] = None      # substring that must NOT appear in selected lines
    line_before: int = 0                         # include N lines before each match
    line_after: int = 0
    line_only: bool = False                      # If True, return ONLY matching lines (no context)
    line_between_start_contains: Optional[str] = None  # extract text between markers on same line
    line_between_end_contains: Optional[str] = None
    line_after_contains: Optional[str] = None    # extract N chars after marker on same line
    line_after_chars: int = 0

    between_start_contains: Optional[str] = None # first line that contains this
    between_end_contains: Optional[str] = None   # first line that contains this

    # Conditional execution: only run this step if previous command output contains this
    if_previous_contains: Optional[str] = None  # skip if previous stdout doesn't contain this

        # NEW: Conditional TestView start (triggered by expect pass/fail)
    start_testview_on_pass: bool = False
    start_testview_on_fail: bool = False
    testview_operation_on_pass: str = "SLT"     # "SLT" or "PRETEST"
    testview_operation_on_fail: str = "SLT"
    testview_use_validate_on_pass: bool = True
    testview_use_validate_on_fail: bool = True


@dataclass
class RuleIssueLinkAction:
    """
    Create a Jira issue link to another issue key.

    `type` is a human-facing relationship string, e.g.:
      - "is blocked by"
      - "relates to"
    `target` is the issue key to link to, e.g. "PRODISS-15074".
    """

    type: str
    target: str


@dataclass
class RuleAction:
    """
    What we do if this rule matches.
    """
    type: str = "comment_only"
    close: bool = False
    comment_template: str = ""
    ilom_filter_contains: Optional[List[str]] = None
    assign_to: Optional[str] = None
    reassign_to: Optional[str] = None
    transition_to: Optional[str] = None
    timer_after_seconds: Optional[int] = None
    # NEW: optional command sequence
    command_steps: Optional[List[RuleCommandStep]] = None
    text_extracts: Optional[List[Dict[str, Any]]] = None
    failure_message_line_contains: Optional[str] = None
    failure_message_line_before: int = 0
    failure_message_line_after: int = 0
    failure_message_line_between_start_contains: Optional[str] = None
    failure_message_line_between_end_contains: Optional[str] = None
    failure_message_line_after_contains: Optional[str] = None
    failure_message_line_after_chars: int = 0
    failure_message_between_start_contains: Optional[str] = None
    failure_message_between_end_contains: Optional[str] = None

     # NEW: TestView log selection (for use in comments)
    # If testview_testcase_contains is set, we will:
    #   - find the latest SLT run for this SN
    #   - choose the testcase whose name contains this substring
    #   - download its TestView log
    #   - slice the log using the selectors below
    testview_testcase_contains: Optional[str] = None
    testview_testset: Optional[str] = None  # optional override (else use latest failed_testset)

    testview_line_contains: Optional[str] = None
    testview_line_before: int = 0
    testview_line_after: int = 0
    testview_line_between_start_contains: Optional[str] = None
    testview_line_between_end_contains: Optional[str] = None
    testview_line_after_contains: Optional[str] = None
    testview_line_after_chars: int = 0
    testview_between_start_contains: Optional[str] = None
    testview_between_end_contains: Optional[str] = None
        # NEW: post-filter

    testview_filter_line_contains: Optional[str] = None
     # NEW: SLT auto-start config (runs from cmd101-sr1, not RAMSES)
    start_slt: bool = False             # if true, call TestView SLT API
    slt_operation: str = "SLT"          # e.g. "SLT" (default)
    slt_use_validate: bool = True       # run validate_server first
    link_issue: Optional[RuleIssueLinkAction] = None

    # NEW: nested TestView configuration (backwards compatible with existing testview_* keys)
    # Intended YAML shape:
    #   action:
    #     testview:
    #       testcase:
    #         contains: "5_PROGRAM_SYSTEM_RECORD"
    #       testset: "RESET_FACTORY"   # optional
    #       select:                  # optional snippet selection
    #         between_start_contains: "Error:"
    #         between_end_contains: "End"
    #       cases:                   # optional: ordered, first match wins
    #         - when:
    #             contains: "Some string"
    #             source: "log_snippet"  # or: "log_text"
    #           comment_template: |
    #             Comment when matched
    testview: Optional[Dict[str, Any]] = None


@dataclass
class Rule:
    """
    A fully parsed rule from YAML.
    """
    id: str
    name: str
    description: str
    scope: Dict[str, Any]          # e.g. {"test_phase": "SLT"} (not used yet)
    patterns: List[RulePattern]
    action: RuleAction
    priority: int = 0  # Higher priority rules win when multiple match (default: 0)
    allow_on_same_failure: bool = False
    allow_high_slt_attempts: bool = False  # allow running when jira_slt_attempts exceeds MAX_SLT_ATTEMPTS


@dataclass
class RuleMatchResult:
    """
    Result of classifying an ErrorEvent with a Rule.
    """
    rule: Rule
    confidence: float
    matched_patterns: List[RulePattern]


from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


