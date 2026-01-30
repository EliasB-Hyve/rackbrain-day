# Integrations

RackBrain is a rules engine that reads Jira tickets and can optionally enrich context or run
external steps. Most integrations are enabled by environment variables (to keep secrets out of
version control).

## Jira (required)

Purpose:

- Read Jira issues (summary/description/comments/fields).
- Optionally write updates when you run with `--apply` (comment, assignment, transitions, links).

Configuration:

- `jira.base_url` in `config/config.yaml`
- Jira auth via `RACKBRAIN_JIRA_PAT` (or the env var named by `jira.pat_env`)

## hyvetest DB enrichment (optional)

Purpose:

- Enrich the `ErrorEvent` with DB-derived fields such as `failure_message`, `failed_testset`,
  `db_failed_testcase_list`, and `db_same_failure_count` so rules can be more precise.

Enablement:

- `RACKBRAIN_DB_HOST`
- `RACKBRAIN_DB_USER`
- `RACKBRAIN_DB_PASS`
- `RACKBRAIN_DB_NAME`

Behavior:

- If any DB env var is missing, DB lookups are skipped (expected behavior).

## TestView (optional)

Purpose:

- Fetch TestView logs/snippets for comments.
- Optionally start SLT/PRETEST runs when a rule requests it (only when `--apply`).

Enablement:

- `HYVE_TESTVIEW_COOKIE` (required for TestView API calls)
- Optional: `HYVE_TESTVIEW_BASE_URL`

Notes:

- Log retrieval is performed by `Testviewlog.py` and is cookie-authenticated.
- When rules request TestView snippets, RackBrain may call TestView even in dry-run.

## EVE/ILOM/diag command steps (optional)

Purpose:

- Run remote commands (ILOM/diag/faultmgmt/etc.) and surface relevant excerpts in comments.

Enablement:

- Rule-side: `action.command_steps`
- Runtime: environment and scripts required for the runner path.

Operational notes:

- Use `--skip-commands` for safe iteration on matching and templates.
- The remote wrapper path requires a Linux environment capable of running `/bin/bash` and the
  wrapper dependencies (for example, `sshpass`).

## Precheck (OCR auto-pass)

Purpose:

- For PRECHECK / PRE-RLT tickets, detect the precheck phrase in:
  - Jira description and comments (fast path), then
  - OCR image attachments when needed.
- Enable YAML rules to comment `Pass` and transition to `Closed`.

Implementation:

- Enrichment logic: `rackbrain/integrations/precheck.py`
- Default rule: `config/rules/precheck_rules.yaml`

Enablement:

- Uses normal RackBrain Jira auth via `RACKBRAIN_JIRA_PAT` (no separate credentials).
- OCR dependencies are pinned in `pyproject.toml` / `requirements.txt`:
  `Pillow`, `numpy`, `rapidocr-onnxruntime`, `onnxruntime`.

Debugging:

- `JIRA_OCR_DEBUG`: set truthy to enable OCR debug dumps
- `JIRA_OCR_DEBUG_DIR`: directory for debug output (default: `~/ocr_debug`)

Fields added to `ErrorEvent`:

- `precheck_marker_found` (bool)
- `precheck_phrase_found` (bool)
- `precheck_phrase_source` (str; `description`, `comments`, or `attachment:<filename>`)
- `precheck_latest_comment_is_pass` (bool; spam prevention)

## Cinder verification (special-case)

Purpose:

- Build a report for specific "Outpost Refurb - Cinder Verification" tickets and close/reassign
  them via a dedicated rule (`config/rules/cinder_verification.yaml`).

Data sources:

- MySQL query via the `mysql` CLI (requires `mysql` in `PATH`).
- Seizo HTTP calls (requires network access to the configured base URL).

Enablement (secrets via env):

- `RACKBRAIN_CINDER_DB_PASS` (or `RACKBRAIN_DB_PASS` as fallback)
- Optional overrides: `RACKBRAIN_SEIZO_BASE`, `RACKBRAIN_CINDER_DB_HOST`,
  `RACKBRAIN_CINDER_DB_USER`, `RACKBRAIN_CINDER_DB_NAME`, `RACKBRAIN_CINDER_DB_PASS_ENV`

Behavior:

- If report generation fails, RackBrain intentionally does not edit the Jira ticket.
