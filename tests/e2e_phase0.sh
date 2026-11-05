#!/bin/bash
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Phase 0 exit test against two lab arrays. Each step starts a rulebook with
# lab intervals, induces the condition with a test playbook, waits for the
# rule to act, then restores the array. Output goes to tests/out/*.log and
# the alert log is printed at the end.
#
#   A=<array a> B=<array b> POD=<pod on A> REMOTE_POD=<replica pod on B> \
#     LED_COMPONENT=<component, e.g. CT1> ./tests/e2e_phase0.sh
#
# Needs env.sh working (env file, venv) and the vars and inventory files
# named by FAEDA_VARS / FAEDA_INVENTORY (defaults vars/local-lab.yml and
# inventory/local-lab.yml), with a pod replica link A:POD -> B:REMOTE_POD.
# Credentials for A come from the environment variables named by A_URL_ENV /
# A_TOKEN_ENV (defaults ARRAY_A_URL / ARRAY_A_TOKEN), exported as FA_URL /
# FA_TOKEN for the SDK calls. Every step runs from tests/out/vars_test.yml, a
# copy of the vars file with alert_log and request_dir under tests/out, so
# the configured alert log is never touched.
# Changes made on A: NTP drift (repaired by the rulebook), the replica link
# paused and resumed, a maintenance window opened and closed, the identify
# LED on LED_COMPONENT lit and cleared, one alert-watcher test message. An
# interrupted run still undoes whatever was still in place.
set -u
USAGE="usage: A=<array a> B=<array b> POD=<pod on A> REMOTE_POD=<replica pod on B> LED_COMPONENT=<component, e.g. CT1> $0"
for v in A B POD REMOTE_POD LED_COMPONENT; do
  [ -n "${!v:-}" ] || { echo "$USAGE" >&2; echo "$v is not set" >&2; exit 2; }
done
cd "$(dirname "$0")/.."
# shellcheck source=../env.sh
. ./env.sh || exit 1
A_URL_ENV=${A_URL_ENV:-ARRAY_A_URL}; A_TOKEN_ENV=${A_TOKEN_ENV:-ARRAY_A_TOKEN}
export FA_URL=${!A_URL_ENV:?$A_URL_ENV is not set (export it, put it in the env file, or name another variable with A_URL_ENV)}
export FA_TOKEN=${!A_TOKEN_ENV:?$A_TOKEN_ENV is not set (export it, put it in the env file, or name another variable with A_TOKEN_ENV)}
[ -f "$FAEDA_VARS" ] && [ -f "$FAEDA_INVENTORY" ] || { echo "need $FAEDA_VARS and $FAEDA_INVENTORY (copy vars/example.yml and inventory/example.yml, or set FAEDA_VARS / FAEDA_INVENTORY)" >&2; exit 1; }
OUT=tests/out
mkdir -p "$OUT" "$OUT/requests"
# every step starts from a copy of the vars file that keeps the alert log and
# the request files under tests/out
ALERT_LOG=$PWD/$OUT/alerts.log
{ grep -v -E '^(alert_log|request_dir):' "$FAEDA_VARS"; echo "alert_log: $ALERT_LOG"; echo "request_dir: $PWD/$OUT/requests"; } > "$OUT/vars_test.yml"
export FAEDA_VARS=$OUT/vars_test.yml
rm -f "$ALERT_LOG"
cfg() { python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))[sys.argv[2]])' "$FAEDA_VARS" "$1"; }
PB() { ansible-playbook -i "$FAEDA_INVENTORY" "$@" 2>&1 | grep -E "changed=|fatal|FAILED" | cut -c1-160; }
RB() { local secs=$1 log=$2; shift 2; timeout "$secs" ./run.sh "$@" > "$OUT/$log" 2>&1; }
summary() { grep -E "polling|Ruleset:|Rule:|Action:|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR" "$OUT/$1" | grep -v "^$" | cut -c1-220 | head -${2:-25}; }
BASE_NTP=$(cfg baseline_ntp_servers_csv)

# Each induced condition stays flagged until it is undone. restore runs after
# the step and again from the EXIT trap, so Ctrl-C, SIGTERM or a dropped
# session leaves A as the script found it (the helpers are idempotent).
NTP_DRIFTED=0; LINK_PAUSED=0; WINDOW_OPEN=0; LED_ON=0
restore() {
  if [ "$NTP_DRIFTED" = 1 ]; then PB playbooks/remediate_ntp.yml -e target_array="$A" -e baseline_ntp_servers_csv="$BASE_NTP" -e alert_log="$ALERT_LOG"; NTP_DRIFTED=0; fi
  if [ "$LINK_PAUSED" = 1 ]; then PB tests/replica_link_pause.yml -e target_array="$A" -e pod="$POD" -e remote_array="$B" -e remote_pod="$REMOTE_POD" -e pause=false; LINK_PAUSED=0; fi
  if [ "$WINDOW_OPEN" = 1 ]; then PB tests/maintenance_window.yml -e target_array="$A" -e window_state=absent; WINDOW_OPEN=0; fi
  if [ "$LED_ON" = 1 ]; then PB tests/identify_led_off.yml -e target_array="$A" -e component="$LED_COMPONENT"; LED_ON=0; fi
}
trap restore EXIT
trap 'exit 130' INT TERM

echo "===== 1. alerts, health, replication, capacity, performance: 45s each, steady state"
for rb in alerts health replication capacity performance; do
  RB 45 "$rb.log" "extensions/eda/rulebooks/$rb.yml" -v
  echo "--- $rb"; summary "$rb.log" 12
done

echo "===== 2. config drift: induce NTP drift, let the rulebook repair it (60s)"
NTP_DRIFTED=1
PB tests/induce_ntp_drift.yml -e target_array="$A" -e baseline_ntp_servers_csv="$BASE_NTP"
RB 60 config_drift.log extensions/eda/rulebooks/config_drift.yml -v
summary config_drift.log 20

echo "===== 3. replication: pause the replica link, expect the paused rule, then resume (60s)"
LINK_PAUSED=1
PB tests/replica_link_pause.yml -e target_array="$A" -e pod="$POD" -e remote_array="$B" -e remote_pod="$REMOTE_POD" -e pause=true
RB 60 replication_paused.log extensions/eda/rulebooks/replication.yml -v
restore
summary replication_paused.log 20

echo "===== 4. health: open a maintenance window, expect the window to be reported, then close it (45s)"
WINDOW_OPEN=1
PB tests/maintenance_window.yml -e target_array="$A" -e window_state=present -e window_seconds=3600
RB 45 health_window.log extensions/eda/rulebooks/health.yml -v
restore
summary health_window.log 20

echo "===== 5. health replay: fixture hardware events drive the identify LED on $LED_COMPONENT, then clear it (40s)"
{ cat "$FAEDA_VARS"; sed "/^---$/d; s/REPLAY_ARRAY/$A/g; s/REPLAY_COMPONENT/$LED_COMPONENT/g" tests/fixtures/hardware_critical.yml; } > "$OUT/vars_replay.yml"
LED_ON=1
FAEDA_VARS="$OUT/vars_replay.yml" RB 40 replay_health.log tests/replay_health.yml -v
restore
summary replay_health.log 25

echo "===== 6. audits: the drift test above produced audit records; run the audits signal with a 10 minute lookback (30s; plugin log in $OUT/audits_cli.log)"
timeout 30 python3 extensions/eda/plugins/event_source/purefa_events.py "$A" "$FA_URL" FA_TOKEN audits 60 2> "$OUT/audits_cli.log" \
  | python3 -c 'import json, sys
for line in sys.stdin:
    e = json.loads(line)
    if "purefa_poll_error" in e:
        print("poll error:", e["purefa_poll_error"].get("error")); continue
    a = e.get("purefa_audit")
    if a:
        print(a["time"], a["user"], a["command"], a["subcommand"], (a["arguments"] or "")[:60])' | tail -8

echo "===== 7. test alert to the configured watchers"
python3 tests/send_test_alert.py

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-300
