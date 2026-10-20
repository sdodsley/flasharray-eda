#!/bin/bash
# Phase 3 exit test: capacity approval gate, QoS cap with hysteresis, the
# composite anomaly rule. All actions run on the real array.
#   A=<array> VOLUME=<busy volume on A> ./tests/e2e_phase3.sh
#
#  1. approval gate: a throwaway volume is created and destroyed (pending
#     eradication); the capacity rulebook runs with a 1 % risk threshold so
#     it raises a request; the request is approved over the webhook with
#     action eradicate_pending; the volume is gone.
#  2. QoS: the performance rulebook runs with thresholds low enough for VOLUME
#     to count as a hot noisy neighbour -> cap applied + tag; then with
#     thresholds high -> normal polls -> cap lifted, tag removed.
#  3. anomaly: three replayed signals -> protect_now snapshots every local
#     protection group and lengthens the eradication delay; the delay is put
#     back afterwards.
set -u
cd "$(dirname "$0")/.."
A=${A:?array}; VOLUME=${VOLUME:?a volume with some IOPS on A}
OUT=tests/out; mkdir -p "$OUT" "$OUT/requests"
ALERT_LOG=$(awk '/^alert_log:/ {print $2}' vars/local-lab.yml)
APPROVAL_PORT=$(awk '/^approval_port:/ {print $2}' vars/local-lab.yml)
rm -f "$ALERT_LOG" "$OUT"/requests/*.json
set -a; source "${FAEDA_ENV:-$HOME/.pure_test_env}"; set +a
source "${FAEDA_VENV:-$HOME/fusion-eda/venv}/bin/activate"
export ANSIBLE_LOCALHOST_WARNING=False
VENV=${FAEDA_VENV:-$HOME/fusion-eda/venv}; COLL=$HOME/.ansible/collections; [ -d "$(dirname "$VENV")/collections" ] && COLL="$(dirname "$VENV")/collections:$COLL"
export ANSIBLE_COLLECTIONS_PATH=${ANSIBLE_COLLECTIONS_PATH:-$COLL}
PB() { ansible-playbook -i inventory/local-lab.yml "$@" 2>&1 | grep -E "changed=|fatal|FAILED" | cut -c1-160; }
summary() { grep -E "polling|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR|Terminating|debug\]|request|QoS|PROTECT" "$OUT/$1" | grep -v "^$" | cut -c1-240 | head -${2:-30}; }
SDK() { python3 - "$@" 2>/dev/null; }

echo "===== 1. approval gate (90s)"
SDK <<'PY'
import os,urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c=flasharray.Client(target=os.environ["PUREFA_URL"].replace("https://",""),api_token=os.environ["PUREFA_API"],verify_ssl=False)
c.post_volumes(names=["eda-reclaim-test"], volume=flasharray.VolumePost(provisioned=1073741824), with_default_protection=False)
c.patch_volumes(names=["eda-reclaim-test"], volume=flasharray.VolumePatch(destroyed=True))
print("pending eradication:", [v.name for v in c.get_volumes(destroyed=True).items])
PY
sed 's/^capacity_risk_pct: .*/capacity_risk_pct: 1/' vars/local-lab.yml > "$OUT/vars_cap.yml"
( sleep 45
  REQ=$(ls -t "$OUT"/requests/*.json 2>/dev/null | head -1)
  if [ -n "$REQ" ]; then
    ID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['request_id'])" "$REQ")
    echo "-- approving $ID"
    curl -s -X POST "http://127.0.0.1:$APPROVAL_PORT/endpoint" -H 'Content-Type: application/json' \
      -d "{\"request_id\":\"$ID\",\"approve\":true,\"approver\":\"e2e\",\"action\":\"eradicate_pending\"}"
  else echo "-- no request file written"; fi ) &
FAEDA_VARS="$OUT/vars_cap.yml" timeout 90 ./run.sh extensions/eda/rulebooks/capacity.yml -v > "$OUT/capacity3.log" 2>&1
wait
summary capacity3.log 30
SDK <<'PY'
import os,urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c=flasharray.Client(target=os.environ["PUREFA_URL"].replace("https://",""),api_token=os.environ["PUREFA_API"],verify_ssl=False)
print("pending eradication now:", [v.name for v in c.get_volumes(destroyed=True).items])
PY

echo "===== 2. QoS cap (70s) then lift (70s) on $VOLUME"
sed -e 's/^hotspot_latency_usec: .*/hotspot_latency_usec: 50/' -e 's/^array_hot_latency_usec: .*/array_hot_latency_usec: 50/' \
    -e 's/^hotspot_consecutive_polls: .*/hotspot_consecutive_polls: 2/' -e 's/^qos_cap_min_iops_share_pct: .*/qos_cap_min_iops_share_pct: 10/' \
    -e 's/^telemetry_interval: .*/telemetry_interval: 15/' vars/local-lab.yml > "$OUT/vars_qos_cap.yml"
FAEDA_VARS="$OUT/vars_qos_cap.yml" timeout 70 ./run.sh extensions/eda/rulebooks/performance.yml -v > "$OUT/qos_cap.log" 2>&1
summary qos_cap.log 20
SDK <<PY
import os,urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c=flasharray.Client(target=os.environ["PUREFA_URL"].replace("https://",""),api_token=os.environ["PUREFA_API"],verify_ssl=False)
v=list(c.get_volumes(names=["$VOLUME"]).items)[0]
print("after cap: iops_limit", v.qos.iops_limit if v.qos else None, "tags", {t.key:t.value for t in c.get_volumes_tags(resource_names=["$VOLUME"], namespaces=["eda"]).items})
PY
sed -e 's/^hotspot_latency_usec: .*/hotspot_latency_usec: 900000/' -e 's/^qos_lift_normal_polls: .*/qos_lift_normal_polls: 2/' \
    -e 's/^telemetry_interval: .*/telemetry_interval: 15/' vars/local-lab.yml > "$OUT/vars_qos_lift.yml"
FAEDA_VARS="$OUT/vars_qos_lift.yml" timeout 70 ./run.sh extensions/eda/rulebooks/performance.yml -v > "$OUT/qos_lift.log" 2>&1
summary qos_lift.log 20
SDK <<PY
import os,urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c=flasharray.Client(target=os.environ["PUREFA_URL"].replace("https://",""),api_token=os.environ["PUREFA_API"],verify_ssl=False)
v=list(c.get_volumes(names=["$VOLUME"]).items)[0]
print("after lift: iops_limit", v.qos.iops_limit if v.qos else None, "tags", {t.key:t.value for t in c.get_volumes_tags(resource_names=["$VOLUME"], namespaces=["eda"]).items})
PY

echo "===== 3. anomaly composite rule (40s)"
BEFORE=$(SDK <<'PY'
import os,urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c=flasharray.Client(target=os.environ["PUREFA_URL"].replace("https://",""),api_token=os.environ["PUREFA_API"],verify_ssl=False)
print(list(c.get_arrays().items)[0].eradication_config.enabled_delay)
PY
)
echo "-- eradication enabled_delay before: $BEFORE ms"
{ cat vars/local-lab.yml; sed "/^---$/d; s/REPLAY_ARRAY/$A/g" tests/fixtures/anomaly.yml; } > "$OUT/vars_replay.yml"
FAEDA_VARS="$OUT/vars_replay.yml" timeout 40 ./run.sh tests/replay_anomaly.yml -v > "$OUT/replay_anomaly.log" 2>&1
summary replay_anomaly.log 30
SDK <<PY
import os,urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c=flasharray.Client(target=os.environ["PUREFA_URL"].replace("https://",""),api_token=os.environ["PUREFA_API"],verify_ssl=False)
a=list(c.get_arrays().items)[0]
print("eradication enabled_delay after:", a.eradication_config.enabled_delay)
print("protect snapshots:", [s.name for s in c.get_protection_group_snapshots(filter="name='*.eda-protect-*'", limit=10).items])
# put the delay back and remove the test snapshots
r=c.patch_arrays(array=flasharray.Arrays(eradication_config=flasharray.EradicationConfig(eradication_delay=int("$BEFORE"))))
print("restore delay ->", r.status_code, getattr(r,'errors',None) and r.errors[0].message)
for s in c.get_protection_group_snapshots(filter="name='*.eda-protect-*'", limit=50).items:
    c.patch_protection_group_snapshots(names=[s.name], protection_group_snapshot=flasharray.ProtectionGroupSnapshotPatch(destroyed=True))
    c.delete_protection_group_snapshots(names=[s.name])
print("protect snapshots after cleanup:", [s.name for s in c.get_protection_group_snapshots(filter="name='*.eda-protect-*'", limit=10).items])
PY

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-360
