#!/bin/bash
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Phase 1 exit test: change control from the audit log, maintenance-window
# suppression, the git-push webhook and the alert runbook map.
#
#   A=<array> HUMAN_PASSWORD=<password for the test user> ./tests/e2e_phase1.sh
#   (HUMAN_USER names the test user, default eda-human)
#
# Needs env.sh working, the vars and inventory files named by FAEDA_VARS /
# FAEDA_INVENTORY, a git checkout at baseline_dir, and GIT_WEBHOOK_SECRET in
# the environment (env file): run.sh hands it to change_control.yml and step
# 3 signs its push payload with it. Credentials for A come from A_URL_ENV /
# A_TOKEN_ENV (defaults ARRAY_A_URL / ARRAY_A_TOKEN) as in the other phases.
# Creates a local array_admin user HUMAN_USER on A and mints a token for it
# (tests/out/human.env, mode 0600) so the out-of-band change is made by a
# different identity from the automation account; the user and the file are
# removed on any exit, including Ctrl-C. Also on A: NTP drifted by the human
# (reconcile and apply_baseline put it back), a maintenance window opened and
# closed, a scratch volume created and eradicated; an empty commit is added
# to the baseline checkout. The alert log is tests/out/alerts.log.
set -u
USAGE="usage: A=<array> HUMAN_PASSWORD=<password for the test user> [HUMAN_USER=eda-human] $0"
for v in A HUMAN_PASSWORD; do
  [ -n "${!v:-}" ] || { echo "$USAGE" >&2; echo "$v is not set" >&2; exit 2; }
done
HUMAN_USER=${HUMAN_USER:-eda-human}
cd "$(dirname "$0")/.."
# shellcheck source=../env.sh
. ./env.sh || exit 1
: "${GIT_WEBHOOK_SECRET:?GIT_WEBHOOK_SECRET is not set (put it in the env file; change_control.yml verifies the push webhook with it)}"
A_URL_ENV=${A_URL_ENV:-ARRAY_A_URL}; A_TOKEN_ENV=${A_TOKEN_ENV:-ARRAY_A_TOKEN}
# the inventory reads the same variables; failing here beats a half-run test
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
summary() { grep -E "polling|Rule:|Action|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR|Terminating|debug\]|->|maintenance|automation" "$OUT/$1" | grep -v "^$" | cut -c1-230 | head -${2:-30}; }
HUMAN_ENV=$PWD/$OUT/human.env
BASELINE_DIR=$(cfg baseline_dir)
WEBHOOK_PORT=$(cfg webhook_port)
BASE_NTP=$(cfg baseline_ntp_servers_csv)

# The human user and its token file go away on any exit, and a maintenance
# window left open by an interrupted step 2 is closed.
WINDOW_OPEN=0
cleanup() {
  if [ "$WINDOW_OPEN" = 1 ]; then PB tests/maintenance_window.yml -e target_array="$A" -e window_state=absent; WINDOW_OPEN=0; fi
  echo "===== 5. remove the human user (exit trap)"
  PB tests/human_user.yml -e target_array="$A" -e human_user="$HUMAN_USER" -e human_password="$HUMAN_PASSWORD" -e human_env_file="$HUMAN_ENV" -e user_state=absent
  rm -f "$HUMAN_ENV"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "===== 0. create the human user and token"
PB tests/human_user.yml -e target_array="$A" -e human_user="$HUMAN_USER" -e human_password="$HUMAN_PASSWORD" -e human_env_file="$HUMAN_ENV"
[ -f "$HUMAN_ENV" ] || { echo "no token file written for $HUMAN_USER; stopping" >&2; exit 1; }
set -a; source "$HUMAN_ENV"; set +a

echo "===== 1. change control: start the rulebook, human drifts NTP after 20s, expect reconcile; then a human destroy (90s)"
( sleep 20; PB tests/as_human.yml -e action=ntp_drift -e baseline_ntp_servers_csv="$BASE_NTP"; sleep 25; PB tests/as_human.yml -e action=destroy_volume ) &
timeout 90 ./run.sh extensions/eda/rulebooks/change_control.yml -v > "$OUT/change_control.log" 2>&1
wait
summary change_control.log 40

echo "===== 2. maintenance window: open it, human drifts NTP, expect log-only; close window, restore NTP (75s)"
WINDOW_OPEN=1
PB tests/maintenance_window.yml -e target_array="$A" -e window_state=present -e window_seconds=3600
( sleep 25; PB tests/as_human.yml -e action=ntp_drift -e baseline_ntp_servers_csv="$BASE_NTP" ) &
timeout 70 ./run.sh extensions/eda/rulebooks/change_control.yml -v > "$OUT/change_control_window.log" 2>&1
wait
PB tests/maintenance_window.yml -e target_array="$A" -e window_state=absent; WINDOW_OPEN=0
summary change_control_window.log 20

echo "===== 3. git push webhook: commit to the baseline repo, POST a signed push payload, expect apply_baseline to restore NTP (60s)"
( cd "$BASELINE_DIR" && git -c commit.gpgsign=false commit -q --allow-empty -m "e2e push $(date -Is)" )
BODY="{\"ref\":\"refs/heads/main\",\"after\":\"$(git -C "$BASELINE_DIR" rev-parse HEAD)\",\"repository\":{\"name\":\"baseline\"}}"
# GitHub's signature: hex HMAC-SHA256 of the exact body, sent as sha256=<hex>
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GIT_WEBHOOK_SECRET" | awk '{print $NF}')
( sleep 20; curl -s -X POST "http://127.0.0.1:$WEBHOOK_PORT/endpoint" -H 'Content-Type: application/json' \
    -H "X-Hub-Signature-256: sha256=$SIG" --data-binary "$BODY" ) &
timeout 60 ./run.sh extensions/eda/rulebooks/change_control.yml -v > "$OUT/webhook.log" 2>&1
wait
summary webhook.log 25

echo "===== 4. alert runbook map: replay four alert fixtures (30s)"
{ cat "$FAEDA_VARS"; sed "/^---$/d; s/REPLAY_ARRAY/$A/g" tests/fixtures/alerts.yml; } > "$OUT/vars_replay.yml"
FAEDA_VARS="$OUT/vars_replay.yml" timeout 30 ./run.sh tests/replay_alerts.yml -v > "$OUT/replay_alerts.log" 2>&1
summary replay_alerts.log 30

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-320
