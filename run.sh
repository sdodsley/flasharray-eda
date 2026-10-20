#!/bin/bash
# Launch one rulebook.
#   ./run.sh extensions/eda/rulebooks/alerts.yml
#   ./run.sh extensions/eda/rulebooks/config_drift.yml -v --print-events
# Environment:
#   FAEDA_ENV        file sourced for array URLs/tokens (default ~/.pure_test_env)
#   FAEDA_VENV       virtualenv with ansible-rulebook + py-pure-client
#   FAEDA_VARS       vars file (default vars/local-lab.yml)
#   FAEDA_INVENTORY  inventory file (default inventory/local-lab.yml)
set -eu
cd "$(dirname "$0")"

ENV_FILE=${FAEDA_ENV:-$HOME/.pure_test_env}
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

VENV=${FAEDA_VENV:-$HOME/fusion-eda/venv}
# shellcheck disable=SC1091
source "$VENV/bin/activate"
export ANSIBLE_COLLECTIONS_PATH=${ANSIBLE_COLLECTIONS_PATH:-$HOME/.ansible/collections}
export ANSIBLE_LOCALHOST_WARNING=False

RULEBOOK=${1:-extensions/eda/rulebooks/alerts.yml}
shift || true

exec ansible-rulebook \
  --rulebook "$RULEBOOK" \
  --inventory "${FAEDA_INVENTORY:-inventory/local-lab.yml}" \
  --vars "${FAEDA_VARS:-vars/local-lab.yml}" \
  --source-dir extensions/eda/plugins/event_source \
  "$@"
