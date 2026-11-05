#!/bin/bash
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Phase 2 exit test: replication actions on real arrays.
#   A=<source array> B=<target array> PGROUP=<replicated pg on A> POD=<stretched pod> ./tests/e2e_phase2.sh
#
#  1. steady state: the replication rulebook with the enriched events (45 s)
#  2. replay: an overdue protection group -> immediate replicated snapshot of
#     PGROUP on A (real); a degraded stretched pod -> preferred arrays set on
#     the test host connected to POD (real), then restored by the recovery event
#  3. DR drill: copy a volume from the latest replicated snapshot of A:PGROUP
#     on B, verify, destroy (real), driven by the tick ruleset
#
# Needs env.sh working and the vars and inventory files named by FAEDA_VARS /
# FAEDA_INVENTORY. Credentials for A come from A_URL_ENV / A_TOKEN_ENV
# (defaults ARRAY_A_URL / ARRAY_A_TOKEN), exported as FA_URL / FA_TOKEN for
# the SDK check. Creates a throwaway host and volume in POD on A (removed on
# any exit), takes a replicated snapshot of PGROUP and a drill copy on B. The
# alert log is tests/out/alerts.log.
set -u
USAGE="usage: A=<source array> B=<target array> PGROUP=<replicated protection group on A> POD=<stretched pod> $0"
for v in A B PGROUP POD; do
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
PB() { ansible-playbook -i "$FAEDA_INVENTORY" "$@" 2>&1 | grep -E "changed=|fatal|FAILED" | cut -c1-160; }
summary() { grep -E "polling|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR|Terminating|debug\]|->" "$OUT/$1" | grep -v "^$" | cut -c1-240 | head -${2:-30}; }

# the test host and volume are removed on any exit
HOST_CREATED=0
cleanup() {
  if [ "$HOST_CREATED" = 1 ]; then PB tests/pod_test_host.yml -e target_array="$A" -e pod="$POD" -e cleanup=true; HOST_CREATED=0; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "===== 1. replication rulebook steady state (45s; the tick ruleset also fires one DR drill)"
timeout 45 ./run.sh extensions/eda/rulebooks/replication.yml -v > "$OUT/replication2.log" 2>&1
summary replication2.log 20

echo "===== 2. replay: overdue pgroup -> replicate now; degraded pod -> preferred arrays on the test host; recovered -> restore (70s)"
HOST_CREATED=1
PB tests/pod_test_host.yml -e target_array="$A" -e pod="$POD"
{ cat "$FAEDA_VARS"; sed "/^---$/d; s/REPLAY_ARRAY/$A/g; s/REPLAY_PGROUP/$PGROUP/g; s/REPLAY_POD/$POD/g; s/REPLAY_ONLINE/$A/g; s/REPLAY_OFFLINE/$B/g" tests/fixtures/replication.yml; } > "$OUT/vars_replay.yml"
FAEDA_VARS="$OUT/vars_replay.yml" timeout 70 ./run.sh tests/replay_replication.yml -v > "$OUT/replay_replication.log" 2>&1
summary replay_replication.log 40
echo "-- preferred arrays on the test host now (expect none after restore):"
python3 - <<PY
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
print([(h.name, list(h.preferred_arrays or [])) for h in c.get_hosts(names=["eda-drill-host"]).items])
print("newest pgsnaps:", [s.name for s in c.get_protection_group_snapshots(filter="name='$PGROUP.eda-*'", sort=["created-"], limit=3).items])
PY
cleanup

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-360
