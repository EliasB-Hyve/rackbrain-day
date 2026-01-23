# RackBrain rules reference

This document summarizes the YAML rule format used in `config/rules/*.yaml`.

## Rule shape

Each rule is a mapping with these common keys:

```yaml
- id: dimm_retry
  name: DIMM retry workflow
  description: Optional free-form description.
  priority: 10
  allow_on_same_failure: false
  allow_high_slt_attempts: false
  scope: {}
  patterns:
    - type: contains
      value: "DIMM"
  action:
    comment_template: |
      Example comment for {ticket_key}
```

## Matching logic

- `patterns` are evaluated against the combined Jira summary + description.
- Rules can also define a `scope` to limit matches using structured fields (for example, model,
  arch, failed_testset, failure_message, jira_location).
- When multiple rules match, RackBrain selects the highest `priority`. If priorities are equal,
  it chooses the higher confidence (pattern match ratio).
- RackBrain currently requires a minimum confidence of `0.5` (hardcoded default).

### Pattern types

Supported `patterns[*].type` values:

- `contains`: case-insensitive substring match.
- `not_contains`: case-insensitive negative match.
- `regex`: case-insensitive regex.

### Scope matching

Scope values can be:

- Scalars (exact match, case-insensitive).
- Lists (any match).
- Dicts with `contains`, `not_contains`, or `regex` keys.

Example:

```yaml
scope:
  arch: "EVE"
  failed_testset:
    - "AC_OFF_SP"
    - "AC_ON_SP"
  model:
    contains: "L40S"
  failure_message:
    regex: "io.pcie.*ce"
```

Notes:

- Scope keys are matched against fields on `ErrorEvent` (see `rackbrain/core/models.py`).
- Unknown scope keys are ignored (so older rules don’t break when fields change).

### Repeated failures and SLT attempts

- `allow_on_same_failure`: opt-in when the same failure repeats (requires hyvetest/TestView data).
- `allow_high_slt_attempts`: opt-in when Jira's `slt attempts` exceeds the default threshold.

## Action settings

The `action` block controls Jira updates and optional integrations.

### Core fields

```yaml
action:
  comment_template: |
    Comment text. Use {sn}, {rule_id}, {jira_location}, etc.
  assign_to: "currentUser()"
  reassign_to: "someone@example.com"
  transition_to: "In Progress"
  transition_comment_mode: "during_transition"
  timer_after_seconds: 3600
  link_issue:
    type: "is blocked by"
    target: "PRODISS-1234"
```

Notes:

- `transition_comment_mode: during_transition` posts the comment as part of the transition.
- `timer_after_seconds` starts a timer that suppresses all rules until it expires.
- `link_issue.type` supports `is blocked by` and `relates to`.

### Comment templates and placeholders

Comment templates use Python `str.format()` placeholders. Common placeholders include:

- Ticket fields: `{ticket_key}`, `{sn}`, `{jira_location}`, `{jira_customer}`, `{jira_reporter}`.
- Rule fields: `{rule_id}`, `{rule_name}`, `{confidence}`.
- Command output: `{last_cmd_stdout}`, `{last_cmd_selected_lines}`, `{all_commands_code}`.
- TestView fields: `{testview_log_snippet}`, `{db_latest_slt_id}`.

RackBrain also exposes per-command placeholders for each executed command step. If a step has
`id: ilom_check`, templates can use:

- `{command_ilom_check_stdout}`, `{command_ilom_check_stdout_code}`
- `{command_ilom_check_selected_lines}`, `{command_ilom_check_selected_lines_code}`
- `{command_ilom_check_stderr}`, `{command_ilom_check_stderr_code}`
- `{command_ilom_check_status}`, `{command_ilom_check_cmd}`, `{command_ilom_check_context}`

If a step omits `id`, RackBrain auto-generates IDs like `cmd_1`, `cmd_2`, etc.

### Failure-message selection

Some workflows need a stable, small excerpt from `failure_message` without writing custom regexes.
Rule actions support selection keys that populate:

- `{failure_message_selected}` (raw text)
- `{failure_message_selected_code}` (Jira `{code}` block)

Supported selectors (all optional):

- `failure_message_line_contains`, `failure_message_line_before`, `failure_message_line_after`
- `failure_message_line_between_start_contains`, `failure_message_line_between_end_contains`
- `failure_message_line_after_contains`, `failure_message_line_after_chars`
- `failure_message_between_start_contains`, `failure_message_between_end_contains`

## Command steps

Use `action.command_steps` to run EVE commands before rendering comments.

```yaml
action:
  command_steps:
    - id: ilom_check
      cmd: "{ilom} show System/Open_Problems"
      expect_status: 0
      expect_contains: "Open Problems"
      on_expect_fail_comment: |
        ILOM check failed. {command_ilom_check_stderr_code}
```

Supported fields include:

- `cmd`: command string (supports `{sn}`, `{ticket_key}`, and `{telnet_cmd}`).
- `expect_status`, `expect_contains`, `expect_not_contains`: gating conditions.
- `on_expect_pass_comment`, `on_expect_fail_comment`: optional template overrides.
- `timer_after_seconds`: start a timer when the step succeeds.
- `for_each_extract`: run once per extracted item (use `[item]` in `cmd`).
- `if_previous_contains`: skip step unless previous stdout contains a substring.
- Line selectors: `line_contains`, `line_before`, `line_after`, `line_only`,
  `line_between_start_contains`, `line_between_end_contains`, `line_after_contains`,
  `line_after_chars`, `between_start_contains`, `between_end_contains`.

## Text extracts

Use `action.text_extracts` to pull structured data from `failure_message`, `testview_log_text`,
or other sources and reuse it in templates or command steps.

```yaml
action:
  text_extracts:
    - name: ilom_failed_cmd
      source: "failure_message"
      line_contains: "FAILED:"
      take: "first"
```

Extracts become template variables and can be referenced in command strings as `{ilom_failed_cmd}`.

## TestView integration

RackBrain can fetch TestView logs and choose templates based on log content. Use the nested
`action.testview` block:

```yaml
action:
  testview:
    testcase:
      contains: "5_PROGRAM_SYSTEM_RECORD"
    testset: "RESET_FACTORY"
    select:
      line_contains: "Error"
      line_before: 2
      line_after: 4
    cases:
      - when:
          contains: "specific signature"
          source: "log_snippet"
        comment_template: |
          Matched signature. {testview_log_snippet_code}
```

Legacy `testview_*` keys are still supported, but the nested block is preferred for new rules.

If `action.testview.cases` is configured and none of the cases match, RackBrain suppresses the
comment body for that rule (useful for "only comment when signature is present" workflows).

### Starting SLT runs

You can request a TestView SLT/PRETEST start:

```yaml
action:
  start_slt: true
  slt_operation: "SLT"
  slt_use_validate: true
```

Command steps can also trigger SLT starts conditionally using `start_testview_on_pass` or
`start_testview_on_fail`.
