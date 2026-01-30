# Runbook

This runbook is for operators running RackBrain against a real Jira instance.

## Golden rules

- Dry-run is the default: it prints the comment but does not edit Jira.
- Use `--apply` only when you are confident the config/rules are correct.
- Start with `--skip-commands` until EVE/ILOM command execution is known-good in your environment.
- Keep secrets out of the repo: use environment variables for Jira PAT, DB password, TestView cookie.

## Setup

### 1) Create a local config

`config/config.yaml` is intentionally gitignored. Create it from the example:

- Copy `config/config.example.yaml` -> `config/config.yaml`
- Edit the Jira base URL and your desired default processing actions.

### 2) Set required environment variables

Minimum for anything that talks to Jira:

- `RACKBRAIN_JIRA_PAT` (or whatever `jira.pat_env` is set to)

Optional integrations:

- hyvetest DB enrichment: `RACKBRAIN_DB_HOST`, `RACKBRAIN_DB_USER`, `RACKBRAIN_DB_PASS`,
  `RACKBRAIN_DB_NAME`
- TestView: `HYVE_TESTVIEW_COOKIE`
- Cinder verification (specific tickets): `RACKBRAIN_CINDER_DB_PASS` (and optionally
  `RACKBRAIN_SEIZO_BASE`, `RACKBRAIN_CINDER_DB_HOST`, `RACKBRAIN_CINDER_DB_USER`,
  `RACKBRAIN_CINDER_DB_NAME`)

### 3) Install dependencies

Linux:

- `./scripts/bootstrap.sh`

## Smoke checks

- `python -m rackbrain --help`
- `python -m rackbrain --config config/config.example.yaml doctor`

`doctor` checks:

- Config path selection and base dir
- Rule file existence (`rules.files`)
- Jira PAT presence (config or env)
- `--check-db`: whether DB env vars are present (it does not validate connectivity)

## Running safely

### Single ticket (recommended for rule testing)

Dry run (no Jira writes):

- `python -m rackbrain --config config/config.yaml process-ticket MFGS-123456 --skip-commands`

Apply mode:

- `python -m rackbrain --config config/config.yaml process-ticket MFGS-123456 --apply`

### Polling

Dry run:

- `python -m rackbrain --config config/config.yaml poll --once --skip-commands`

Apply mode (live):

- `python -m rackbrain --config config/config.yaml poll --once --apply`

For continuous polling, omit `--once`. Use Ctrl+C to stop.

## Logs, metrics, and state

### Logs

- Processing logs are written under `logging.log_dir` (config-normalized to an absolute path).
- When `logging.rotate_daily` is enabled, `rackbrain_processed.log` becomes
  `rackbrain_processed_YYYY-MM-DD.log`.

### Metrics

The `metrics` subcommand summarizes JSON logs in `logging.log_dir`.

Notes:

- Metrics require `logging.log_format: json`.
- The CLI currently accepts `--days`, but the summary output is primarily date-based (`--date`).

### Timers (suppression)

Timers are stored in a local SQLite file (default: `paths.state_dir/rackbrain_state.sqlite`).

Common operational implications:

- If a ticket appears to "stop matching" after an action, check whether a timer is active.
- Clearing timers is a local-state operation; deleting the SQLite file resets suppression state.

## Troubleshooting

### "RackBrain config not found"

- Provide `--config path/to/config.yaml`, or set `RACKBRAIN_CONFIG`, or set `RACKBRAIN_HOME`.
- If you are in a repo checkout, `./config/config.yaml` is also a valid default.

### "Missing Jira PAT" / Jira 401

- Ensure `RACKBRAIN_JIRA_PAT` is set (or the env var named by `jira.pat_env`).
- Confirm PAT permissions allow reading issues and doing the intended writes.

### Rules files missing

- Run `doctor` and fix `rules.files` paths.
- Prefer keeping rule paths relative to the repo config base dir and letting RackBrain normalize.

### DB enrichment skipped

- `rackbrain/adapters/hyvetest_client.py` skips DB lookups unless all `RACKBRAIN_DB_*` env vars are
  present. This is expected for "ticket-only" runs.

### TestView errors / missing cookie

- TestView requires `HYVE_TESTVIEW_COOKIE`.
- In dry-run mode, RackBrain still *may* attempt to fetch logs if rules request it; disable
  log-based rules or remove TestView selectors when running without TestView access.

### EVE command execution issues

If command steps fail:

- Confirm you are running in an environment that supports the remote wrapper (Linux host with
  `/bin/bash` and `sshpass`).
- Confirm `RAMSES_TESTER_PASS` is set for `bin/eve_cmd_runner_remote.sh`.
- Use `--skip-commands` to continue validating rule matching and template output without commands.
