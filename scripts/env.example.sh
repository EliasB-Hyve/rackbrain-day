#!/usr/bin/env bash
#
# Example exports for ~/.bashrc (edit values).
#
export RACKBRAIN_HOME="$HOME/rackbrain"
export RACKBRAIN_CONFIG="$RACKBRAIN_HOME/config/config.yaml"

# Jira auth
export RACKBRAIN_JIRA_PAT="REPLACE_ME"

# Optional DB context enrichment
export RACKBRAIN_DB_HOST="REPLACE_ME"
export RACKBRAIN_DB_USER="REPLACE_ME"
export RACKBRAIN_DB_PASS="REPLACE_ME"
export RACKBRAIN_DB_NAME="hyvetest"

# Optional: override where state/log files go
# export RACKBRAIN_STATE_DIR="$HOME/.local/state/rackbrain"
# export RACKBRAIN_TIMER_DB_PATH="$RACKBRAIN_STATE_DIR/rackbrain_state.sqlite"

