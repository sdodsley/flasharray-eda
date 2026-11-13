#!/bin/bash
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The run-twice contract on real arrays: every template here is launched
# twice with the same extra vars, and the second run must leave the array as
# it is, report changed=false and say so in its record. Steps:
#  1. expand volume: a scratch volume grown to 2G twice (second: already at size)
#  2. replicate now: PGROUP snapshotted and replicated twice (second: inside
#     the cooldown, nothing requested)
#  3. protect now: every local group snapshotted twice with the eradication
#     delay left alone (second: repeat inside the cooldown, nothing taken)
#  4. capacity request twice with one request_key (second: already raised, the
#     same id); reclaim twice when the only pending object is the scratch
#     volume (second: already done)
#  5. Alertmanager silence: create twice (second reuses the id), expire twice
#     (second: already expired); skipped without ALERTMANAGER_URL
#  6. DR switchover: with RUN_SWITCHOVER=1, SOURCE_POD on A to TARGET_POD on B
#     twice (second: nothing to do), then back twice
# Everything the test creates is destroyed and eradicated at the end. Needs
# env.sh working, FAEDA_VARS / FAEDA_INVENTORY, and A's credentials in the env
# file (A_URL_ENV / A_TOKEN_ENV, defaults ARRAY_A_URL / ARRAY_A_TOKEN).
#   A=<array> PGROUP=<replicated protection group on A> [ALERTMANAGER_URL=http://host:9093] \
#   [RUN_SWITCHOVER=1 B=<array> SOURCE_POD=<pod on A> TARGET_POD=<pod on B>] ./tests/e2e_run_twice.sh
set -u
USAGE="usage: A=<array> PGROUP=<replicated protection group on A> [ALERTMANAGER_URL=...] [RUN_SWITCHOVER=1 B=... SOURCE_POD=... TARGET_POD=...] $0"
for v in A PGROUP; do
  [ -n "${!v:-}" ] || { echo "$USAGE" >&2; echo "$v is not set" >&2; exit 2; }
done
cd "$(dirname "$0")/.."
# shellcheck source=../env.sh
. ./env.sh || exit 1
A_URL_ENV=${A_URL_ENV:-ARRAY_A_URL}; A_TOKEN_ENV=${A_TOKEN_ENV:-ARRAY_A_TOKEN}
export FA_URL=${!A_URL_ENV:?$A_URL_ENV is not set}
export FA_TOKEN=${!A_TOKEN_ENV:?$A_TOKEN_ENV is not set}
[ -f "$FAEDA_VARS" ] && [ -f "$FAEDA_INVENTORY" ] || { echo "need $FAEDA_VARS and $FAEDA_INVENTORY" >&2; exit 1; }
OUT=tests/out
mkdir -p "$OUT" "$OUT/requests_twice"
ALERT_LOG=$PWD/$OUT/alerts_twice.log
REQUEST_DIR=$PWD/$OUT/requests_twice
rm -f "$ALERT_LOG" "$REQUEST_DIR"/*.json
VOL=eda-runtwice-vol
RECLAIM_VOL=eda-runtwice-reclaim
T0=$(date -u +%s)
FAIL=0
PB() { ansible-playbook -i "$FAEDA_INVENTORY" -e "alert_log=$ALERT_LOG" "$@" 2>&1 | grep -E "changed=|fatal|FAILED|msg" | cut -c1-200; }
rec() { python3 - "$1" "$2" <<'PY'
import json, sys
kind, field = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(sys.argv[0] if False else "tests/out/alerts_twice.log")]
rows = [r for r in rows if r.get("kind") == kind]
print(json.dumps(rows[-1].get(field)) if rows else "missing")
PY
}
check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then echo "  OK   $1 = $2"; else echo "  FAIL $1 = $2 (expected $3)"; FAIL=1; fi
}
SDK() { python3 - "$@"; }
trap 'exit 130' INT TERM

echo "===== 1. expand volume twice"
SDK <<'PY' || exit 1
import os, urllib3
urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", ""), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
r = c.post_volumes(names=["eda-runtwice-vol"], volume=flasharray.VolumePost(provisioned=1073741824), with_default_protection=False)
print("scratch volume ->", r.status_code)
PY
PB -e target_array="$A" -e volume=$VOL -e new_size=2G playbooks/expand_volume.yml | tail -2
PB -e target_array="$A" -e volume=$VOL -e new_size=2G playbooks/expand_volume.yml | tail -2
check "expand second run changed" "$(rec volume_expand changed)" "false"
check "expand second run already_at_size" "$(rec volume_expand already_at_size)" "true"

echo "===== 2. replicate now twice"
PB -e target_array="$A" -e pgroup="$PGROUP" -e reason=overdue playbooks/replicate_now.yml | tail -2
PB -e target_array="$A" -e pgroup="$PGROUP" -e reason=overdue playbooks/replicate_now.yml | tail -2
check "replicate second run changed" "$(rec replicate_now changed)" "false"
check "replicate second run skipped" "$(rec replicate_now skipped)" "true"

echo "===== 3. protect now twice (eradication delay left alone)"
PB -e target_array="$A" -e extend_eradication_by_s=0 -e audit_user=test -e audit_command=e2e playbooks/protect_now.yml | tail -2
PB -e target_array="$A" -e extend_eradication_by_s=0 -e audit_user=test -e audit_command=e2e playbooks/protect_now.yml | tail -2
check "protect second run skipped" "$(rec protect_now skipped)" "true"
check "protect second run eradication_changed" "$(rec protect_now eradication_changed)" "false"

echo "===== 4. capacity request twice, reclaim twice"
SDK <<'PY' || exit 1
import os, urllib3
urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", ""), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
c.post_volumes(names=["eda-runtwice-reclaim"], volume=flasharray.VolumePost(provisioned=1073741824), with_default_protection=False)
c.patch_volumes(names=["eda-runtwice-reclaim"], volume=flasharray.VolumePatch(destroyed=True))
pending_v = [v.name for v in c.get_volumes(destroyed=True).items]
pending_s = [s.name for s in c.get_volume_snapshots(destroyed=True).items]
print("pending eradication:", pending_v, pending_s)
open("tests/out/reclaim_ok", "w").write("1" if pending_v == ["eda-runtwice-reclaim"] and not pending_s else "0")
PY
REQ="-e at_risk_array=$A -e request_dir=$REQUEST_DIR -e request_key=runtwice-2026 -e used_pct=91 -e days_to_full=9"
PB $REQ playbooks/capacity_request.yml | tail -1
PB $REQ playbooks/capacity_request.yml | tail -1
check "request second run status" "$(rec capacity_request status)" '"already_raised"'
check "request files written" "$(ls "$REQUEST_DIR"/*.json | wc -l | tr -d ' ')" "1"
REQ_ID=$(rec capacity_request request_id | tr -d '"')
if [ "$(cat $OUT/reclaim_ok)" = "1" ]; then
  RECL="-e request_dir=$REQUEST_DIR -e request_id=$REQ_ID -e reclaim_action=eradicate_pending -e approver=e2e"
  PB $RECL playbooks/capacity_reclaim.yml | tail -1
  PB $RECL playbooks/capacity_reclaim.yml | tail -1
  check "reclaim second run already_done" "$(rec capacity_reclaim already_done)" "true"
else
  echo "  SKIP reclaim: the array holds other pending-eradication objects than the scratch volume"
fi

echo "===== 5. Alertmanager silence twice (create, create, expire, expire)"
if [ -n "${ALERTMANAGER_URL:-}" ]; then
  SIL="-e alertmanager_url=$ALERTMANAGER_URL -e silence_array_regex=array-a|array-b -e silence_comment=run-twice-test"
  PB -e mode=create $SIL playbooks/alertmanager_silence.yml | tail -1
  FIRST_ID=$(rec alertmanager_silence silence_id)
  PB -e mode=create $SIL playbooks/alertmanager_silence.yml | tail -1
  check "silence second create reused" "$(rec alertmanager_silence reused)" "true"
  check "silence second create same id" "$(rec alertmanager_silence silence_id)" "$FIRST_ID"
  SID=$(echo "$FIRST_ID" | tr -d '"')
  PB -e mode=expire -e alertmanager_url="$ALERTMANAGER_URL" -e silence_id="$SID" playbooks/alertmanager_silence.yml | tail -1
  check "silence first expire changed" "$(rec alertmanager_silence changed)" "true"
  PB -e mode=expire -e alertmanager_url="$ALERTMANAGER_URL" -e silence_id="$SID" playbooks/alertmanager_silence.yml | tail -1
  check "silence second expire already_expired" "$(rec alertmanager_silence already_expired)" "true"
else
  echo "  SKIP: ALERTMANAGER_URL not set"
fi

if [ "${RUN_SWITCHOVER:-0}" = "1" ]; then
  echo "===== 6. DR switchover twice each way"
  for v in B SOURCE_POD TARGET_POD; do [ -n "${!v:-}" ] || { echo "$v is not set" >&2; exit 2; }; done
  SW="-e source_array=$A -e source_pod=$SOURCE_POD -e target_array=$B -e target_pod=$TARGET_POD -e eradicate_undo_demote=true"
  PB $SW playbooks/dr_switchover.yml | tail -2
  PB $SW playbooks/dr_switchover.yml | tail -2
  check "switchover second run changed" "$(rec dr_switchover changed)" "false"
  SWB="-e source_array=$B -e source_pod=$TARGET_POD -e target_array=$A -e target_pod=$SOURCE_POD -e eradicate_undo_demote=true"
  PB $SWB playbooks/dr_switchover.yml | tail -2
  PB $SWB playbooks/dr_switchover.yml | tail -2
  check "switchback second run changed" "$(rec dr_switchover changed)" "false"
fi

echo "===== cleanup"
T0=$T0 PGROUP=$PGROUP SDK <<'PY'
import os, time, urllib3
urllib3.disable_warnings()
from pypureclient import flasharray
c = flasharray.Client(target=os.environ["FA_URL"].replace("https://", ""), api_token=os.environ["FA_TOKEN"], verify_ssl=False)
t0 = int(os.environ["T0"]) * 1000 - 60000
def why(r):
    return "" if r.status_code == 200 else (r.errors[0].message if getattr(r, "errors", None) else "HTTP %s" % r.status_code)
for name in ("eda-runtwice-vol", "eda-runtwice-reclaim"):
    c.patch_volumes(names=[name], volume=flasharray.VolumePatch(destroyed=True))
    r = c.delete_volumes(names=[name]); print("eradicate", name, "->", r.status_code, why(r))
# the snapshots this run took are destroyed; an array with eradication
# disabled keeps them pending until its own timer runs out
snaps = [s for s in c.get_protection_group_snapshots(filter="name='*.eda-*'", sort=["created-"], limit=60).items if s.created >= t0]
for s in snaps:
    c.patch_protection_group_snapshots(names=[s.name], protection_group_snapshot=flasharray.ProtectionGroupSnapshotPatch(destroyed=True))
    r = c.delete_protection_group_snapshots(names=[s.name]); print("eradicate snapshot", s.name, "->", r.status_code, why(r))
PY
echo "===== records"
python3 - <<'PY'
import json
for line in open("tests/out/alerts_twice.log"):
    r = json.loads(line)
    keys = [k for k in ("changed", "already_at_size", "skipped", "status", "already_done", "reused", "already_expired") if k in r]
    print("  ", r["time"], r["kind"], {k: r[k] for k in keys})
PY
[ "$FAIL" = "0" ] && echo "RUN TWICE: all checks passed" || { echo "RUN TWICE: some checks FAILED"; exit 1; }
