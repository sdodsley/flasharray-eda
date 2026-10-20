#!/bin/bash
# Phase 0 exit test against two lab arrays. Each step starts a rulebook with
# lab intervals, induces the condition with a test playbook, waits for the
# rule to act, then restores the array. Output goes to tests/out/*.log and
# the alert log is printed at the end.
#
#   A=<array a> B=<array b> ./tests/e2e_phase0.sh
#
# Needs: run.sh working (env file, venv), inventory/local-lab.yml,
# vars/local-lab.yml, a pod replica link A:<pod> -> B:<remote pod>.
set -u
cd "$(dirname "$0")/.."
A=${A:?source array name}
B=${B:?target array name}
POD=${POD:-test}
REMOTE_POD=${REMOTE_POD:-test-rl}
LED_COMPONENT=${LED_COMPONENT:-CT1}
OUT=tests/out
mkdir -p "$OUT"
ALERT_LOG=$(awk '/^alert_log:/ {print $2}' vars/local-lab.yml)
rm -f "$ALERT_LOG"

ENV_FILE=${FAEDA_ENV:-$HOME/.pure_test_env}
set -a; source "$ENV_FILE"; set +a
source "${FAEDA_VENV:-$HOME/fusion-eda/venv}/bin/activate"
export ANSIBLE_LOCALHOST_WARNING=False
PB() { ansible-playbook -i inventory/local-lab.yml "$@" 2>&1 | grep -E "changed=|fatal|FAILED" | cut -c1-160; }
RB() { local secs=$1 log=$2; shift 2; timeout "$secs" ./run.sh "$@" > "$OUT/$log" 2>&1; }
summary() { grep -E "polling|Ruleset:|Rule:|Action:|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR" "$OUT/$1" | grep -v "^$" | cut -c1-220 | head -${2:-25}; }

echo "===== 1. alerts, health, replication, capacity, performance: 45s each, steady state"
for rb in alerts health replication capacity performance; do
  RB 45 "$rb.log" "extensions/eda/rulebooks/$rb.yml" -v
  echo "--- $rb"; summary "$rb.log" 12
done

echo "===== 2. config drift: induce NTP drift, let the rulebook repair it (60s)"
PB tests/induce_ntp_drift.yml -e target_array="$A" -e baseline_ntp_servers_csv="$(awk -F'"' '/^baseline_ntp_servers_csv/ {print $2}' vars/local-lab.yml)"
RB 60 config_drift.log extensions/eda/rulebooks/config_drift.yml -v
summary config_drift.log 20

echo "===== 3. replication: pause the replica link, expect the paused rule, then resume (60s)"
PB tests/replica_link_pause.yml -e target_array="$A" -e pod="$POD" -e remote_array="$B" -e remote_pod="$REMOTE_POD" -e pause=true
RB 60 replication_paused.log extensions/eda/rulebooks/replication.yml -v
PB tests/replica_link_pause.yml -e target_array="$A" -e pod="$POD" -e remote_array="$B" -e remote_pod="$REMOTE_POD" -e pause=false
summary replication_paused.log 20

echo "===== 4. health: open a maintenance window, expect the window to be reported, then close it (45s)"
PB tests/maintenance_window.yml -e target_array="$A" -e window_state=present -e window_seconds=3600
RB 45 health_window.log extensions/eda/rulebooks/health.yml -v
PB tests/maintenance_window.yml -e target_array="$A" -e window_state=absent
summary health_window.log 20

echo "===== 5. health replay: fixture hardware events drive the identify LED on $LED_COMPONENT, then clear it (40s)"
{ cat vars/local-lab.yml; sed "/^---$/d; s/REPLAY_ARRAY/$A/g; s/REPLAY_COMPONENT/$LED_COMPONENT/g" tests/fixtures/hardware_critical.yml; } > "$OUT/vars_replay.yml"
FAEDA_VARS="$OUT/vars_replay.yml" RB 40 replay_health.log tests/replay_health.yml -v
PB tests/identify_led_off.yml -e target_array="$A" -e component="$LED_COMPONENT"
summary replay_health.log 25

echo "===== 6. audits: the drift test above produced audit records; run the audits signal with a 10 minute lookback (30s)"
timeout 30 python extensions/eda/plugins/event_source/purefa_events.py "$A" "$PUREFA_URL" PUREFA_API audits 60 2>/dev/null \
  | python3 -c 'import json,sys
for line in sys.stdin:
    e = json.loads(line)["purefa_audit"]
    print(e["time"], e["user"], e["command"], e["subcommand"], (e["arguments"] or "")[:60])' | tail -8

echo "===== 7. test alert to the configured watchers"
FA_URL="$PUREFA_URL" FA_TOKEN="$PUREFA_API" python3 tests/send_test_alert.py

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-300
