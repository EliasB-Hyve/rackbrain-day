# Architecture

This doc describes the RackBrain runtime pipeline, the main modules, and where side effects happen.

## End-to-end flow

RackBrain has two main execution modes: single ticket (`process-ticket`) and polling (`poll`).
Both paths share the same per-ticket pipeline.

### CLI entrypoint

Entry: `rackbrain/cli/main.py`

High level:

1) Parse args (`process-ticket`, `poll`, `metrics`, `doctor`)
2) Load and normalize config (`rackbrain/core/config_loader.py`)
3) Initialize logging (`rackbrain/services/logger.py`)
4) Load rules from YAML (`rackbrain/core/rules_engine.py`)
5) Create Jira client (`rackbrain/adapters/jira_client.py`)
6) Dispatch to:
   - `rackbrain/services/ticket_processor.process_ticket()` (single ticket)
   - `rackbrain/services/polling_service.run_polling_loop()` (poll)

### Per-ticket pipeline

Orchestrator: `rackbrain/services/ticket_processor.py`

For a single Jira issue key:

1) Jira fetch (read side effect)
   - `JiraClient.get_issue()`
   - May also fetch the latest comment via `JiraClient.get_issue_comments()` if Jira truncated the
     issue payload's comments.
2) Build context (`ErrorEvent`)
   - `rackbrain/core/context_builder.build_ticket()`
   - `rackbrain/core/context_builder.build_error_event()`
     - Extracts structured fields from Jira text (SN, testcase, etc.)
     - Optional DB lookup via
       `rackbrain/adapters/hyvetest_client.fetch_server_details_from_db()`
     - Optional ILOM open problems fetch via
       `rackbrain/adapters/ilom_client.get_open_problems_output()`
     - Optional TestView \"latest failed run\" enrichment via
       `rackbrain/core/testview_context.add_testview_context()`
3) Timer suppression (local state side effect)
   - `rackbrain/services/timer_store.TimerStore` can suppress processing (or specific rules) based
     on persisted timers in a local SQLite DB.
4) Rule matching
   - `rackbrain/core/classification.classify_error()`
     - Filters by rule `scope` first.
     - Then computes a simple confidence = matched_patterns / total_patterns.
     - Selects by highest `priority`, then highest confidence.
5) Action-specific enrichment
   - Optional command steps (EVE/ILOM/diag):
     `rackbrain/services/command_steps.execute_command_steps()`
   - Optional SLT/PRETEST start:
     `rackbrain/services/testview_actions.maybe_start_slt_for_action()`
   - Optional TestView log snippet fetch:
     `rackbrain/services/testview_actions.populate_testview_log_for_action()`
   - Optional \"TestView case template override\":
     `rackbrain/services/testview_actions.select_testview_case_template()`
   - Optional Cinder report:
     `rackbrain/integrations/cinder_verification.build_cinder_verification_report()`
   - Optional Precheck phrase detection (OCR attachments when needed):
     `rackbrain/integrations/precheck.populate_precheck_context()`
6) Comment rendering
   - `rackbrain/services/comment_renderer.build_comment_body()`
   - Uses Python `str.format()` against a context dictionary (see `docs/RULES.md`).
7) Apply Jira actions (write side effects, only when `--apply`)
   - `rackbrain/services/jira_actions.apply_jira_actions()`
     - Assign -> transition -> (optional link) -> comment -> reassign.
     - Some rules can add a timer to stage multi-step workflows.
8) Logging (local filesystem side effect)
   - `rackbrain/services/logger.ProcessingLogger` writes structured or text logs.
   - Optional \"rule match history\" file groups matches by rule id.

### Polling mode

Entry: `rackbrain/services/polling_service.py`

Polling:

- Queries Jira via JQL (`JiraClient.search_issues()`).
- Processes each ticket in a thread pool (`ThreadPoolExecutor`).
- Supports `polling.extra_queries` to re-run additional JQL queries using only a subset of rules
  (`only_rule_ids`), while de-duplicating tickets already handled earlier in the same cycle.

## Side effects and dependencies

RackBrain is intentionally \"read heavy\" in dry-run mode, and \"write capable\" in apply mode.

**Always-on (when invoked)**

- Reads config YAML from disk.
- Reads rules YAML from disk.

**Jira**

- Reads: issue fields, transitions, comments.
- Writes (only with `--apply`): assignment, transitions, comments, issue links.

**hyvetest DB (optional)**

- Reads over MySQL (`pymysql`) when `RACKBRAIN_DB_*` env vars are present.
- If missing DB env vars, DB enrichment is skipped and rules relying on DB fields may not match.

**TestView (optional)**

- Reads logs and run metadata over HTTPS via `Testviewlog.py` when `HYVE_TESTVIEW_COOKIE` is set.
- Can start SLT/PRETEST runs (only with `--apply` and when requested by the rule).

**EVE/ILOM commands (optional)**

- Command steps call into `rackbrain/eve_command_runner.py`, which prefers the \"remote wrapper\"
  path `rackbrain/eve_remote.py` + `bin/eve_cmd_runner_remote.sh` (Linux only; requires `/bin/bash`
  and `sshpass`).
- The bash runner that actually executes commands is `eve_cmd_runner.sh`.

**Local state**

- Timer DB: SQLite file, default under `paths.state_dir` (see `config/CONFIG_REFERENCE.md`).
- Logs: structured/text logs, default under `logging.log_dir`.

## Configuration and path semantics

Config discovery and path normalization are in `rackbrain/core/config_loader.py`:

- Config is searched in this order: `--config` -> `RACKBRAIN_CONFIG` -> `RACKBRAIN_HOME` ->
  `./config/config.yaml` -> XDG paths.
- After loading, relative paths are rewritten to absolute paths using a stable base directory so
  calling RackBrain from different working directories behaves consistently.
