# Verification

What is exercised against FlashArray hardware, what is replayed from
fixtures, and what is not verified. The design is in
[DESIGN.md](DESIGN.md); array and tool facts are in
[HARDWARE_NOTES.md](HARDWARE_NOTES.md).

## Environment

Verified against two Purity//FA 6.10.6 arrays (REST 2.46) connected for sync
replication, with one async pod replica link, one stretched (ActiveCluster)
pod and one protection group replicating hourly from one array to the other.
Tooling: ansible-rulebook 1.3.2, ansible-core 2.17, py-pure-client 1.94.1,
ansible.eda 2.13.0. Array A below is the replication source (change control,
capacity and QoS tests run there); array B is the target (the DR drill runs
there). The alerts signal also ran read-only against a third array on
Purity//FA 6.12.4 (REST 2.59): the poll succeeds there, and `/alerts/events`
still demands alert ids on that version, so the plugin's diff of `/alerts`
is the path on every Purity it has met. This is a prototype; it is not a
supported product.

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
`tests/fixtures/` through `ansible.eda.generic`; the replayed events still
drive the real actions on the array. Everything the scripts write goes under
`tests/out/`. The environment variables the scripts read, the files under
`tests/out/` and the `E2E_ERADICATE_ALL=1` guard on the eradicate step are
described in [Testing](../README.md#testing).

## Exercised on hardware

Every test restores what it changes: links are resumed, windows removed,
LEDs cleared, the eradication delay put back, scratch objects eradicated.
Replayed fixture events still drive real actions on the array: the identify
LED, the replicate-now snapshot, the preferred arrays and the protection
snapshots.

### Pull path and the base rulebooks

| Area | Test | Result |
| --- | --- | --- |
| alerts steady state | runs `alerts.yml` against both arrays | alerts already open at start are reported as `initial`, no false `opened` |
| config drift | adds a server to the NTP list (`tests/induce_ntp_drift.yml`) | the next poll emits the drift, `remediate_ntp.yml` reapplies the baseline (`changed: true`), the following poll is clean |
| replication | pauses the pod replica link (`tests/replica_link_pause.yml`) | the paused rule fires on the source array and on the target (inbound view); the test resumes the link |
| health, maintenance window | opens a window (`tests/maintenance_window.yml`) | the health event carries `maintenance_active: true` with the window's expiry |
| health, hardware replay | replays three fixture events: a critical controller, an unhealthy bay inside a window, a controller failover | the identify LED lights on the named controller of the real array, the in-window bay is logged not actioned, the failover notifies; the test clears the LED |
| audits | runs the audits signal with a lookback | every change made by the tests appears with user, command, subcommand and arguments |
| test alert | runs `tests/send_test_alert.py` | the array sends its test alert to the configured watcher |
| capacity, performance steady state | runs the telemetry signal on an idle array | events flow, nothing fires |

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

### Push path and zero-code path (replayed input; the live syslog path is
under "Exercised on a platform")

| Area | Test | Result |
| --- | --- | --- |
| syslog parser | feeds representative audit, alert, test and garbage lines through `purefa_syslog.py parse` | the audit line's user, action, result and key/value fields parse; alert and test lines are recognised; garbage returns `kind: other` |
| syslog listener | runs `syslog.yml` and sends lines over UDP and then TCP with `tests/syslog_send.py` | both transports deliver; a human's `purevol destroy` and the alert produce notifications, the test line is logged |
| Alertmanager | runs `alertmanager.yml` and sends one webhook payload with a replica-link alert, a hardware alert and a resolved capacity alert (README 'Testing' shows a representative body) | the hardware alert lights the identify LED on a controller of the real array (cleared by the test); the others notify and log |

## Exercised through Prometheus and Alertmanager

Prometheus 3.5 and Alertmanager 0.28 run as containers on the platform host,
with [prometheus.example.yml](../prometheus/prometheus.example.yml) and
[alertmanager.example.yml](../prometheus/alertmanager.example.yml) filled in
for two arrays: one scrape job per array against the array's native
OpenMetrics exporter (`/metrics?namespace=purefa`, the API token as a bearer
credential, `array_name` set as a target label). Both targets scrape in
under a second (about 3,800 series per array on Purity//FA 6.10.6) and all
eleven rules of [flasharray.rules.yml](../prometheus/flasharray.rules.yml)
load and evaluate. The exporter facts the rules are written around:
`purefa_array_space_utilization` is the used percentage, status is a label
with value 1 on `purefa_hw_component_status` and `purefa_drive_capacity_bytes`,
replica links carry `local_pod`, `remote`, `remote_pod` and `status`,
latency has a `dimension` label (`usec_per_write_op` and the like), there is
no family for the array's open alerts or its array connections, and only
`purefa_info` carries `array_name`.

The chain is exercised with a lab-only rule file that lowers the capacity
threshold to one percent (the shipped rules stay at 85): FlashArrayCapacityHigh
fires for the array at 2.6 percent used, Alertmanager delivers it to the
platform's alertmanager activation (`alertmanager.yml` as a rulebook
activation on port 5057 of the platform host), and its capacity rule
launches **FlashArray - capacity request** against the array (the request
file written, the objects pending eradication listed as candidates).
Removing the override resolves the alert. FlashArrayReplicaLinkNotReplicating
fires for real: pausing the pod replica link for eight minutes takes the
rule through pending to firing on both arrays, Alertmanager delivers both,
and the activation launches **FlashArray - notify** for each; resuming the
link resolves them. FlashArrayScrapeFailed fires for real too: pointing
array B's scrape target at a closed port takes the rule through its
five-minute `for` to firing, Alertmanager delivers it to the platform
activation, whose scrape rule launches **FlashArray - notify** (critical,
the instance and the array in the text), and restoring the target resolves
the alert and the resolved notification follows (info). The same chain ran
earlier into `alertmanager.yml` as a standalone container from the
decision-environment image.

## Exercised on a platform

The objects `aap/seed.yml` builds are seeded on Ansible Automation Platform
2.7 and the capacity reclaim workflow is launched by hand: the request job
reads the array's pending-eradication objects and publishes its artifacts,
the workflow pauses at the approval node, a platform user approves, and the
reclaim node rebuilds the request from the artifacts; the array refuses
every candidate (members of a protection group snapshot) and the reclaim
records each refusal; nothing is eradicated. The checks and their results
are in [aap/README.md](../aap/README.md).

The ten platform rulebooks run as activations on the EDA controller from
the decision-environment image, with the fleet credential injecting the
array list and the platform credential giving ansible-rulebook the
controller API. They poll both arrays and act through `run_job_template`:
within their first half hour the change-control, config-drift and
replication activations fire rules and launch the remediate NTP,
remediate DNS, notify and DR drill job templates, whose playbooks resolve
the array from the fleet credential and run against it. The webhook
rulesets have no platform variant, so approvals and git pushes stay with
the controller workflow and event streams.

The syslog path runs live on the platform. Both arrays carry a syslog
server with the URI `tcp://<platform host>:5140`, and all four controllers
hold a TCP connection to the syslog activation, which the EDA controller
runs with host networking so the listener's port is the host's. The
arrays' own syslog test (`/syslog-servers/test`) arrives as a `purity.test`
line and the test rule logs it. A local administrator created for the
test creates, destroys and eradicates a scratch volume over REST: each
command arrives as one `purity.audit` line in the command layout, the
human-action rule fires on each and launches **FlashArray - notify** with
the user, the array and the command in the subject and the parsed fields
(command, subcommand, arguments, message id, UTC time) in the detail; the
first notification lands about 26 seconds before the change-control
activation's REST audit poll reports the same command. The session
records around those commands (session request, operation request,
session end) are parsed and skipped, and so are the audit lines the
array's own services write every few minutes (`purity` with auth source
`system` for the scheduled protection group snapshots, `pure_datainsights`
for its tag housekeeping), which fired the human-action rule until the
listener counted them as automation. Everything else the controllers
forward (sshd, sudo, bash, kernel, hundreds of lines a minute) is dropped
by program before any rule sees it; before that filter existed the
catch-all rule fired about 1,200 times in five minutes. `run_job_template`
holds the ruleset until the job ends, so three commands three seconds
apart became three notifications about a minute apart. A `purity.test`
line sent over UDP from the platform host is accepted the same way, and so
is one from the array itself: a PATCH of the server's URI to `udp://` left
the TCP connection up, but deleting the syslog server and recreating it with
the `udp://` URI made the array's next test line arrive over UDP
(`protocol: udp` on the event); the `tcp://` server was then put back.

A live `purity.alert` line is verified by making the array raise an alert
the way the QA suite's history shows it happens: stretching an empty
scratch pod from array A to array B raises alert 152 (info, "<pod> is now
online") on both arrays and alert 120 (critical, "offline on peer") on
array A, which opens and closes within milliseconds. Each array sends one
syslog line for the 152 alert, and array A one for the 120 alert (not
within the two minutes watched on every run); the alert rule fires on each
and launches **FlashArray - notify**. The layout (a level word, the
subject, the code in brackets, the description, the support-case sentence,
`Severity: <Word>`, UTC and array time, array name, domain, knowledge base
link and suggested action) is what the listener parses into fields, and
with the rebuilt decision environment in the activation the notifications
read "alert 152: <pod> is now online" at info and "alert 120: Pod offline
on peer" at critical, each with the description and the knowledge base
link. Array B's open NTP alert (code 13, "Time sync
failure") had produced a line as well, which arrived 36 minutes after the
alert's own UTC time. While the listener was absent for 80 minutes the
arrays queued their lines and replayed them within two minutes of its
return. The pod is unstretched, its default protection
group taken off the pod's default-protection list, destroyed and
eradicated, then the pod ([HARDWARE_NOTES.md](HARDWARE_NOTES.md)).

The EDA-to-orchestrator handoff is exercised end to end against a live
orchestrator; see [ORCHESTRATOR.md](ORCHESTRATOR.md). It also runs from
a running activation: with the capacity threshold lowered to one percent
for one poll, the capacity activation's handoff rule launches
**FlashArray - orchestrator post**, the job posts the event (the
orchestrator on the platform host is reached from the job's execution
environment as `host.containers.internal`), the execution runs the
request and pauses at its approval, and on approval the reclaim
eradicates the two scratch volumes pending eradication and is refused the
four SafeMode-locked snapshots (`result: partial`). On the same
orchestrator, `orchestrator/day2_expand_volume.json` runs from its manual
trigger against a scratch volume (notify, approval, the expand job grows
the volume from one to two gibibytes and publishes `old_size`, `new_size`
and `changed`, notify) and `orchestrator/dr_drill_schedule.json`,
published with its schedule, runs the DR drill job (one drill against the
replicated protection group, `failed_count` 0), takes the condition's
`false` port and notifies the summary, first started by hand from its
scheduled trigger and then by the schedule itself at 02:00 UTC the next
night, with nobody starting it. The three day-2 definitions run from their
forms: identify LED lights and clears a controller's LED with no
approval; QoS cap caps a scratch volume at the requested IOPS after its
approval (`previous_iops` null, the volume destroyed afterwards); protect
now snapshots every local protection group after its approval and
reports the groups it could snapshot and the ones the array refused.
The ActiveDR failover test (`orchestrator/dr_failover_test.json`) runs
against the replica link's target pod on the second array: after the
first approval `dr_promote.yml` promotes the pod (its replica link stays
replicating, the source pod is untouched) and the notify carries the
states before and after; after the second approval `dr_demote.yml`
demotes it with quiesce, discarding the writes made during the test, and
the pod is a replica-link target again. Both playbooks refuse a pod that
is not the target of a replica link: Purity counts the sources linked to
a pod in `link_source_count`, so a target pod has a source count above
zero and an inbound link.

The overdue-replication route runs on the same orchestrator.
`orchestrator/replication_overdue_approval.json` is imported and
published, and the bridge playbook posts an overdue event for the
protection group that replicates from array A to array B, shaped as the
replication signal emits it; the execution notifies, pauses at its
one-hour approval, and on approval **FlashArray - replicate now** creates
the immediate replicated snapshot on array A and publishes `pgroup`,
`snapshot_suffix` and `changed`, which the closing notify reads, verified
on three executions. The first version of the definition read artifact
keys the playbook did not publish and failed at the closing notify, which
validation cannot see, so the playbook publishes them, a unit test
cross-checks every artifact a definition reads against its playbook's
`set_stats`, and the corrected definition went live as the next version
through the publish call that carries a definition. One execution's
approval nobody decided within the hour expired: the orchestrator's worker
records the decision as expired ("Approval decision window expired") and
the execution fails at the approval node with neither port taken. The
latest execution's approval was given from Slack through
`slack/approval_bot.py` (below). `fallback_decision` changes nothing: a
throwaway copy of the definition with `fallback_decision: reject` and a
one-minute window expired the same way, the execution failed at the node
and the rejected branch was skipped; the engine of this release never reads
the field (orchestrator/README.md). `tools/ao_sync.py` runs live as the
service account once it holds the project-admin role on the project: it
created and published the failover-and-failback workflow, republished the
drill schedule, whose description had changed in the file, as version 2,
and reports the seven others unchanged; its first live run found the
service-accounts path misspelt and the export contract (no
`credential_names`, no empty `extra_vars`), both now in the tool.

Slack is exercised from the platform. **FlashArray - notify** with the
`Slack webhook` credential attached posts the record to the channel (the
post returns 200 and the failure report is skipped); the text is the
severity icon, the severity, the subject with the array named once and the
detail on its own line, and rendered against a capture server it carries a
real line break, drops a carriage return and escapes `&`, `<` and `>`. The
approval bot runs as a container on the platform host with its two
secrets as podman secrets, connects over Socket Mode, posts the card for a
new orchestrator approval within one poll, and a click on Approve decides
it: the orchestrator records the approval under the bot's identity with
`via Slack by <user> (<id>)` in the decision notes and the execution
resumes into the replicate-now job. The controller's queue is watched the
same way: launching the controller-tier workflow by hand puts its approval
card in the channel, the platform's own Slack notification template posts
that the approval node needs review, then that it was approved and that
the workflow succeeded, and the click from the channel approves it (the
reclaim then runs and the array refuses the SafeMode-locked candidates).
The bot runs under identities made for it, an orchestrator service account
with the project-user role and a platform user with the Approve role on
the workflow, and polls both queues with them.

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
  critical lines verified above came from a pod stretch and an NTP
  failure; 26 minutes without a reachable NTP server raised nothing within
  the test window (array B raised its NTP alert hours later).
- The hardware, drive, latency, data-reduction and mediator rules firing
  for real: all eleven load and evaluate against live series, the capacity
  rule (lab override), the replica-link rule (a real pause) and the scrape
  rule (a closed port) are driven through Alertmanager to the platform
  activation; the rest wait for a real fault.
- The ansible-builder images on the platform. All three definitions build
  with ansible-builder 3.2 on a `vfs` image store (rootless podman with
  native overlay on the lab host fails every multi-stage `COPY --from`
  with "copier: stat ... no such file" although the stage image holds the
  path, [PACKAGING.md](PACKAGING.md)): the decision environment from
  `decision-environment/execution-environment.yml` carries the three
  collections, ansible-rulebook 1.3.2 and the plain-file tree, and runs
  `alertmanager.yml` as a standalone listener that takes a posted alert
  through its rule to a notify record; `aap/execution-environment.yml` on
  the supported base carries `everpure.flasharray` and py-pure-client on
  the Python 3.12 Ansible runs on; `aap/decision-environment.yml` on the
  supported base carries the three collections and py-pure-client, with
  `systemd-python` excluded. Neither supported-base image has run a job or
  an activation on the platform; the images that run there are the ones
  `decision-environment/Containerfile` and `aap/Containerfile.ee` build.
  CI runs the static checks only. The upstream-based definitions pin
  `quay.io/ansible/ansible-rulebook:v1.3.2`, the tested ansible-rulebook;
  `latest` on that repository is not guaranteed to be the newest release.
- A rulebook activation launching the controller-tier approval workflow:
  the platform capacity rulebook hands the event to the orchestrator
  (verified above) or launches the request job; it does not launch the
  controller workflow.
- `tools/ao_sync.py` deleting or unpublishing anything: by design it never
  does, so the live run exercised create, update and unchanged only.
- The platform's Slack notification template on a job failure: its
  approval-pending post is verified (above); no job has failed since it
  was attached. The bot deciding a controller workflow approval: the card
  is posted; the click is a person's.
- Scale beyond two arrays.
- Drive, controller or PSU failure and real performance degradation:
  fixtures only (`tests/fixtures/`), though the replayed events drive the
  real identify LED.
- Array connection path failure or throttle changes: not induced.
- QoS under synthetic load: thresholds are lowered on a volume with live
  IOPS; no load generator is run.
- Packaging into `everpure.flasharray` and the Red Hat EDA certification
  track ([PACKAGING.md](PACKAGING.md)).
