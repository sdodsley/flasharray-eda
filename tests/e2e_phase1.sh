#!/bin/bash
# Phase 1 exit test: change control from the audit log, maintenance-window
# suppression, the git-push webhook and the alert runbook map.
#
#   A=<array> HUMAN_PASSWORD=<pw> ./tests/e2e_phase1.sh
#
# Creates a local user on A (removed at the end) so the out-of-band change
# is made by a different identity from the automation account.
set -u
cd "$(dirname "$0")/.."
A=${A:?array name}
HUMAN_USER=${HUMAN_USER:-eda-human}
HUMAN_PASSWORD=${HUMAN_PASSWORD:?password for the test user}
HUMAN_ENV=$HOME/.flasharray-eda-human.env
OUT=tests/out
mkdir -p "$OUT"
ALERT_LOG=$(awk '/^alert_log:/ {print $2}' vars/local-lab.yml)
BASELINE_DIR=$(awk '/^baseline_dir:/ {print $2}' vars/local-lab.yml)
WEBHOOK_PORT=$(awk '/^webhook_port:/ {print $2}' vars/local-lab.yml)
BASE_NTP=$(awk -F'"' '/^baseline_ntp_servers_csv/ {print $2}' vars/local-lab.yml)
rm -f "$ALERT_LOG"

set -a; source "${FAEDA_ENV:-$HOME/.pure_test_env}"; set +a
source "${FAEDA_VENV:-$HOME/fusion-eda/venv}/bin/activate"
export ANSIBLE_LOCALHOST_WARNING=False
VENV=${FAEDA_VENV:-$HOME/fusion-eda/venv}; COLL=$HOME/.ansible/collections; [ -d "$(dirname "$VENV")/collections" ] && COLL="$(dirname "$VENV")/collections:$COLL"
export ANSIBLE_COLLECTIONS_PATH=${ANSIBLE_COLLECTIONS_PATH:-$COLL}
PB() { ansible-playbook -i inventory/local-lab.yml "$@" 2>&1 | grep -E "changed=|fatal|FAILED" | cut -c1-160; }
summary() { grep -E "polling|Rule:|Action|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR|Terminating|debug\]|->|maintenance|automation" "$OUT/$1" | grep -v "^$" | cut -c1-230 | head -${2:-30}; }

echo "===== 0. create the human user and token"
PB tests/human_user.yml -e target_array="$A" -e human_user="$HUMAN_USER" -e human_password="$HUMAN_PASSWORD" -e human_env_file="$HUMAN_ENV"
set -a; source "$HUMAN_ENV"; set +a

echo "===== 1. change control: start the rulebook, human drifts NTP after 20s, expect reconcile; then a human destroy (90s)"
( sleep 20; PB tests/as_human.yml -e action=ntp_drift -e baseline_ntp_servers_csv="$BASE_NTP"; sleep 25; PB tests/as_human.yml -e action=destroy_volume ) &
timeout 90 ./run.sh extensions/eda/rulebooks/change_control.yml -v > "$OUT/change_control.log" 2>&1
wait
summary change_control.log 40

echo "===== 2. maintenance window: open it, human drifts NTP, expect log-only; close window, restore NTP (75s)"
PB tests/maintenance_window.yml -e target_array="$A" -e window_state=present -e window_seconds=3600
( sleep 25; PB tests/as_human.yml -e action=ntp_drift -e baseline_ntp_servers_csv="$BASE_NTP" ) &
timeout 70 ./run.sh extensions/eda/rulebooks/change_control.yml -v > "$OUT/change_control_window.log" 2>&1
wait
PB tests/maintenance_window.yml -e target_array="$A" -e window_state=absent
summary change_control_window.log 20

echo "===== 3. git push webhook: commit to the baseline repo, POST a push payload, expect apply_baseline to restore NTP (60s)"
( cd "$BASELINE_DIR" && git -c commit.gpgsign=false commit -q --allow-empty -m "e2e push $(date -Is)" )
( sleep 20; curl -s -X POST "http://127.0.0.1:$WEBHOOK_PORT/endpoint" -H 'Content-Type: application/json' \
    -d "{\"ref\":\"refs/heads/main\",\"after\":\"$(git -C "$BASELINE_DIR" rev-parse HEAD)\",\"repository\":{\"name\":\"baseline\"}}" ) &
timeout 60 ./run.sh extensions/eda/rulebooks/change_control.yml -v > "$OUT/webhook.log" 2>&1
wait
summary webhook.log 25

echo "===== 4. alert runbook map: replay four alert fixtures (30s)"
{ cat vars/local-lab.yml; sed "/^---$/d; s/REPLAY_ARRAY/$A/g" tests/fixtures/alerts.yml; } > "$OUT/vars_replay.yml"
FAEDA_VARS="$OUT/vars_replay.yml" timeout 30 ./run.sh tests/replay_alerts.yml -v > "$OUT/replay_alerts.log" 2>&1
summary replay_alerts.log 30

echo "===== 5. remove the human user"
PB tests/human_user.yml -e target_array="$A" -e human_user="$HUMAN_USER" -e human_password="$HUMAN_PASSWORD" -e human_env_file="$HUMAN_ENV" -e user_state=absent
rm -f "$HUMAN_ENV"

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-320
