#!/bin/bash
# Phase 2 exit test: replication actions on real arrays.
#   A=<source array> B=<target array> PGROUP=<replicated pg on A> POD=<stretched pod> ./tests/e2e_phase2.sh
#
#  1. steady state: the replication rulebook with the enriched events (45 s)
#  2. replay: an overdue protection group -> immediate replicated snapshot of
#     PGROUP on A (real); a degraded stretched pod -> preferred arrays set on
#     the test host connected to POD (real), then restored by the recovery event
#  3. DR drill: copy a volume from the latest replicated snapshot of A:PGROUP
#     on B, verify, destroy (real), driven by the tick ruleset
set -u
cd "$(dirname "$0")/.."
A=${A:?source array}; B=${B:?target array}; PGROUP=${PGROUP:?replicated protection group on A}; POD=${POD:?stretched pod}
OUT=tests/out; mkdir -p "$OUT"
ALERT_LOG=$(awk '/^alert_log:/ {print $2}' vars/local-lab.yml)
rm -f "$ALERT_LOG"
set -a; source "${FAEDA_ENV:-$HOME/.pure_test_env}"; set +a
source "${FAEDA_VENV:-$HOME/fusion-eda/venv}/bin/activate"
export ANSIBLE_LOCALHOST_WARNING=False
VENV=${FAEDA_VENV:-$HOME/fusion-eda/venv}; COLL=$HOME/.ansible/collections; [ -d "$(dirname "$VENV")/collections" ] && COLL="$(dirname "$VENV")/collections:$COLL"
export ANSIBLE_COLLECTIONS_PATH=${ANSIBLE_COLLECTIONS_PATH:-$COLL}
PB() { ansible-playbook -i inventory/local-lab.yml "$@" 2>&1 | grep -E "changed=|fatal|FAILED" | cut -c1-160; }
summary() { grep -E "polling|PLAY RECAP|ok=|failed=|msg|poll failed|Traceback|ERROR|Terminating|debug\]|->" "$OUT/$1" | grep -v "^$" | cut -c1-240 | head -${2:-30}; }

echo "===== 1. replication rulebook steady state (45s; the tick ruleset also fires one DR drill)"
timeout 45 ./run.sh extensions/eda/rulebooks/replication.yml -v > "$OUT/replication2.log" 2>&1
summary replication2.log 20

echo "===== 2. replay: overdue pgroup -> replicate now; degraded pod -> preferred arrays on the test host; recovered -> restore (70s)"
PB tests/pod_test_host.yml -e target_array="$A" -e pod="$POD"
{ cat vars/local-lab.yml; sed "/^---$/d; s/REPLAY_ARRAY/$A/g; s/REPLAY_PGROUP/$PGROUP/g; s/REPLAY_POD/$POD/g; s/REPLAY_ONLINE/$A/g; s/REPLAY_OFFLINE/$B/g" tests/fixtures/replication.yml; } > "$OUT/vars_replay.yml"
FAEDA_VARS="$OUT/vars_replay.yml" timeout 70 ./run.sh tests/replay_replication.yml -v > "$OUT/replay_replication.log" 2>&1
summary replay_replication.log 40
echo "-- preferred arrays on the test host now (expect none after restore):"
python3 - <<PY 2>/dev/null
import os,urllib3; urllib3.disable_warnings()
from pypureclient import flasharray
c=flasharray.Client(target=os.environ["PUREFA_URL"].replace("https://",""),api_token=os.environ["PUREFA_API"],verify_ssl=False)
print([(h.name, list(h.preferred_arrays or [])) for h in c.get_hosts(names=["eda-drill-host"]).items])
print("newest pgsnaps:", [s.name for s in c.get_protection_group_snapshots(filter="name='$PGROUP.eda-*'", sort=["created-"], limit=3).items])
PY
PB tests/pod_test_host.yml -e target_array="$A" -e pod="$POD" -e cleanup=true

echo "===== alert log"
cat "$ALERT_LOG" 2>/dev/null | cut -c1-360
