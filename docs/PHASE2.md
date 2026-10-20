# Phase 2 record: replication as an RPO contract

Date: 2026-10-20. Same lab and tooling as [PHASE0.md](PHASE0.md): two
arrays with a sync array connection, an async pod replica link (test ->
test-rl), a stretched pod (cinder-pod) and a protection group (cinder-group)
replicating hourly from A to B.

## What was built

- Replication events enriched: failed path details per array connection,
  RPO breach streaks and pause duration per replica link, online/offline
  member lists and a `recovered` flag per stretched pod, overdue and stuck
  detection per replicating protection group.
- `extensions/eda/rulebooks/replication.yml`: escalation after N breached
  polls, replicate-now for overdue or stuck groups, host preferred arrays
  moved to the online side when a stretched pod loses a member and moved back
  on recovery, failed paths and paused or unhealthy links notified, and a
  tick-driven DR drill. Each action sits behind a var
  (`replication_auto_replicate_now`, `activecluster_auto_prefer`).
- Playbooks: `replicate_now.yml` (purefa_pgsnap with `remote: true`),
  `preferred_array.yml` (hosts discovered from the pod's volume connections,
  changed with purefa_host, hosts with an existing preference left alone),
  `dr_drill.yml` + `dr_drill_one.yml` (copy one member volume out of the
  latest fully transferred snapshot on the target, verify size against the
  snapshot, destroy and eradicate).

## Exit test results

| Step | What was done | Result |
| --- | --- | --- |
| steady state | replication rulebook, both arrays, 45 s | all events flowed, nothing fired; the tick ruleset ran one DR drill |
| DR drill | on B, from `A:cinder-group` | latest completed snapshot found, member volume copied, size verified, copy destroyed and eradicated, `dr_drill` record `info` |
| replicate now | replayed overdue event for cinder-group | `cinder-group.eda-overdue-<stamp>` created with immediate replication (real) |
| preferred arrays | replayed degraded pod event with a throwaway host connected to a volume in cinder-pod | preferred array set to the online array on the local hosts of the pod (the peer array's hosts, listed as `<array>:<host>`, correctly skipped); the recovery event cleared it again |

## What the lab taught us

- `purefa_pgsnap` forbids `now` together with `remote`; `remote: true` on
  its own is the immediate replicated snapshot.
- Connections to a stretched pod's volumes list the peer array's hosts as
  `<array>:<host>`; only unqualified names are local and changeable.
- `host.preferred_arrays` comes back as references, so the discovery
  serialises names.
- A protection group's `targets` are not on the group object at REST 2.46;
  `/protection-groups/targets` is one extra call per poll.
- Transfers that completed before the poller started have no `started`;
  the drill picks the latest `completed`.
