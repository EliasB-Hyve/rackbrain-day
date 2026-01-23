# RackBrain Rules YAML Reference (for rule authors)

This document explains the YAML keywords used in `config/rules/*.yaml`, what they mean, and where they can be used.

## How rules are evaluated

1. **Scope filtering**: `scope` is checked first against structured fields on `ErrorEvent`. If any scope check fails, the rule is ignored.
2. **Pattern scoring**: `patterns` are matched against `combined_text` (ticket summary + description). Confidence is:
   `matched_patterns / total_patterns`.
3. **Winner selection**: highest `priority` wins; ties break by higher confidence.

Notes:
- If `scope` references an unknown field, it is ignored (backwards compatible).
- If `scope` references a known field that is `None`, the scope check fails.
- Rules must have at least 1 pattern (even if you rely mostly on scope).
- Rules only evaluate tickets that were fetched by polling JQL; rules cannot "expand" the search window. If you need older/different tickets for a specific rule without changing other rules, use `polling.extra_queries` in `config/config.yaml` (see `config/CONFIG_REFERENCE.md`).

---

## Rule file format

Each rules file is a YAML list of rules:

```yaml
- id: unique_rule_id
  name: "Human readable name"
  description: >
    Long description (optional).
  priority: 20
  allow_on_same_failure: false

  scope:
    arch: "EVE"
    jira_location:
      contains: "Fremont"

  patterns:
    - type: contains
      value: "some text"

  action:
    type: comment_only
    comment_template: |
      Your comment here for {sn}
```

---

## Top-level rule keys

- `id` (required): unique identifier string.
- `name` (optional): human-friendly name.
- `description` (optional): longer explanation.
- `priority` (optional, default `0`): higher wins when multiple rules match.
- `allow_on_same_failure` (optional, default `false`): when `db_same_failure_count >= 2`, RackBrain restricts eligibility to rules with this flag (otherwise it skips the ticket).
- `allow_high_slt_attempts` (optional, default `false`): allow this rule to be considered even when `jira_slt_attempts` exceeds the global hard limit (`MAX_SLT_ATTEMPTS`).
- `scope` (optional): hard filters on structured fields (see below).
- `patterns` (required): text matchers against `combined_text`.
- `action` (required): what to do when the rule wins.

---

## `scope`: structured filters

`scope` keys correspond to attributes on `ErrorEvent` (examples: `arch`, `jira_location`, `failure_message`, `db_latest_failed_testset`, `testcase`, `ilom_open_problems_raw`, etc.).

Supported forms:

### 1) Exact match (case-insensitive)
```yaml
scope:
  arch: "EVE"
```

### 2) One-of list (case-insensitive exact match)
```yaml
scope:
  jira_latest_comment_author:
    - "alice@example.com"
    - "bob@example.com"
```

### 3) Contains / not_contains / regex
```yaml
scope:
  failure_message:
    contains: "Advanced Boot Loader"
    not_contains: "some excluded text"
  jira_slt_attempts:
    regex: "^[2-9][0-9]*$"
```

### Common `ErrorEvent` fields you can use in `scope`

These are the most-used structured fields available for scope checks (case-insensitive matching rules still apply):

- Ticket text: `combined_text`, `ticket.summary`, `ticket.description`
- Identity: `sn`, `arch`, `testcase`
- Jira fields: `jira_location`, `jira_customer`, `jira_slt_attempts`, `jira_model`, `jira_customer_ipn`, `jira_slt_rack_sn`, `jira_tester_email`
- Jira comments: `jira_comments_text`, `jira_latest_comment_text`, `jira_latest_comment_author`, `jira_latest_comment_author_display_name`, `jira_latest_comment_author_email`
- DB/TestView summary: `failure_message`, `failed_testset`, `db_latest_failed_testset`, `db_failed_testcase`, `db_failed_testcase_list`, `db_same_failure_count`, `db_latest_slt_id`
- ILOM: `ilom_open_problems_raw`

Tip: you can also use *any* other `ErrorEvent` field in `scope`; unknown fields are ignored (but known fields that are `None` will cause the scope check to fail).

---

## `patterns`: matching ticket text

`patterns` always match against `combined_text` (summary + description), not the DB fields.

Pattern item:
```yaml
patterns:
  - type: contains   # or: not_contains, regex
    value: "literal substring"
```

Tips:
- `contains` is a literal substring match (case-insensitive). Punctuation/spacing must still match.
- `not_contains` matches when the substring is NOT present (case-insensitive).
- Use `regex` when formatting varies.
- If you rely mainly on `scope`, you can use an “always-true” pattern:
  ```yaml
  patterns:
    - type: regex
      value: ".*"
  ```

---

## `action`: what happens on match

Common action keys:
- `type`: currently `comment_only`.
- `close`: reserved for future behavior (usually `false`).
- `comment_template`: the Jira comment text template.
- `link_issue`: optionally create a Jira **Issue Link** to another ticket (see below).
- `timer_after_seconds`: start an internal timer (see below).
- `ilom_filter_contains`: list of filter phrases used when rendering `{ilom_components}`.
- `assign_to`, `reassign_to`, `transition_to`: optional Jira actions (if enabled in processing config).

### `action.link_issue`: link this ticket to another ticket

Use this for outages / master-tracking tickets. This creates a standard Jira "Issue Links"
relationship (the same thing you do manually via **More → Link**).

```yaml
action:
  type: "comment_only"
  comment_template: |
    Linked to PRODISS-15074 for tracking.
  link_issue:
    type: "is blocked by"   # or: "relates to"
    target: "PRODISS-15074"
```

Supported values:
- `type`: `"is blocked by"` or `"relates to"` (case-insensitive)
- `target`: the issue key to link to (e.g. `PRODISS-15074`)

Notes:
- If linking fails in live mode, RackBrain skips posting the comment and skips reassignment so it can retry next poll cycle.

### Template placeholders (common)

You can reference these in `comment_template`:
- Ticket/identity: `{ticket_key}`, `{sn}`
- Rule info: `{rule_id}`, `{rule_name}`, `{confidence}`
- DB/TestView metadata: `{db_latest_failed_testset}`, `{db_failed_testcase}`, `{db_failed_testcase_list}`, `{db_same_failure_count}`, `{db_latest_slt_id}`
- Failure message selection: `{failure_message_selected}`, `{failure_message_selected_code}`
- ILOM: `{ilom_open_problems_raw}`, `{ilom_open_problems_code}`, `{ilom_components}`
- TestView snippet: `{testview_log_snippet}`, `{testview_log_snippet_code}`
- All commands: `{all_commands_code}`, `{commands_summary}`, `{command_count}`

For each command step with `id: some_id`, you also get:
- `{command_some_id_stdout}`, `{command_some_id_stdout_code}`
- `{command_some_id_selected_lines}`, `{command_some_id_selected_lines_code}`
- `{command_some_id_status}`, `{command_some_id_cmd}`, `{command_some_id_context}`, `{command_some_id_info}`

---

## `timer_after_seconds`: internal non-blocking timer

You can add `timer_after_seconds` either:
- under `action` (unconditional when the rule wins), or
- under a `command_steps` item (conditional when the step runs; if the step has any `expect_*`, it only starts when expectations pass).

Behavior:
- When a timer is active for a ticket, RackBrain will **skip the ticket entirely** (no rules will match) until it expires.
- After the timer expires, the **rule that started the timer is suppressed** (so it doesn’t restart the timer in a loop).
- That suppression clears automatically when the ticket’s **assignee or status changes** (this is the “flag” that lets the same timer rule run again).

Follow-up rules (no Jira marker needed):
- When a timer has expired, RackBrain exposes a scope field `timer_expired_for` (a list of rule ids whose timers have expired for this ticket under the current assignee+status).
- Gate a follow-up rule like:
```yaml
scope:
  timer_expired_for: "your_timer_rule_id"
```

Example (start timer + comment):
```yaml
action:
  timer_after_seconds: 600
  comment_template: |
    Kicked off async check; I’ll follow up in ~10 minutes.
```

Example (start timer without commenting):
```yaml
action:
  timer_after_seconds: 600
  comment_template: ""
```

---

## `command_steps`: run commands and select output

Under `action`:

```yaml
action:
  command_steps:
    - id: "fmadm_faulty"
      cmd: "{faultmgmt} fmadm faulty -a"
      expect_status: 0
      expect_contains: "Suspect"
      expect_not_contains: "No problems"
      stop_on_decision: false
      on_expect_pass_comment: |
        Optional comment override when expectations pass.
      on_expect_fail_comment: |
        Optional comment override when expectations fail.
```

### Command contexts (`{ilom}`, `{hostnic}`, ...)

In `command_steps[].cmd`, you prefix the command with a context in curly braces:

- `{ilom}`: SSH as `root@ILOM_IP`
- `{hostnic}`: SSH as `root@HOSTNIC_IP`
- `{sunservice}`: SSH as `sunservice@ILOM_IP`
- `{diag}`: run inside ILOM diag shell (`/SP/diag/shell`)
- `{faultmgmt}`: run inside ILOM faultmgmt shell (`/SP/faultmgmt/shell`)
- `{local}`: run locally on the TE box (RAMSES)

Connection failures are detected via the command step expectations:
- Use `expect_status: 0` to require a successful connection/command.
- If it fails, `status` will be non-zero (SSH commonly uses `255`) and the error is typically in `{command_<id>_stderr_code}`.

Output selection for a command step (for `{command_<id>_selected_lines}`):
- `line_contains`: pick lines containing substring (+ optional context).
  - `line_before`, `line_after`: number of context lines.
  - `line_only: true`: only the matching line(s), no context.
- `line_not_contains`: filter out any selected line(s) containing this substring.
- `between_start_contains` + `between_end_contains`: pick lines between markers (inclusive).
- **Inline (same-line) extraction** (returns only the extracted fragments):
  - `line_between_start_contains` + `line_between_end_contains`
  - `line_after_contains` + `line_after_chars` (`0` = rest of line)

If inline extraction is set, it takes precedence for that step.

Flow control:

Looping (advanced):
- `for_each_extract`: run this command once per item from a `text_extracts` variable.
  - The referenced extract should usually use `take: all` (which returns one item per line).
  - In the `cmd`, use `[item]` and it will be replaced with the current item value.
  - If you use single quotes `'` in a `{hostnic}` / `{ilom}` / `{diag}` / `{faultmgmt}` command, make sure your `bin/eve_cmd_runner_remote.sh` is up to date; older versions break when the command contains `'`.

Example:
```yaml
text_extracts:
  - name: pcie_addresses
    source: combined_text
    line_contains: "Link speed/width for PCIE slot"
    line_between_start_contains: "slot "
    line_between_end_contains: " in GI:"
    take: all

command_steps:
  - id: pcie_lnksta
    for_each_extract: pcie_addresses
    cmd: "{hostnic} lspci -vvv -s [item] | grep -i LnkSta"
    expect_status: 0
    expect_contains: "LnkSta"
    expect_not_contains: "downgraded"
    stop_on_decision: false
```
- `if_previous_contains`: only run this step if the previous step's stdout contains the substring.
- In any command step comment override, you can include:
  - `{command_<id>_stdout_code}` for stdout
  - `{command_<id>_stderr_code}` for stderr (often contains SSH/connection errors)

---

## Failure message selection (for `{failure_message_selected}`)

Under `action`:

Line/block selection:
- `failure_message_between_start_contains` + `failure_message_between_end_contains`
- `failure_message_line_contains` (+ `failure_message_line_before`, `failure_message_line_after`)

Inline (same-line) extraction (takes precedence if set):
- `failure_message_line_between_start_contains` + `failure_message_line_between_end_contains`
- `failure_message_line_after_contains` + `failure_message_line_after_chars` (`0` = rest of line)

---

## TestView log snippet selection (for `{testview_log_snippet}`)

Under `action` (only runs if `testview_testcase_contains` is set):
- `testview_testcase_contains` (required to enable snippet fetching)
- `testview_testset` (optional override; otherwise uses latest failed testset)

Selection options:
- `testview_line_contains` (+ `testview_line_before`, `testview_line_after`)
- `testview_between_start_contains` + `testview_between_end_contains`
- `testview_filter_line_contains` (post-filter within the selected snippet)

Inline (same-line) extraction:
- `testview_line_between_start_contains` + `testview_line_between_end_contains`
- `testview_line_after_contains` + `testview_line_after_chars` (`0` = rest of line)

---

## TestView conditional comments (first match wins)

If you want RackBrain to **post a different comment depending on what the TestView log/snippet contains**,
use the nested `action.testview` shape with ordered `cases`.

Behavior:
- RackBrain fetches the TestView log/snippet once.
- It evaluates `cases` in order; the **first matching** case selects the comment template.
- If `cases` is present and **no case matches**, RackBrain posts **no comment**.

Example:
```yaml
action:
  type: comment_only

  testview:
    # Which testcase log to fetch
    testcase:
      contains: "5_PROGRAM_SYSTEM_RECORD"
    testset: "RESET_FACTORY"  # optional override

    # Optional: default snippet selection (used for templates and "source: auto"
    # when a case doesn't define its own select)
    select:
      between_start_contains: "Error: Connector value"
      between_end_contains: "SP      Aspeed Revis"

    # Ordered "if" cases (first match wins)
    cases:
      - when:
          contains: "Known signature A"
          source: "auto"  # default; or: "log_snippet" / "log_text"
        # Optional per-case snippet selection override (line/between + before/after).
        # When present and this case matches, {testview_log_snippet_code} will use
        # this case's selection (not the action.testview.select selection).
        select:
          line_contains: "Known signature A"
          line_before: 0
          line_after: 2
        comment_template: |
          Found signature A in TestView.

          Snippet:
          {testview_log_snippet_code}

      - when:
          regex: "SignatureB:\\s*[0-9]+"
          source: "log_text"
        comment_template: |
          Found signature B in full TestView log.
```

Notes:
- This is **in addition to** the existing `testview_*` action keys; old rules keep working.

---

## `text_extracts`: create new template variables from text (no regex)

`text_extracts` lets you pull specific values from a text source and use them in templates as `{your_name}`.

Example:
```yaml
action:
  text_extracts:
    - name: iou_bay
      source: failure_message
      line_contains: "hwdiag_io_config -> IOU Bay"
      line_between_start_contains: "IOU Bay "
      line_between_end_contains: " in GI"
      take: first
      default: "?"
```

Keys:
- `name` (required): the variable name added to templates.
- `source` (optional, default `failure_message`): where to read from. This is a path on `ErrorEvent` such as:
  - `failure_message`, `combined_text`, `ticket.summary`, `ticket.description`,
    `ilom_open_problems_raw`, `jira_comments_text`, `jira_latest_comment_text`,
    `testview_log_text`, `testview_log_snippet`, etc.
- Optional narrowing:
  - `between_start_contains` + `between_end_contains`: narrow to the smallest matching line block.
  - `line_contains`: keep only lines containing this substring.
- Inline extraction (same-line):
  - `line_between_start_contains` + `line_between_end_contains`
  - `line_after_contains` + `line_after_chars` (`0` = rest of line)
- `take` (optional): which match to return if there are multiple fragments:
  - `first` (default), `last`, `all`
- `default` (optional): fallback value if nothing is found.

Tip: if you want to base a rule on DB text but confirm Jira matches too, add both fields to `scope`:
```yaml
scope:
  failure_message:
    contains: "some marker"
  combined_text:
    contains: "some marker"
```

---

## Example: IOU mismatch (plain English)

```yaml
comment_template: |
  IOU{iou_bay} has PCIe cable plugged into slot {system_slot} instead of slot {gi_slot} (correct location).

text_extracts:
  - name: iou_bay
    source: failure_message
    line_contains: "hwdiag_io_config -> IOU Bay"
    line_between_start_contains: "IOU Bay "
    line_between_end_contains: " in GI"
  - name: gi_slot
    source: failure_message
    between_start_contains: "in GI:"
    between_end_contains: "in system:"
    line_contains: "PCIe Data Connectors on IOU Module"
    line_between_start_contains: "['"
    line_between_end_contains: "'"
  - name: system_slot
    source: failure_message
    between_start_contains: "in system:"
    between_end_contains: "Check Result:"
    line_contains: "PCIe Data Connectors on IOU Module"
    line_between_start_contains: "['"
    line_between_end_contains: "'"
```
