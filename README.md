# RackBrain

RackBrain is a Python rules engine that triages Jira failure tickets using YAML rules under `config/rules/`.

This repo is intended to be portable: keep local config and all secrets out of version control.

## Quick start (Linux/macOS bash)

1) Create a local config file:

```bash
cp config/config.example.yaml config/config.yaml
```
alias poll='python -m rackbrain poll --apply'

2) Add exports to your shell (example snippet):

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
How to get testview cookie(copy the entire block of text next to "Cookie")
<img width="540" height="400" alt="image" src="https://github.com/user-attachments/assets/f863c5b5-4512-4e25-9e32-7b2b6313d91f" />
# ----------------------------
# Convenience: enter project + activate venv
# ----------------------------
    rack() {
    cd "$RACKBRAIN_HOME" || return
    if [ -f ".venv/bin/activate" ]; then
        source ".venv/bin/activate"
    else
        echo "No .venv found at $RACKBRAIN_HOME/.venv"
        return 1
    fi
    }


3) Bootstrap a local virtualenv and install deps:

```bash
cd "$RACKBRAIN_HOME"
./scripts/bootstrap.sh
```

4) Run:

"rack" alias to enable env
"poll" to start polling for tickets to be handled
```bash
./scripts/rackbrain --help
./scripts/rackbrain process-ticket MFGS-123456 --skip-commands
./scripts/rackbrain poll --once --skip-commands
```

## Notes

- Rules are loaded from the paths listed in `config/config.yaml` (usually `config/rules/*.yaml`); there is intentionally no `rackbrain/config/rules/` directory inside the Python package.
- Secrets are read from environment variables; never commit PATs, DB passwords, or TestView cookies.
