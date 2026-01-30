# RackBrain rules

This document is the canonical reference for writing and maintaining RackBrain rules in
`config/rules/*.yaml`.

## Where rules live

- Rule files live in `config/rules/*.yaml`.
- The list of rule files is configured in `config/config.yaml` under `rules.files`.

## How rules are evaluated

For each ticket, RackBrain builds an `ErrorEvent` (ticket fields + optional enrichment), then:

1) Scope filtering: `scope` is checked first against `ErrorEvent` fields.
2) Pattern scoring: `patterns` are matched and a confidence score is computed:
   `matched_patterns / total_patterns`.
3) Winner selection: highest `priority` wins; ties break by higher confidence.

Notes:

- Rules must have at least 1 pattern. Rules with no patterns are skipped.
- Unknown `scope` keys are ignored (backwards compatible).
- Some integrations add additional `ErrorEvent` fields that you can use in `scope`/`patterns`.
- If a known `scope` field exists but is `None`, the scope check fails.

## Rule file format

Each rules file is a YAML list of rules:

```yaml
- id: example_rule
  name: Example rule
  description: Optional longer text.
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

## Top-level keys

- `id` (required): unique identifier string.
- `name` (optional): human-friendly name.
- `description` (optional): longer explanation.
- `priority` (optional, default `0`): higher wins when multiple rules match.
- `allow_on_same_failure` (optional, default `false`): opt-in when the same failure repeats.
- `allow_high_slt_attempts` (optional, default `false`): opt-in when SLT attempts are high.
- `scope` (optional): structured filters (see below).
- `patterns` (required): text matchers (see below).
- `action` (required): what to do when the rule wins (see below).

## `scope`: structured filters

Scope keys correspond to attributes on `ErrorEvent` (see `rackbrain/core/models.py`). Scope values
can be:

1) Exact match (case-insensitive)

```yaml
scope:
  arch: "EVE"
```

2) One-of list (case-insensitive exact match)

```yaml
scope:
  jira_location:
    - "Fremont"
    - "Austin"
```

3) Contains / not_contains / regex

```yaml
scope:
  failure_message:
    contains: "Advanced Boot Loader"
    not_contains: "excluded text"
  jira_slt_attempts:
    regex: "^[2-9][0-9]*$"
```

## `patterns`: matching ticket and enriched text

Each pattern item has:

- `type` (required): `contains`, `not_contains`, or `regex` (case-insensitive).
- `value` (required): the substring or regex.
- `source` (optional): where to read the haystack text from (default: `combined_text`).

By default, patterns match against `combined_text` (ticket summary + description). You can match
against other `ErrorEvent` fields by setting `source`, for example:

```yaml
patterns:
  - type: contains
    value: "Open Problems"
    source: ilom_open_problems_raw
  - type: regex
    value: "io.pcie.*ce"
    source: failure_message
```

`source` also supports dotted paths like `ticket.summary` when the attribute chain exists.

## `action`: what happens when a rule wins

The `action` block controls comment templates, optional command/TestView steps, and optional Jira
updates (only when you run with `--apply`).

### Core Jira fields

```yaml
action:
  comment_template: |
    Comment text for {ticket_key}
  assign_to: "currentUser()"
  reassign_to: "someone@example.com"
  transition_to: "In Progress"
  transition_comment_mode: "during_transition"
```

Notes:

- `transition_comment_mode: during_transition` posts the comment as part of the transition request.
- Per-rule `assign_to` / `reassign_to` override the defaults from `processing.*` in config.
- Setting `reassign_to: ""` disables reassignment for that rule.

### Timers (suppression / staging)

```yaml
action:
  timer_after_seconds: 3600
```

Timers are local state (SQLite) and suppress re-processing until the timer expires.

### Issue linking (optional)

```yaml
action:
  link_issue:
    type: "is blocked by"
    target: "PRODISS-1234"
```

Supported `type` values are currently `is blocked by` and `relates to`.

### Command steps (EVE/ILOM/diag)

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

Use `--skip-commands` to test matching and templates without running command steps.

### Text extracts

Use `action.text_extracts` to pull structured data from `failure_message`, `testview_log_text`,
or other sources and reuse it in templates or command strings.

### TestView integration

RackBrain can:

- Fetch TestView log snippets for templates.
- Start SLT/PRETEST runs (only when `--apply`).
- Choose a comment template based on TestView log content.

There are two configuration styles:

- Legacy `testview_*` keys on `action` (supported for compatibility).
- A nested `action.testview` block (preferred for new rules).

See `config/rules/rule_template_comprehensive.yaml` for a complete, copyable example.

## Templates and placeholders

Templates use Python `str.format()`. The render context is built in
`rackbrain/services/comment_renderer.py`. For a quick way to discover available placeholders, look
at:

- `rackbrain/core/models.py` (`ErrorEvent` fields)
- `rackbrain/services/comment_renderer.py` (template context mapping)

## Safe rule testing workflow

1) Update a rule file under `config/rules/*.yaml`.
2) Run `python -m rackbrain --config config/config.yaml doctor` to confirm rule files load.
3) Test a real ticket in dry-run:
   `python -m rackbrain --config config/config.yaml process-ticket MFGS-123456 --skip-commands`
4) If matching is correct, remove `--skip-commands` (if your rule uses command steps).
5) Only then run with `--apply`.
