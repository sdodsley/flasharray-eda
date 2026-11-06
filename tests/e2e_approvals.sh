#!/bin/bash
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end test: capacity approval gate, QoS cap with hysteresis, the
# composite anomaly rule. All actions run on the real array.
#   A=<array> VOLUME=<busy volume on A> ./tests/e2e_approvals.sh
#
#  1. approval gate: a throwaway volume is created and destroyed (pending
#     eradication); the capacity rulebook runs with a 1 % risk threshold so
#     it raises a request; the request is approved over the webhook (bearer
#     APPROVAL_TOKEN) with action eradicate_pending; the volume is gone.
#     WARNING: eradicate_pending eradicates EVERY pending-eradication volume
#     and volume snapshot on A, not just the scratch volume. The script
#     refuses to start if any exist, unless E2E_ERADICATE_ALL=1.
#  2. QoS: the performance rulebook runs with thresholds low enough for VOLUME
#     to count as a hot noisy neighbour -> cap applied + tag; then with
#     thresholds high -> normal polls -> cap lifted, tag removed.
#  3. anomaly: three replayed signals -> protect_now snapshots every local
#     protection group and lengthens the eradication delay; the delay is put
#     back afterwards (from the exit trap too, if the run is interrupted) and
#     the test snapshots are eradicated.
#
# Needs env.sh working, the vars and inventory files named by FAEDA_VARS /
# FAEDA_INVENTORY, and APPROVAL_TOKEN in the environment (env file): run.sh
# hands it to capacity.yml and step 1 approves with it. Credentials for A come
# from A_URL_ENV / A_TOKEN_ENV (defaults ARRAY_A_URL / ARRAY_A_TOKEN), exported
# as FA_URL / FA_TOKEN for the SDK calls. The alert log and the request files
# live under tests/out.
set -u
USAGE="usage: A=<array> VOLUME=<a volume with some IOPS on A> [E2E_ERADICATE_ALL=1] $0"
for v in A VOLUME; do
  [ -n "${!v:-}" ] || { echo "$USAGE" >&2; echo "$v is not set" >&2; exit 2; }
done
cd "$(dirname "$0")/.."
# shellcheck source=../env.sh
. ./env.sh || exit 1
: "${APPROVAL_TOKEN:?APPROVAL_TOKEN is not set (put it in the env file; capacity.yml requires it on the approvals webhook)}"
A_URL_ENV=${A_URL_ENV:-ARRAY_A_URL}; A_TOKEN_ENV=${A_TOKEN_ENV:-ARRAY_A_TOKEN}
export FA_URL=${!A_URL_ENV:?$A_URL_ENV is not set (export it, put it in the env file, or name another variable with A_URL_ENV)}
export FA_TOKEN=${!A_TOKEN_ENV:?$A_TOKEN_ENV is not set (export it, put it in the env file, or name another variable with A_TOKEN_ENV)}
[ -f "$FAEDA_VARS" ] && [ -f "$FAEDA_INVENTORY" ] || { echo "need $FAEDA_VARS and $FAEDA_INVENTORY (copy vars/example.yml and inventory/example.yml, or set FAEDA_VARS / FAEDA_INVENTORY)" >&2; exit 1; }
OUT=tests/out
mkdir -p "$OUT" "$OUT/requests"
# every step starts from a copy of the vars file that keeps the alert log and
# the request files under tests/out, so the approval step can find the request
ALERT_LOG=$PWD/$OUT/alerts.log
{ grep -v -E '^(alert_log|request_dir):' "$FAEDA_VARS"; echo "alert_log: $ALERT_LOG"; echo "request_dir: $PWD/$OUT/requests"; } > "$OUT/vars_test.yml"
export FAEDA_VARS=$OUT/vars_test.yml
rm -f "$ALERT_LOG" "$OUT"/requests/*.json
cfg() { python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))[sys.argv[2]])' "$FAEDA_VARS" "$1"; }
PB() { ansible-playbook -i "$FAEDA_INVENTORY" "$@" 2>&1 | grep -E "changed=|fatal|FAILED" | cut -c1-160; }
summary() { grep -E "polling|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR|Terminating|debug\]|request|QoS|PROTECT" "$OUT/$1" | grep -v "^$" | cut -c1-240 | head -${2:-30}; }
SDK() { python3 - "$@"; }   # stderr stays visible: a failed restore must show
APPROVAL_PORT=$(cfg approval_port)
trap 'exit 130' INT TERM

echo "===== 1. approval gate (90s)"
# refuse to run if eradicate_pending would take anything but the scratch volume
SDK <<'PY' || exit 1
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
pend = [v.name for v in c.get_volumes(destroyed=True).items]
snaps = [s.name for s in c.get_volume_snapshots(destroyed=True).items]
if (pend or snaps) and os.environ.get("E2E_ERADICATE_ALL") != "1":
    print("ABORT: eradicate_pending would also eradicate", pend + snaps, "- set E2E_ERADICATE_ALL=1 to accept that")
    raise SystemExit(1)
PY
SDK <<'PY'
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
c.post_volumes(names=["eda-reclaim-test"], volume=flasharray.VolumePost(provisioned=1073741824), with_default_protection=False)
c.patch_volumes(names=["eda-reclaim-test"], volume=flasharray.VolumePatch(destroyed=True))
print("pending eradication:", [v.name for v in c.get_volumes(destroyed=True).items])
PY
sed 's/^capacity_risk_pct: .*/capacity_risk_pct: 1/' "$FAEDA_VARS" > "$OUT/vars_cap.yml"
( sleep 45
  REQ=$(ls -t "$OUT"/requests/*.json 2>/dev/null | head -1)
  if [ -n "$REQ" ]; then
    ID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['request_id'])" "$REQ")
    echo "-- approving $ID"
    curl -s -X POST "http://127.0.0.1:$APPROVAL_PORT/endpoint" -H 'Content-Type: application/json' \
      -H "Authorization: Bearer $APPROVAL_TOKEN" \
      -d "{\"request_id\":\"$ID\",\"approve\":true,\"approver\":\"e2e\",\"action\":\"eradicate_pending\"}"
  else echo "-- no request file written"; fi ) &
FAEDA_VARS="$OUT/vars_cap.yml" timeout 90 ./run.sh extensions/eda/rulebooks/capacity.yml -v > "$OUT/capacity3.log" 2>&1
wait
summary capacity3.log 30
SDK <<'PY'
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
print("pending eradication now:", [v.name for v in c.get_volumes(destroyed=True).items])
PY

echo "===== 2. QoS cap (70s) then lift (70s) on $VOLUME"
sed -e 's/^hotspot_latency_usec: .*/hotspot_latency_usec: 50/' -e 's/^array_hot_latency_usec: .*/array_hot_latency_usec: 50/' \
    -e 's/^hotspot_consecutive_polls: .*/hotspot_consecutive_polls: 2/' -e 's/^qos_cap_min_iops_share_pct: .*/qos_cap_min_iops_share_pct: 10/' \
    -e 's/^telemetry_interval: .*/telemetry_interval: 15/' "$FAEDA_VARS" > "$OUT/vars_qos_cap.yml"
FAEDA_VARS="$OUT/vars_qos_cap.yml" timeout 70 ./run.sh extensions/eda/rulebooks/performance.yml -v > "$OUT/qos_cap.log" 2>&1
summary qos_cap.log 20
SDK <<PY
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
v = list(c.get_volumes(names=["$VOLUME"]).items)[0]
print("after cap: iops_limit", v.qos.iops_limit if v.qos else None, "tags", {t.key: t.value for t in c.get_volumes_tags(resource_names=["$VOLUME"], namespaces=["eda"]).items})
PY
sed -e 's/^hotspot_latency_usec: .*/hotspot_latency_usec: 900000/' -e 's/^qos_lift_normal_polls: .*/qos_lift_normal_polls: 2/' \
    -e 's/^telemetry_interval: .*/telemetry_interval: 15/' "$FAEDA_VARS" > "$OUT/vars_qos_lift.yml"
FAEDA_VARS="$OUT/vars_qos_lift.yml" timeout 70 ./run.sh extensions/eda/rulebooks/performance.yml -v > "$OUT/qos_lift.log" 2>&1
summary qos_lift.log 20
SDK <<PY
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
v = list(c.get_volumes(names=["$VOLUME"]).items)[0]
print("after lift: iops_limit", v.qos.iops_limit if v.qos else None, "tags", {t.key: t.value for t in c.get_volumes_tags(resource_names=["$VOLUME"], namespaces=["eda"]).items})
PY

echo "===== 3. anomaly composite rule (40s)"
BEFORE=$(SDK <<'PY'
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
print(list(c.get_arrays().items)[0].eradication_config.enabled_delay)
PY
)
case "$BEFORE" in
  ''|*[!0-9]*) echo "-- could not read the eradication delay ('$BEFORE'); not running the anomaly step, which changes it" >&2; exit 1 ;;
esac
echo "-- eradication enabled_delay before: $BEFORE ms"
# the delay goes back from the EXIT trap as well, so an interrupted run does
# not leave it lengthened
DELAY_CHANGED=1
restore_delay() {
  [ "$DELAY_CHANGED" = 1 ] || return 0
  SDK <<PY
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
r = c.patch_arrays(array=flasharray.Arrays(eradication_config=flasharray.EradicationConfig(eradication_delay=int("$BEFORE"))))
print("restore delay ->", r.status_code, getattr(r, 'errors', None) and r.errors[0].message)
PY
  DELAY_CHANGED=0
}
trap restore_delay EXIT
{ cat "$FAEDA_VARS"; sed "/^---$/d; s/REPLAY_ARRAY/$A/g" tests/fixtures/anomaly.yml; } > "$OUT/vars_replay.yml"
FAEDA_VARS="$OUT/vars_replay.yml" timeout 40 ./run.sh tests/replay_anomaly.yml -v > "$OUT/replay_anomaly.log" 2>&1
summary replay_anomaly.log 30
SDK <<'PY'
import os, urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", "").strip("/"), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
a = list(c.get_arrays().items)[0]
print("eradication enabled_delay after:", a.eradication_config.enabled_delay)
print("protect snapshots:", [s.name for s in c.get_protection_group_snapshots(filter="name='*.eda-protect-*'", limit=10).items])
# remove the test snapshots
for s in c.get_protection_group_snapshots(filter="name='*.eda-protect-*'", limit=50).items:
    c.patch_protection_group_snapshots(names=[s.name], protection_group_snapshot=flasharray.ProtectionGroupSnapshotPatch(destroyed=True))
    c.delete_protection_group_snapshots(names=[s.name])
print("protect snapshots after cleanup:", [s.name for s in c.get_protection_group_snapshots(filter="name='*.eda-protect-*'", limit=10).items])
PY
restore_delay

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-360
