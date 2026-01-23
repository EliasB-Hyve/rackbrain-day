# RackBrain

RackBrain is a Python rules engine that triages Jira failure tickets using YAML rules stored in
`config/rules/`. It can enrich tickets with hyvetest/TestView data, run EVE/ILOM diagnostics, and
post Jira updates based on rule matches.

This repo is intended to be portable: keep local configuration and all secrets out of version
control.

More docs live in `docs/README.md`.

## What RackBrain actually does

RackBrain's workflow is driven by code, not just config. At runtime it:

- Loads config from `--config`, `RACKBRAIN_CONFIG`, `RACKBRAIN_HOME`, or default config paths.
- Loads rules from the `rules.files` list and evaluates them against ticket text + metadata.
- Enriches tickets with hyvetest DB fields (if DB env vars are set) and TestView context/logs.
- Optionally runs EVE commands (locally via `eve_cmd_runner.sh` or remotely via the SR1 -> RAMSES
  wrapper under `bin/`).
- Applies Jira actions: assign, transition, comment, reassign, and optional issue links.
- Uses timers (SQLite) to suppress repeated actions until a delay expires.
- Supports polling with parallel workers and per-query rule subsets.

## CLI commands

RackBrain ships a CLI with the following subcommands:

- `process-ticket <ISSUE_KEY>`: Fetch, classify, and suggest/comment for one ticket.
  - Dry-run is the default behavior.
  - `--apply` posts live Jira changes (assign/transition/comment/reassign).
  - `--skip-commands` bypasses EVE command execution (still renders templates).
- `poll`: Run a polling loop for tickets matching JQL.
  - Dry-run is the default behavior.
  - `--apply` posts live Jira changes (assign/transition/comment/reassign).
  - `--skip-commands` skips EVE command execution.
  - `--once` runs one poll cycle and exits.
  - `--jql` overrides configured JQL.
  - `--interval` overrides the poll interval.
- `metrics`: Summarize JSON logs for a day (date-based summary).
  - `--date YYYY-MM-DD`, `--days N` (currently informational), `--format text|json`.
- `doctor`: Validate config paths and critical env vars.
  - `--check-db` also checks for DB env vars.

## Quick start (bash on Linux/macOS/WSL recommended)

1) Edit the config YAML to match your workflow, especially who tickets should be reassigned to
(`processing.reassign_to`). Additional guidance for each setting lives as comments in the config
file itself.

2) Add environment variables to your shell (example snippet):

```bash
export RACKBRAIN_HOME="$HOME/rackbrain"
export RACKBRAIN_CONFIG="$RACKBRAIN_HOME/config/config.yaml"

# Jira auth (required)
export RACKBRAIN_JIRA_PAT="YOUR_JIRA_PAT_HERE"

# Optional: DB enrichment (if you want DB-derived fields/rules)
export RACKBRAIN_DB_HOST="YOUR_DB_HOST"
export RACKBRAIN_DB_USER="YOUR_DB_USER"
export RACKBRAIN_DB_PASS="YOUR_DB_PASS"
export RACKBRAIN_DB_NAME="hyvetest"

# Optional: TestView (if you use SLT/TestView features)
export HYVE_TESTVIEW_COOKIE='request_id=...; access_token=...'

# Optional: where RackBrain stores runtime state/logs (portable across CWDs)
export RACKBRAIN_STATE_DIR="$HOME/.local/state/rackbrain"
export RACKBRAIN_LOG_DIR="$RACKBRAIN_STATE_DIR/logs"
export RACKBRAIN_TIMER_DB_PATH="$RACKBRAIN_STATE_DIR/rackbrain_state.sqlite"
```

3) (Optional) Add convenience shell helpers:

```bash
# Enter the repo and activate the local venv if it exists.
rack() {
  cd "$RACKBRAIN_HOME" || return
  if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
  else
    echo "No .venv found at $RACKBRAIN_HOME/.venv"
    return 1
  fi
}

# Poll Jira using the CLI module.
alias poll='python -m rackbrain poll --apply'
```

4) Bootstrap a local virtualenv and install dependencies:

```bash
cd "$RACKBRAIN_HOME"
./scripts/bootstrap.sh
```

5) Run RackBrain:

```bash
./scripts/rackbrain --help
./scripts/rackbrain process-ticket MFGS-123456 --skip-commands
./scripts/rackbrain poll --once --skip-commands
```

On Windows without bash/WSL, you can run via Python directly:

```powershell
py -m rackbrain --help
py -m rackbrain --config config/config.example.yaml doctor
```

## Where to read next

- `docs/README.md`: documentation index and repo map
- `docs/ARCHITECTURE.md`: data flow and code map
- `docs/RUNBOOK.md`: safe operation and troubleshooting
- `docs/RULE_AUTHORING.md`: rule-writing practices and template placeholders
- `config/CONFIG_REFERENCE.md`: config keys
- `config/RULES_REFERENCE.md`: rule YAML schema

## Configuration and state

- Config discovery uses `--config`, then `RACKBRAIN_CONFIG`, then `RACKBRAIN_HOME`, then standard
  `config/config.yaml` and XDG defaults.
- Logging paths (`logging.log_dir`) and timer state (`processing.timer_db_path`) are resolved
  relative to the config base dir or `RACKBRAIN_HOME`.
- Timer state defaults to `state/rackbrain_state.sqlite` unless overridden via
  `RACKBRAIN_TIMER_DB_PATH`.

See also:

- `config/CONFIG_REFERENCE.md` for config settings.
- `config/RULES_REFERENCE.md` for rule schema details.

## Integrations and environment variables

- Jira: `RACKBRAIN_JIRA_PAT` (or `jira.pat`) is required for all live operations.
- hyvetest DB: `RACKBRAIN_DB_HOST`, `RACKBRAIN_DB_USER`, `RACKBRAIN_DB_PASS`,
  `RACKBRAIN_DB_NAME` enable DB enrichment.
- TestView: `HYVE_TESTVIEW_COOKIE` is required to download logs or start SLT runs.
- Cinder verification: `RACKBRAIN_SEIZO_BASE`, `RACKBRAIN_CINDER_DB_PASS`, and related env vars
  are used by the Cinder report integration for specific tickets.

## Notes

- Rules are loaded from the paths listed in `config/config.yaml` (usually `config/rules/*.yaml`);
  there is intentionally no `rackbrain/config/rules/` directory inside the Python package.
- Secrets are read from environment variables; never commit PATs, DB passwords, or TestView
  cookies.
