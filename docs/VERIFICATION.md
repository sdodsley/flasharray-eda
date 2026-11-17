# Verification

What is exercised against FlashArray hardware, what is replayed from
fixtures, and what is not verified. The design is in
[DESIGN.md](DESIGN.md); array and tool facts are in
[HARDWARE_NOTES.md](HARDWARE_NOTES.md).

## Environment

Two Purity//FA 6.10.6 arrays (REST 2.46) connected for sync replication,
with one async pod replica link, one stretched (ActiveCluster) pod and one
protection group replicating hourly from one array to the other. Tooling:
ansible-rulebook 1.3.2, ansible-core 2.17, py-pure-client 1.94.1,
ansible.eda 2.13.0. Array A is the replication source (change control,
capacity and QoS tests); array B is the target (the DR drill). The alerts
signal also ran read-only against a third array on Purity//FA 6.12.4
(REST 2.59): the poll succeeds, and `/alerts/events` demands alert ids
there too, so the plugin diffs `/alerts` on every Purity it has met. This
is a prototype, not a supported product.

## How the tests run

`tests/e2e_sources.sh`, `tests/e2e_change_control.sh`,
`tests/e2e_replication.sh` and `tests/e2e_approvals.sh` induce on a lab
array what can be induced safely and restore it afterwards: NTP drift, a
second local user's changes, a replica link pause and resume
(`purefa_pod_replica`), a maintenance window, a replicate-now snapshot, a
test alert (`/alert-watchers/test` -> `purity.test`), the identify LED, a
pending-eradication volume, and a QoS cap on a volume with live IOPS
(thresholds lowered). What cannot be induced (drive, controller or PSU
failure, a degraded stretched pod, the anomaly signature) is replayed from
`tests/fixtures/` through `ansible.eda.generic`. Everything the scripts
write goes under `tests/out/`; the environment variables they read, the
files under `tests/out/` and the `E2E_ERADICATE_ALL=1` guard on the
eradicate step are in [Testing](../README.md#testing).

## Exercised on hardware

Every test restores what it changes: links resumed, windows removed, LEDs
cleared, the eradication delay put back, scratch objects eradicated.
Replayed fixture events drive real actions on the array.

### Pull path and the base rulebooks

| Area | Test | Result |
| --- | --- | --- |
| alerts steady state | runs `alerts.yml` against both arrays | alerts already open at start are reported as `initial`, no false `opened` |
| config drift | adds a server to the NTP list (`tests/induce_ntp_drift.yml`) | the next poll emits the drift, `remediate_ntp.yml` reapplies the baseline (`changed: true`), the following poll is clean |
| replication | pauses the pod replica link (`tests/replica_link_pause.yml`) | the paused rule fires on the source array and on the target (inbound view); the test resumes the link |
| health, maintenance window | opens a window (`tests/maintenance_window.yml`) | the health event carries `maintenance_active: true` with the window's expiry |
| health, hardware replay | replays four fixture events: a critical controller, an unhealthy bay inside a window, a controller failover, the controller recovered | the identify LED lights on the named controller of the real array, the in-window bay is logged not actioned, the failover notifies, the recovered event clears the LED (the helper clears it only as a fallback) |
| audits | runs the audits signal with a lookback | every change made by the tests appears with user, command, subcommand and arguments |
| test alert | runs `tests/send_test_alert.py` | the array sends its test alert to the configured watcher |
| capacity, performance steady state | runs the telemetry signal on an idle array | events flow, nothing fires |

The loop's own failure path, standalone: the alerts rulebook against one
array whose URL was a closed port, polling every two seconds, wrote a
warning record on the first failed poll ("alerts poll failed") and a
critical one at the tenth ("poll has failed 10 times in a row"), then
nothing more across 38 failures in 75 seconds. The plugin's `consecutive`
count and `escalate` flag drove both; Slack was not attached, so the
records are the proof.

### Change control, suppression, runbook map

| Area | Test | Result |
| --- | --- | --- |
| human change | a second local user (created by the test) adds an NTP server | the audit arrives with that user's name; `reconcile.yml` reverts it to the baseline within one poll |
| human destroy | the same user creates, destroys and eradicates a scratch volume | each destructive command is reported with user and interface |
| maintenance window | opens a window, then induces the same drift again | the audit carries `maintenance_active: true`; logged, not reconciled |
| git push | makes an empty commit in the baseline repository and POSTs the signed push payload to the webhook | `apply_baseline.yml` pulls and reapplies NTP and DNS |
| runbook map | replays four alerts: a ticket code, a code with per-code tasks, an unknown code, one inside a window | ticket; ticket plus per-code tasks; default notify; logged, not routed |

### Replication

| Area | Test | Result |
| --- | --- | --- |
| steady state | runs `replication.yml` against both arrays | events flow, nothing fires; the tick ruleset runs one DR drill |
| DR drill | runs on array B, from the protection group replicated out of array A | the drill finds the latest completed snapshot, copies one member volume, verifies its size, destroys and eradicates the copy, and writes a `dr_drill` record of severity `info` |
| replicate now | replays an overdue event for the protection group | an immediate replicated snapshot `<group>.eda-overdue-<stamp>` is created on the real array |
| preferred arrays | replays a degraded-pod event, with a throwaway host connected to a volume in the stretched pod | the preferred array is set to the online array on the pod's local hosts (the peer array's hosts, listed as `<array>:<host>`, are skipped); the recovery event clears it again |

### Capacity approval, QoS hysteresis, the composite rule

| Area | Test | Result |
| --- | --- | --- |
| approval gate | destroys a scratch volume (pending eradication); runs `capacity.yml` with a threshold low enough to fire; POSTs an approval to the webhook with the bearer token | the request is written and logged; on approval the scratch volume is eradicated, and a SafeMode-locked snapshot is reported as refused by the array, not forced |
| QoS cap | lowers thresholds so a volume with live IOPS counts as a noisy neighbour on a hot array | a temporary IOPS limit is applied, tags `qos_cap` and `qos_prev_iops` set, a record written |
| QoS lift | raises thresholds so the same volume reads as normal | after two normal polls (`qos_lift_normal_polls`, set to 2 by the test; the example default is 5) the previous limit is restored and the tags removed |
| anomaly | replays three signals for one array | the multi-event condition fires; snapshots are created on every protection group with members (empty groups report as failures); the enabled eradication delay is lengthened from one day to two (put back by the test); the snapshot in a SafeMode-protected group cannot be eradicated afterwards, as designed |

### Push path and zero-code path (replayed input; the live syslog path is under "Exercised on a platform")

| Area | Test | Result |
| --- | --- | --- |
| syslog parser | feeds representative audit, alert, test and garbage lines through `purefa_syslog.py parse` | the audit line's user, action, result and key/value fields parse; alert and test lines are recognised; garbage returns `kind: other` |
| syslog listener | runs `syslog.yml` and sends lines over UDP and then TCP with `tests/syslog_send.py` | both transports deliver; a human's `purevol destroy` and the alert produce notifications, the test line is logged |
| Alertmanager | runs `alertmanager.yml` and sends one webhook payload with a replica-link alert, a hardware alert and a resolved capacity alert (README 'Testing' shows a representative body) | the hardware alert lights the identify LED on a controller of the real array (cleared by the test); the others notify and log |

### Run twice (2026-11-13, standalone against the arrays)

Seven playbooks launched twice with the same extra vars, seconds apart,
from `tests/e2e_run_twice.sh`:

- Expand volume grew a scratch volume to 2G; the second run found it
  already at that size (changed false, `already_at_size` true).
- Replicate now snapshotted the replicated protection group; the second
  run, 3 s later, requested nothing (inside the cooldown, `skipped` true).
- Protect now snapshotted the two local groups that hold volumes (the
  array refuses a snapshot of an empty group, four of the six, recorded as
  failures); the second run took nothing (`skipped` true). It ran with
  `extend_eradication_by_s` 0, so the guard against lengthening the delay
  twice is unit-tested only.
- The capacity request wrote one request file for one `request_key`; the
  second run reused it (`already_raised`, one file).
- The Alertmanager silence: the second create reused the first's id; the
  first expire changed, the second read it already expired. The same held
  at the default 30 minute length once the reuse rule had its five-minute
  floor.
- The DR switchover ran twice each way on the lab link; the second run of
  each direction changed nothing.
- The bridge's pending check, on the platform: with a capacity execution
  paused at its approval, two launches of the orchestrator-post template
  with the same event each recorded `pending` with that execution's id
  and posted nothing (jobs 486 and 488); the approval was then rejected,
  so nothing was reclaimed.

Not run live: the reclaim's already-done path (array A refuses
eradication, `Eradication is disabled`; the static test pins it), and the
drill's in-flight skip, its stale-copy removal and a demotion retried
inside its quiesce (they need two overlapping launches; unit-tested).

### Self-check and definitions check (2026-11-13, standalone)

`playbooks/self_check.yml` ran from the standalone checkout against both
arrays three times:

- Syslog and phone-home tests only: every row passed on both arrays, phone
  home and remote assist alike; the platform's syslog activation logged
  the arrays' `purity.test` lines as they arrived.
- The alert-watcher test to one named watcher: array A passed; array B
  answered "No SMTP relay host configured.", the row was critical after
  the retry and the play failed for B as designed.
- An unknown test, and the SNMP test on arrays with no SNMP manager: the
  unknown test critical and the job failed; the SNMP rows warning.

`playbooks/definitions_check.yml` ran on the platform host with the
orchestrator service account and the platform token in its environment,
as the job gets them. The first run reported one drifted definition
(`capacity_reclaim_approval.json`, changed in the tree and not
re-imported) and, through a test-harness mistake, nine activations; after
the import and the harness fix it reported zero drift on both halves and
exited 0, and the kept temp file audited by hand agreed.

### The QoS cap and volume groups (2026-11-13/14)

Standalone against array A, with a scratch volume group carrying a 50,000
IOPS limit and a scratch member. The plugin's performance poll, run
directly, stamped `volume_group`, `volume_group_qos` true and, from a probe
tag on the group, `eda_capped` with scope `vgroup`. `qos_cap.yml` with
scope notify recorded the refusal and changed nothing; with scope volume
recorded the array's own refusal ("Cannot set rate limits on individual
volumes inside volume groups with rate limit.") without failing the job;
with scope vgroup tagged the group (`qos_cap`, `qos_prev_iops` 50000,
`qos_cap_volume`) and set its limit to 20,000, a second cap changed
nothing, the lift restored 50,000 and removed the tags, and a second lift
changed nothing. The group and the member were destroyed and eradicated
afterwards.

Through the platform, under the fio load of the earlier QoS proof, five
runs:

1. Fired nothing: the load volume read 55 µs unthrottled and was not among
   the ten slowest volumes the telemetry source fetches, while a datastore
   on the array produced the sustained-latency notices at the lowered
   thresholds. The volume was put in a group with a 300 IOPS limit, making
   it the slowest volume on the array (139 µs against a 40 µs threshold)
   at 61 % of its IOPS.
2. Never ran: the lab VM sat at a load average of 19 on 12 CPUs under
   another project's activations, the EDA server's running-activation cap
   of twelve was full, and the recreated performance activation failed its
   60-second readiness check nine starts in a row. Once the other project
   paused, the load fell to 9 within four minutes and the activation came
   up in 70 seconds.
3. Silent with every rule condition met: the decision environment in the
   workers' store predated the plugin change, and an image that does not
   emit `volume_group_qos` and `volume_group_capped` cannot match a
   condition that reads them; nothing reports it (the upgrade note's first
   instruction). The image was rebuilt and reloaded.
4. Scope notify on the rebuilt image: the new rule fired for the member
   (`volume_group_qos` true, 137 µs, 65.7 % of the array's IOPS for three
   polls) with the warning "sits in volume group ..., which has its own
   QoS" and no cap, and again 38 minutes later after its 30-minute
   throttle. The 1 % share threshold also let the cap rule, which the
   scope steers only for members of a rate-limited group, cap a datastore
   volume outside any group (408 µs, 19.7 % of the array's IOPS) at 200
   IOPS; the lift restored it 48 minutes later, at the first poll after
   the lab's thresholds were put back. The switch to scope vgroup failed
   on the platform gateway's 403 and 503 answers while another project's
   activations were starting.
5. Scope vgroup on the rebuilt image, share threshold 40 %, which only the
   load volume (60 %) reaches: the cap rule fired for the member (133 µs,
   61.6 % of the array's IOPS for four polls, `volume_group_qos` true),
   the playbook chose the group (`cap_target` vgroup), tagged it
   (`qos_cap`, `qos_cap_volume` naming the member, `qos_prev_iops` 300)
   and set its limit to 200, where the array reported 189 reads per
   second. fio was stopped at the cap; the lift rule fired for the member
   after three normal polls (`eda_capped` true, scope vgroup), restored
   300 and removed the tags within two minutes of the cap. The datastore
   volume was capped a second time during its own bursts (405 reads and
   127 writes per second, 63.9 % of the array, 221 µs for four polls) and
   carried a 200 IOPS limit for eight minutes until the lift at the first
   normal poll after the lab's thresholds were restored.

Both datastore caps were the design acting on the noisiest volume at that
moment; lowered thresholds on a shared array are a change to production,
and a proof on a shared array wants a source-side volume filter, which the
source lacks, or an array of its own. Every scratch object was destroyed and eradicated after
each run; the performance activation runs on the lab's own thresholds
again.

## Exercised through Prometheus and Alertmanager

Prometheus 3.5 and Alertmanager 0.28 run as containers on the platform host,
with [prometheus.example.yml](../prometheus/prometheus.example.yml) and
[alertmanager.example.yml](../prometheus/alertmanager.example.yml) filled in
for two arrays: one scrape job per array against the array's native
OpenMetrics exporter (`/metrics?namespace=purefa`, the API token as a bearer
credential, `array_name` set as a target label). Both targets scrape in
under a second (about 3,800 series per array on Purity//FA 6.10.6) and all
eleven rules of [flasharray.rules.yml](../prometheus/flasharray.rules.yml)
load and evaluate. The exporter facts the rules are written around
(`purefa_array_space_utilization` as the used percentage, the status
label with value 1 on `purefa_hw_component_status` and
`purefa_drive_capacity_bytes`, the `local_pod`, `remote`, `remote_pod` and
`status` labels on replica links, the latency `dimension` label such as
`usec_per_write_op`, no family for open alerts or array connections,
`array_name` only on `purefa_info`) are in
[HARDWARE_NOTES.md](HARDWARE_NOTES.md).

The chain is exercised with a lab-only rule file that lowers the capacity
threshold to one percent (the shipped rules stay at 85):

- FlashArrayCapacityHigh fires for the array at 2.6 percent used.
  Alertmanager delivers it to the platform's alertmanager activation
  (`alertmanager.yml` as a rulebook activation on port 5057 of the
  platform host), whose capacity rule launches
  **FlashArray - capacity request** against the array (the request file
  written, the objects pending eradication listed as candidates). Removing
  the override resolves the alert.
- FlashArrayReplicaLinkNotReplicating fires for real: pausing the pod
  replica link for eight minutes takes the rule through pending to firing
  on both arrays, Alertmanager delivers both, and the activation launches
  **FlashArray - notify** for each; resuming the link resolves them.
- FlashArrayScrapeFailed fires for real: pointing array B's scrape target
  at a closed port takes the rule through its five-minute `for` to firing,
  and the scrape rule launches **FlashArray - notify** (critical, the
  instance and the array in the text); restoring the target resolves the
  alert and the resolved notification follows (info).

The same chain also ran into `alertmanager.yml` as a standalone container
from the decision-environment image.

## Exercised on a platform

The objects `aap/seed.yml` builds are seeded on Ansible Automation Platform
2.7 and the capacity reclaim workflow launched by hand: the request job
reads the array's pending-eradication objects and publishes its artifacts,
the workflow pauses at the approval node, a platform user approves, the
reclaim node rebuilds the request from the artifacts, and the array
refuses every candidate (members of a protection group snapshot); each
refusal is recorded, nothing eradicated. The checks and their results are
in [aap/README.md](../aap/README.md).

The ten platform rulebooks run as activations on the EDA controller from
the decision-environment image, with the fleet credential and the platform
credential. They poll both arrays and act through `run_job_template`:
within their first half hour the change-control, config-drift and
replication activations launch the remediate NTP, remediate DNS, notify
and DR drill job templates, whose playbooks resolve the array from the
fleet credential. The webhook rulesets have no platform variant; approvals
and git pushes stay with the controller workflow and event streams.

The syslog path runs live on the platform. Both arrays carry a syslog
server with the URI `tcp://<platform host>:5140`; all four controllers
hold a TCP connection to the syslog activation. The arrays' syslog test
(`/syslog-servers/test`) arrives as a `purity.test` line and the test rule
logs it. A local administrator created for the test creates, destroys and
eradicates a scratch volume over REST: each command arrives as one
`purity.audit` line in the command layout, and the human-action rule
launches **FlashArray - notify** with the user, the array and the command
in the subject and the parsed fields (command, subcommand, arguments,
message id, UTC time) in the detail, about 26 seconds before the
change-control activation's REST audit poll reports the same command. The
session records around those commands and the arrays' own service audit
lines (`purity` with auth source `system`, `pure_datainsights`) are
skipped as automation; everything else the controllers forward (sshd,
sudo, bash, kernel, hundreds of lines a minute) is dropped by program,
without which the catch-all rule fired about 1,200 times in five minutes.
`run_job_template` holds the ruleset until the job ends, so three commands
three seconds apart became three notifications about a minute apart. UDP
is accepted from the platform host and from the array: a PATCH of the
server's URI to `udp://` left the TCP connection up, but deleting the
syslog server and recreating it with the `udp://` URI made the next test
line arrive over UDP (`protocol: udp` on the event); the `tcp://` server
was then put back.

A live `purity.alert` line: stretching an empty scratch pod from array A
to array B raises alert 152 (info, "<pod> is now online") on both arrays
and alert 120 (critical, "offline on peer") on array A, which opens and
closes within milliseconds. Each array sends one syslog line for the 152
alert and array A one for the 120 alert (not within the two minutes
watched on every run); the alert rule launches **FlashArray - notify** for
each, reading "alert 152: <pod> is now online" at info and "alert 120:
Pod offline on peer" at critical, each with the description and the
knowledge base link parsed from the layout in
[HARDWARE_NOTES.md](HARDWARE_NOTES.md) (`Severity: <Word>` among its
fields). Array B's open NTP alert (code 13, "Time sync failure") produced
a line that arrived 36 minutes after the alert's own UTC time. While the
listener was absent for 80 minutes the arrays queued their lines and
replayed them within two minutes of its return. Cleanup: the pod is
unstretched, its default protection group taken off the pod's
default-protection list, destroyed and eradicated, then the pod.

The EDA-to-orchestrator handoff runs end to end against a live
orchestrator ([ORCHESTRATOR.md](ORCHESTRATOR.md)), and from a running
activation: with the capacity threshold lowered to one percent for one
poll, the capacity activation's handoff rule launches
**FlashArray - orchestrator post**, the job posts the event (reaching the
orchestrator on the platform host as `host.containers.internal`), the
execution runs the request and pauses at its approval, and on approval
the reclaim eradicates the two scratch volumes pending eradication and is
refused the four SafeMode-locked snapshots (`result: partial`). On the
same orchestrator:

- `orchestrator/day2_expand_volume.json`, from its manual trigger against
  a scratch volume: notify, approval, the expand job grows the volume from
  one to two gibibytes and publishes `old_size`, `new_size` and `changed`,
  notify.
- `orchestrator/dr_drill_schedule.json`, published with its schedule: the
  DR drill job (one drill against the replicated protection group,
  `failed_count` 0), the condition's `false` port, the summary notify;
  first by hand from its scheduled trigger, then by the schedule itself at
  02:00 UTC the next night.
- The three day-2 definitions from their forms: identify LED lights and
  clears a controller's LED with no approval; QoS cap caps a scratch
  volume at the requested IOPS after its approval (`previous_iops` null,
  the volume destroyed afterwards); protect now snapshots every local
  protection group after its approval and reports the groups snapshotted
  and the ones the array refused.

The QoS cap under a real load: the platform host as iSCSI initiator for a
throwaway 20 GB volume on array A (one portal, fio 4k random reads, queue
depth 32 on four jobs), the day-2 cap requested at 500 IOPS from its form
and approved from Slack while the load ran, twice. On a volume with its
own limit of 1000 IOPS the array's counters went from about 1000 reads a
second to 500 when the cap job wrote the tags (`qos_prev_iops: 1000`) and
the limit, and back to 1000 when the lift job restored the limit and
removed the tags. On a volume created without a limit the cycle ended with
the volume reading no limit at all: the lift clears through the API with
an empty string, because the module's "clear" (0) through
everpure.flasharray 1.46 sets the array's maximum 100000000, which shows
as 100M (docs/HARDWARE_NOTES.md). The first attempt found two defects,
both covered by tests: two lift jobs 19 seconds apart, the second clearing
an already restored, untagged volume to the maximum (both modes are
idempotent: a cap on a tagged volume and a lift on an untagged one change
nothing); and a cap on a volume already at normal latency lifted at the
next poll ("normal for 10 polls") because the lift's count ran from the
volume's last slow read (the telemetry signal starts it at the cap; the
performance activation runs on the rebuilt image). The automatic
noisy-neighbour rule did not fire from this host: the array's service
time for 4k reads stayed near 70 µs with the initiator seeing
milliseconds, and 1 MiB reads on four paths reached 60 MB/s at 200 to 330
µs on the array and a quarter of its IOPS, short of the 2 ms volume
latency and the 1 ms array latency the rule needs. The array-side latency
the plugin reads excludes the wire; only a real hot array fires that rule
honestly.

The two ansible-builder images on the supported bases ran on the platform.
Built with ansible-builder 3.2 on a `vfs` image store seeded with the
platform's own copies of `ee-supported-rhel9` and `de-supported-rhel9` (no
registry login on the host) and loaded into the platform's stores rather
than pulled: the execution environment from
`aap/execution-environment.yml` (4.2 GB; ansible-core 2.16,
`everpure.flasharray` 1.46.0, py-pure-client on the Python Ansible runs
on) into the controller's task container, the decision environment from
`aap/decision-environment.yml` (2.9 GB; ansible-rulebook, the three
collections, py-pure-client) into the activation workers' store. On the
execution environment, as a throwaway EE object, the notify template
posted to Slack in 28 seconds and the switchover template, against a pod
that does not exist, reached the array through py-pure-client and stopped
at its guard with the array's own "Pod does not exist." On the decision
environment, as a throwaway DE object with the pull policy `missing` (the
default `always` pulls `localhost/...` and fails on the registry
certificate, docs/HARDWARE_NOTES.md), the alertmanager activation took a
posted replica-link alert through its rule and launched the notify job.
The activation was then recreated on the project's own decision
environment and the throwaway objects and images removed; the lab's
day-to-day images are the ones `decision-environment/Containerfile` and
`aap/Containerfile.ee` build.

The platform's Slack notification template fired on a job failure: the
switchover against the missing pod failed at its guard and the platform
posted "Job #261 'FlashArray - DR switchover' failed" with the job's link
(the notification record on the job reads `successful`). The failure also
showed the guards' messages mis-rendering for a missing pod; the messages
were corrected.

The ActiveDR failover test (`orchestrator/dr_failover_test.json`) runs
against the replica link's target pod on the second array: after the
first approval `dr_promote.yml` promotes the pod (its replica link stays
replicating, the source pod untouched) and the notify carries the states
before and after; after the second approval `dr_demote.yml` demotes it
with quiesce, discarding the test's writes, and the pod is a replica-link
target again. Both playbooks refuse a pod that is not a replica link's
target (`link_source_count`, docs/HARDWARE_NOTES.md). A test run the same
day as two switchovers of the link had its revert refused: the earlier
demotion's undo-demote pod was still on the array and the demote job
failed with the module's message, leaving the pod promoted until the
undo-demote pod was eradicated by hand and the job relaunched.
`dr_demote.yml` eradicates such pods first when the workflow passes
`eradicate_undo_demote`, which the definition's revert node does; a test
run inside the delay showed it, both approvals from Slack.

The planned failover and failback (`orchestrator/dr_failover_failback.json`,
`playbooks/dr_switchover.yml`) runs on the lab's ActiveDR link, whose two
pods hold no volumes: after the first approval the switchover job demotes
the source pod with quiesce, sees the link report the copy complete,
promotes the target pod and records the link reversed (the target's link
outbound and replicating), in 72 seconds; after the second approval the
same template with the sides swapped puts the link back, and both pods
read as they started. Both approvals of the first run were given in the
orchestrator's UI: the nodes named the `admins` group alone, and the
orchestrator does not list a service account as an approver, which is why
the shipped definitions name an `approvers` group as well.

Repeated with that group in the definitions and the bot logged in as a
user in it: the failover approval was decided from Slack within ten
seconds of the card and recorded as that user with the Slack name in the
notes. The switchover job stopped at the demotion: the first run had left
`test.undo-demote.5` on array A, a destroyed pod holding the pod's earlier
state with 288 hours on its timer, and the module refuses to demote the
pod again while it exists (docs/HARDWARE_NOTES.md). With the playbook
eradicating those pods when `eradicate_undo_demote` is set, the next run
went through on two Slack clicks: the failover eradicated that pod,
demoted the source and promoted the target in 77 seconds; the failback
eradicated array B's `test-rl.undo-demote.21` from the first run's
failback, demoted the target and promoted the source in 75 seconds; both
pods and the link read as they started. Two findings rode along. The
platform's gateway answered the failback's launch with a 503 after the
controller had created the job, so the orchestrator retried the node and
launched the template again 80 seconds later; the second job found both
sides already where they should be, changed nothing, and is the job the
execution recorded. And the replication rulebook, polling the link, saw
it quiesced during the planned demotion and posted a critical notify: to
a monitor that does not know about it, a planned switchover looks like a
stalled link.

With the switchover marking its source pod as planned work and the
replication plugin carrying the mark to the link's other side, the same
failover and failback ran again on two Slack clicks (2026-11-12): the
failover eradicated `test.undo-demote.6`, demoted the source and promoted
the target, the failback did the same the other way, and for the whole
window the replication rulebook recorded only "Replica link changing
during planned work - log only", twice per poll, with no critical notify.
The Alertmanager silence nodes ran before and after each step and
recorded the Alertmanager as unreachable rather than failing, that
instance listening on its loopback alone. The first attempt tagged the
target pod as well and stopped there: a demoted pod refuses tag writes
(docs/HARDWARE_NOTES.md).

The alert lifecycle ran against live alerts the same day. Array B's alert
13 went to `closing` after its NTP servers were set again, and the alerts
activation posted "alert 13 resolved" at info, one step below the alert's
warning, for each of the two ntpd alerts that closed. A pod stretched from
array A to B and back raised alerts 120 and 152 that opened and resolved
inside one poll: the activation routed them as opened (the critical
runbook ran for an alert already resolving) and never saw them close, so
the alerts rulebook notifies once for an alert first seen `closing`
instead of routing it. Standalone replays drove the two actuators: the
health fixtures lit the identify LED on array A's CT1 at the critical
event and cleared it at the recovered event (both reported as changed, the
LED reading off afterwards), and an alert 13 opened event for array B,
with the site map routing code 13 to tasks, ran runbook 13 and put the
baseline NTP servers back in place of the unroutable address the test had
set.

The overdue-replication route runs on the same orchestrator.
`orchestrator/replication_overdue_approval.json` is imported and
published; the bridge playbook posts an overdue event for the protection
group that replicates from array A to array B; the execution notifies,
pauses at its one-hour approval, and on approval
**FlashArray - replicate now** creates the immediate replicated snapshot
on array A and publishes `pgroup`, `snapshot_suffix` and `changed`, which
the closing notify reads; verified on three executions. The first version
of the definition read artifact keys the playbook did not publish and
failed at the closing notify, which validation cannot see; the playbook
publishes them, a unit test cross-checks every artifact a definition reads
against its playbook's `set_stats`, and the corrected definition went live
as the next version. An approval nobody decided within the hour expired:
the worker records the decision as expired ("Approval decision window
expired") and the execution fails at the approval node with neither port
taken. The latest execution's approval came from Slack through
`slack/approval_bot.py` (below). `fallback_decision` changes nothing: a
throwaway copy with `fallback_decision: reject` and a one-minute window
expired the same way, the execution failed at the node and the rejected
branch was skipped; the engine never reads the field
(orchestrator/README.md). `tools/ao_sync.py` runs live as the service
account once it holds the project-admin role on the project: it created
and published the failover-and-failback workflow, republished the drill
schedule (its description had changed in the file) as version 2, and
reported the seven others unchanged. Its first live run found the
service-accounts path misspelt and the export contract (no
`credential_names`, no empty `extra_vars`); both are in the tool.

Slack is exercised from the platform. **FlashArray - notify** with the
`Slack webhook` credential attached posts the record to the channel (200
returned, the failure report skipped): the severity icon, the severity,
the subject with the array named once and the detail on its own line;
against a capture server it carries a real line break, drops a carriage
return and escapes `&`, `<` and `>`. The approval bot, a container on the
platform host connected over Socket Mode, posts the card for a new
orchestrator approval within one poll, and a click on Approve decides it:
the orchestrator records the approval under the bot's user with
`via Slack by <user> (<id>)` in the decision notes and the execution
resumes into the replicate-now job. The controller's queue is watched the
same way: the controller-tier workflow launched by hand puts its approval
card in the channel, the platform's own Slack notification template posts
that the approval node needs review, then that it was approved and the
workflow succeeded, and the click from the channel approves it (the
reclaim runs and the array refuses the SafeMode-locked candidates). The
bot's identities (slack/README.md): an orchestrator user in the
`approvers` group the approval nodes name, the group holding the
project-user role on the project, and a platform user with the Approve
role on the workflow. A service account with the same project-user role
lists the approvals but is refused the decision (403), because approver
names resolve to users and groups; the orchestrator records each decision
as that user with the Slack name in the notes.

`tools/activation_audit.py` against the platform found nine of the ten
activations carrying the vars of the day they were created (no
orchestrator keys, no `syslog_programs`, one with the lab's 60-second
drill interval); `--recreate` rebuilt them with the current site vars,
printing each old and new id. The controller refused one create with
"activation with this name already exists" after the old id had left the
list; the tool waits that out, a second run finished the fleet, and a
read-only run reported all ten clean. `tools/ao_sync.py --check` against
the live orchestrator exits 0 with the nine definitions unchanged.

The schedules (2026-11-13). The seed created the three schedules with the
expected next runs (02:00 and 03:00 UTC the next day, Monday 08:00 UTC),
the two templates with their credentials (the definitions check with the
orchestrator service account and the platform token) and the platform
token credential. Three of the four seed runs that day lost one task each
to a gateway 502 or 503 on a read (the credential types, the activations,
the notification templates) and the next run finished the rest, as
"Re-running safely" says; one run went through clean. Each schedule was
fired once by setting its rule to a single run two minutes ahead and
restoring it, so the jobs carry launch type `scheduled`:

- The self check ran on both arrays (eight tests; syslog, phone home and
  remote assist passed on both, array A had no enabled watcher to e-mail
  and said so, array B's two watchers failed on its missing SMTP relay and
  the job failed as designed). The syslog activation, recreated on the new
  revision, turned the two `purity.test` lines into notify jobs within a
  few seconds, the receipt record showing array A's clock within a second
  of the listener's and array B's 36 minutes behind.
- The nightly drill copied a member volume of the replicated group out of
  the latest completed snapshot on the target, verified and destroyed it,
  and its job passed with `dr_drill_fail_job` set.
- The definitions check, launched by hand, failed correctly while the
  replication activation was being recreated (`status starting`) and
  passed once it ran: both halves compared, nine definitions unchanged,
  ten activations clean.

A second scheduled self check, after a seed run with the Slack variables
had put the notification template on the new templates (a seed run
without them removes it, and the webhook credential from the notify
template), had its failure posted to the channel by the platform's own
notification, with the link into the job.

The approval bot's reminders, expiry handling and state (2026-11-13).
Against the lab orchestrator, a throwaway workflow with a four-minute
approval window that nobody decided was imported, run and deleted three
times. Each time the bot posted the card within one poll, reminded in the
card's thread at half and at ninety percent of the window (the second
reminder shown in the channel), saw the approval leave the queue, read its
end from the detail (`expired`, decided by the worker, "Approval decision
window expired"), rewrote the card as expired, posted the critical message
in the channel and launched the notify template for the critical record
(jobs 507, 508 and 513). The first run found the state directory
unwritable by the image's non-root user under rootless podman, so the bot
warned on every write and posted the card again after a restart; with the
`U` volume flag the second and third runs kept the card across a restart
thirty seconds after posting ("1 card(s) remembered", one card for the
approval) and went on to its end. The third run was the committed code,
with the final-status read ahead of forgetting the card. Controller side:
the controller-tier capacity workflow launched by hand put its approval
in the queue, the bot posted the card, a denial by API as the admin took
the approval out of the queue, and the bot read the final status and
rewrote the card as rejected by `admin` while the workflow took its
failure edge to the warning notify. Not exercised live, pinned by unit
tests: a controller approval timing out (the lab's node waits 24 hours),
a Slack click racing the sweep, and a detail read failing as an approval
leaves the queue.

The platform after a reboot (2026-11-16). The host was rebooted once with
every platform service, the automation orchestrator's stack and this
project's three containers under systemd user units. All of them came
back on their own (the orchestrator's thirty-nine containers settled
within eleven minutes), and no FlashArray activation that had reached its
restart ceiling did: the activation worker marked those failed with the
ceiling message and left them, the four below the ceiling ran once the
websocket proxy was ready, and the other six were restarted by hand, by
id, one at a time, each running within a minute. `tools/activation_restart.py`
and its user unit were written from that reboot. A second reboot
(2026-11-17), with the unit installed, proved it: the API refused
connections for six minutes while the host's load passed 100, the unit
waited, then restarted nine activations one at a time (one needed a second
attempt after a readiness timeout) and exited 0 after eighteen minutes.
The tenth had read as running in the seconds before the worker marked it
failed and was left behind, which is why the tool now pauses after the API
answers and makes a second pass. The other project's unit ran alongside
and finished first; every platform service, the orchestrator's stack and
this project's containers came back under their units again, and the
containers started at boot again had no root mount in podman's view.

## Replayed only

Exercised from fixtures under `tests/fixtures/`, not induced on an array:
hardware failures (a critical controller, an unhealthy bay, a controller
failover), a degraded stretched pod and its recovery, an overdue protection
group, the four alert routes, the anomaly signature, syslog lines and one
Alertmanager payload. The replay rulebooks `tests/replay_*.yml` are kept
rule-for-rule identical to the live ones by the unit test, so a replayed
event takes the same path as a live one.

## Not verified

- An alert line from a hardware or capacity fault: the info, warning and
  critical lines above came from a pod stretch and an NTP failure; 26
  minutes without a reachable NTP server raised nothing within the test
  window (array B raised its NTP alert hours later).
- The hardware, drive, latency, data-reduction and mediator rules firing
  for real: all eleven load and evaluate against live series, and the
  capacity rule (lab override), the replica-link rule (a real pause) and
  the scrape rule (a closed port) are driven through Alertmanager to the
  platform activation; the rest wait for a real fault.
- The ansible-builder images under CI: CI runs the static checks only; the
  three definitions build on the lab host's `vfs` image store (rootless
  podman with native overlay there fails every multi-stage `COPY --from`,
  [PACKAGING.md](PACKAGING.md)) and the two supported-base images have run
  on the platform (above). The upstream-based definitions pin
  `quay.io/ansible/ansible-rulebook:v1.3.2`, the tested ansible-rulebook;
  `latest` on that repository is not guaranteed to be the newest release.
- A rulebook activation launching the controller-tier approval workflow:
  the platform capacity rulebook hands the event to the orchestrator
  (above) or launches the request job; it does not launch the controller
  workflow.
- `tools/ao_sync.py` deleting or unpublishing anything: by design it never
  does, so the live run exercised create, update and unchanged only.
- The post-boot tool's pause, second pass and re-read before acting at a
  real boot: added after the reboot above left one activation behind; the
  passes and the re-read are unit-tested, and the next reboot shows all
  three.
- Scale beyond two arrays.
- Drive, controller or PSU failure and real performance degradation:
  fixtures only (`tests/fixtures/`), though the replayed events drive the
  real identify LED.
- Array connection path failure or throttle changes: not induced.
- The automatic noisy-neighbour cap rule on a real hot array: the cap,
  lift, restore and clear ran under a real fio load (above), but the rule's
  own latency thresholds were never reached from the lab's initiator, so
  the rule has fired only on lowered thresholds and on replayed events.
- Packaging into `everpure.flasharray` and the Red Hat EDA certification
  track ([PACKAGING.md](PACKAGING.md)).

The second routing of an alert whose severity rises, the routing of an
alert the array sets back to open after `closing`, the resolved notice
for an alert next seen already closed, and the second lighting of the LED
for a component back in the issue set within the hour are unit-tested
only; no live alert changed severity or reopened, and no component
flapped, while watched.
