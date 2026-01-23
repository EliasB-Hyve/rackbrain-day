# Rule authoring

This doc focuses on writing and safely testing rule YAML in `config/rules/*.yaml`.

For the formal schema summary, also read `config/RULES_REFERENCE.md`.

## What a rule is

A rule is:

- A set of *patterns* (text matchers against Jira `summary + description`)
- An optional *scope* (structured filters against `ErrorEvent` fields)
- An *action* (comment template, optional command steps, optional Jira actions)

Rules are loaded from the list in `rules.files` (config).

## Matching and selection

Core logic lives in:

- `rackbrain/core/classification.py` (scope + pattern matching + winner selection)
- `rackbrain/core/rules_engine.py` (rule YAML parsing)

Important behaviors:

- Rules with no `patterns` are skipped.
- `scope` is checked first.
  - Unknown scope keys are ignored (backwards compatible).
  - If a known scope field exists but is `None`, the scope check fails.
- Confidence is `matched_patterns / total_patterns`.
  - Current minimum confidence threshold is `0.5` (hardcoded default).
- Winner selection:
  1) Highest `priority` wins.
  2) If `priority` is equal, higher confidence wins.

## Rule skeleton

```yaml
- id: example_rule
  name: Example rule
  priority: 10
  scope:
    arch: "EVE"
    jira_location:
      contains: "Fremont"
  patterns:
    - type: contains
      value: "Failure Message:"
    - type: regex
      value: "fmadm\\s+faulty"
  action:
    comment_template: |
      Example comment for {ticket_key} (SN={sn})
```

## Scope fields (what you can filter on)

Scope is matched against fields on `rackbrain/core/models.ErrorEvent`. Commonly used fields:

- Jira-derived: `arch`, `testcase`, `jira_location`, `jira_customer`, `jira_status`, `jira_assignee`,
  `jira_reporter`, `jira_latest_comment_text`, `jira_comments_text`, `jira_slt_attempts`
- DB/TestView-derived: `failure_message`, `failed_testset`, `db_latest_failed_testset`,
  `db_failed_testcase`, `db_failed_testcase_list`, `db_same_failure_count`, `db_latest_slt_id`
- ILOM-derived: `ilom_open_problems_raw`, `ilom_problems`
- Misc: `telnet_cmd`

Scope value formats:

- Scalar: exact match (case-insensitive)
- List: any-of match
- Dict: `{contains: ...}`, `{not_contains: ...}`, `{regex: ...}`

## Action: comment templates and placeholders

Templates use Python `str.format()`. The render context is built in
`rackbrain/services/comment_renderer.py`.

### Common placeholders

- Ticket/rule: `{ticket_key}`, `{sn}`, `{rule_id}`, `{rule_name}`, `{confidence}`
- Jira extraction: `{arch}`, `{testcase}`, `{jira_location}`, `{jira_customer}`, `{jira_reporter}`
- Failure details: `{error_details}`, `{failure_message_selected}`, `{failure_message_selected_code}`
- ILOM: `{ilom_open_problems_raw}`, `{ilom_open_problems_code}`, `{ilom_components}`
- DB/TestView summary: `{db_latest_slt_id}`, `{db_latest_failed_testset}`, `{db_failed_testcase}`,
  `{db_failed_testcase_list}`, `{db_same_failure_count}`
- TestView log: `{testview_log_snippet}`, `{testview_log_snippet_code}`, `{testview_log_error}`
- Command history: `{commands_summary}`, `{all_commands_code}`, `{command_count}`
- Last command: `{last_cmd_context}`, `{last_cmd}`, `{last_cmd_status}`, `{last_cmd_stdout_code}`,
  `{last_cmd_selected_lines_code}`

### Per-command placeholders

When you use `action.command_steps`, every executed step is recorded and added to the template
context using the step `id`.

If a step has `id: ilom_check`, you can use:

- `{command_ilom_check_stdout}`, `{command_ilom_check_stdout_code}`
- `{command_ilom_check_selected_lines}`, `{command_ilom_check_selected_lines_code}`
- `{command_ilom_check_stderr}`, `{command_ilom_check_stderr_code}`
- `{command_ilom_check_status}`, `{command_ilom_check_cmd}`, `{command_ilom_check_context}`

If a step omits `id`, RackBrain auto-generates `cmd_1`, `cmd_2`, etc.

### Template failures

If a template references an unknown placeholder, RackBrain prints a safe fallback message rather
than crashing the whole run.

## Action: command steps (EVE/ILOM/diag)

Command steps are executed by:

- `rackbrain/services/command_steps.py`
- `rackbrain/eve_command_runner.py` (dispatches to runner scripts)

Key points:

- Use `--skip-commands` to test rules/templates without running any commands.
- Commands use a `{context}` prefix. Common contexts in `eve_cmd_runner.sh`:
  `{ilom}`, `{hostnic}`, `{diag}`, `{faultmgmt}`, `{sunservice}`, `{local}`.
- Command selection controls (`line_contains`, `between_start_contains`, etc.) let you surface a
  small relevant excerpt rather than dumping full output into Jira.

## Action: TestView log snippets and SLT starts

TestView support is in `rackbrain/services/testview_actions.py` and the helper module
`Testviewlog.py`.

- To fetch a log snippet for templates, configure either:
  - Legacy keys like `testview_testcase_contains`, or
  - The nested `action.testview` block (preferred).
- To start SLT/PRETEST:
  - Set `action.start_slt: true`, `action.slt_operation: "SLT"` (or `"PRETEST"`), or
  - Trigger from a command step using `start_testview_on_pass` / `start_testview_on_fail`.

## Action: timers (workflow staging)

Timers are persisted by `rackbrain/services/timer_store.py`. They are used to:

- Suppress re-processing a ticket for a duration, and/or
- Gate follow-up rules that should run only after a previous timer has elapsed.

Timers are local state; they do not rely on Jira comments.

## Safe rule testing workflow

1) Add or update a rule in `config/rules/*.yaml`.
2) Run `doctor` to confirm rule file paths load.
3) Test on a real ticket in dry-run:
   - `python -m rackbrain --config config/config.yaml process-ticket MFGS-123456 --skip-commands`
4) If the match is correct, remove `--skip-commands` (if your rule uses command steps).
5) Only then run with `--apply`.

