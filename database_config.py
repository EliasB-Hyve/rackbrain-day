import os

# Prefer per-user environment variables (ex: exports in ~/.bashrc):
#   export RACKBRAIN_DB_HOST="..."
#   export RACKBRAIN_DB_USER="..."
#   export RACKBRAIN_DB_PASS="..."
#   export RACKBRAIN_DB_NAME="hyvetest"

host = os.environ.get("RACKBRAIN_DB_HOST", "").strip()
user = os.environ.get("RACKBRAIN_DB_USER", "").strip()
passwd = os.environ.get("RACKBRAIN_DB_PASS", "").strip()
db = os.environ.get("RACKBRAIN_DB_NAME", "hyvetest").strip()
