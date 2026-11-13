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
| health, hardware replay | replays four fixture events: a critical controller, an unhealthy bay inside a window, a controller failover, the controller recovered | the identify LED lights on the named controller of the real array, the in-window bay is logged not actioned, the failover notifies, the recovered event clears the LED (the helper clears it only as a fallback) |
| audits | runs the audits signal with a lookback | every change made by the tests appears with user, command, subcommand and arguments |
| test alert | runs `tests/send_test_alert.py` | the array sends its test alert to the configured watcher |
| capacity, performance steady state | runs the telemetry signal on an idle array | events flow, nothing fires |

The loop's own failure path, standalone: the alerts rulebook run against
one array whose URL was a closed port, polling every two seconds, wrote a
warning record on the first failed poll ("alerts poll failed") and a
critical one at the tenth ("poll has failed 10 times in a row"), then
nothing more across 38 failures in 75 seconds, the throttles holding; the
plugin's `consecutive` count and `escalate` flag drove both. Slack was
kept out of that run, so the records are the proof.

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

### Run twice (2026-11-13, standalone against the arrays)

Every template launched twice with the same extra vars, seconds apart,
from `tests/e2e_run_twice.sh`: expand volume grew a scratch volume to 2G
and the second run reported it already at that size (changed false,
`already_at_size` true); replicate now took a replicated snapshot of the
`simon` group and the second run, 3 s later, requested nothing (inside the
cooldown, `skipped` true); protect now snapshotted the two local groups
that hold volumes (the array refuses a snapshot of an empty group, four
of the six, recorded as failures as before) and the second run took
nothing (`skipped` true); it ran with `extend_eradication_by_s` 0, so the
guard against lengthening the delay twice is unit-tested only; the capacity request wrote
one request file for one `request_key` and the second run reused it
(`already_raised`, one file); the Alertmanager silence created once and
the second create reused the same id, the first expire changed and the
second read it already expired; the DR switchover ran twice each way on
the lab link, the second run of each direction changing nothing. The
reclaim's already-done path is pinned by the static test only: array A
refuses eradication (`Eradication is disabled`), so no reclaim ran live.
The bridge's pending check ran
against the mock orchestrator in the unit tests; its first platform run
follows this commit, the platform running the committed tree. The drill's
in-flight skip and a demotion retried inside its quiesce are unit-tested
only (both need two overlapping launches).

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

The QoS cap was then exercised under a real load. The platform host
became an iSCSI initiator for a throwaway 20 GB volume on array A (one
portal, fio 4k random reads, queue depth 32 on four jobs), and the day-2
cap was requested at 500 IOPS from its form and approved from Slack
while the load ran, twice. On a volume created with its own limit of
1000 IOPS the array's counters went from about 1000 reads a second to
500 when the cap job wrote the tags (`qos_prev_iops: 1000`) and the
limit, and back to 1000 when the lift job restored the limit and removed
the tags. On a volume created without a limit the same cycle ended with
the volume reading no limit at all: the lift removes the limit through
the API with an empty string, because the module's "clear" (0) through
everpure.flasharray 1.46 sets the array's maximum 100000000, which reads
back as a limit and shows as 100M (docs/HARDWARE_NOTES.md; the
collection's master fixed it the same day). The first attempt found two defects the
tests now cover. Two lift jobs ran 19 seconds apart; the second read a
volume with the limit restored and the tags already gone and cleared it
to the maximum, so both modes are idempotent now (a cap on a tagged
volume and a lift on an untagged one change nothing). And a cap on a
volume whose latency was already normal was lifted at the next poll
("normal for 10 polls"), because the lift's count ran from whenever the
volume last read slow; the telemetry signal now starts it at the cap,
and the performance activation runs on the rebuilt image that carries
it (the change is unit-tested; the next cap will show it). The automatic
noisy-neighbour rule itself did not fire from this host: the array's
service time for 4k reads stayed near 70 µs with the initiator seeing
milliseconds, and 1 MiB reads on four paths reached 60 MB/s at 200 to 330
µs on the array and a quarter of its IOPS, so neither the 2 ms volume
latency nor the 1 ms array latency the rule needs was approached; the
array-side latency the plugin reads does not include the wire, and a
real hot array is still the only way to fire that rule honestly.

The two ansible-builder images on the supported bases ran on the platform.
Both were built with ansible-builder 3.2 on a `vfs` image store seeded
with the platform's own copies of `ee-supported-rhel9` and
`de-supported-rhel9` (no registry login on the host), and loaded into
the stores the platform uses rather than pulled: the execution
environment from `aap/execution-environment.yml` (4.2 GB; ansible-core
2.16, `everpure.flasharray` 1.46.0, py-pure-client on the Python Ansible
runs on) into the controller's task container, the decision environment
from `aap/decision-environment.yml` (2.9 GB; ansible-rulebook, the three
collections, py-pure-client) into the activation workers' store. On the
execution environment, registered as a throwaway EE object, copies of the
notify and switchover templates ran: the notify posted to Slack in 28
seconds, and the switchover launched against a pod that does not exist
reached the array through py-pure-client and stopped at its guard with
the array's own "Pod does not exist." On the decision environment,
registered as a throwaway DE object with the pull policy `missing` (the
API's default `always` makes the worker pull `localhost/...` and fail on
the registry certificate, docs/HARDWARE_NOTES.md), the alertmanager
activation was recreated, ran in a container from that image, took a
posted replica-link alert through its rule and launched the notify job.
The activation was then recreated on the project's own decision
environment and the throwaway objects and images removed. The images
that run the lab day to day remain the ones `decision-environment/
Containerfile` and `aap/Containerfile.ee` build.

The platform's Slack notification template also fired on a job failure:
the switchover launched against a pod that does not exist failed at its
guard and the platform posted "Job #261 'FlashArray - DR switchover'
failed" with the job's link (the notification record on the job reads
`successful`). That failure also showed the guards' messages
mis-rendering for a missing pod, fixed since.
The ActiveDR failover test (`orchestrator/dr_failover_test.json`) runs
against the replica link's target pod on the second array: after the
first approval `dr_promote.yml` promotes the pod (its replica link stays
replicating, the source pod is untouched) and the notify carries the
states before and after; after the second approval `dr_demote.yml`
demotes it with quiesce, discarding the writes made during the test, and
the pod is a replica-link target again. A test run the same day as two
switchovers of the link found the revert refused: the earlier demotion's
undo-demote pod was still on the array and the demote job failed with the
module's message, leaving the pod promoted until the undo-demote pod was
eradicated by hand and the job relaunched. Since then `dr_demote.yml`
eradicates such pods first when the workflow passes
`eradicate_undo_demote`, which the definition's revert node now does; a
test run inside the delay then showed it, the revert's job eradicating the
earlier undo-demote pod before demoting, both approvals from Slack. Both
playbooks refuse a pod that
is not the target of a replica link: Purity counts the sources linked to
a pod in `link_source_count`, so a target pod has a source count above
zero and an inbound link. The planned failover and failback
(`orchestrator/dr_failover_failback.json`, `playbooks/dr_switchover.yml`)
runs on the lab's ActiveDR link, whose two pods hold no volumes: after the
first approval the switchover job demotes the source pod with quiesce,
sees the link report the copy complete, promotes the target pod and
records the link reversed (the target's link outbound and replicating), in
72 seconds; after the second approval the same template with the sides
swapped puts the link back, and both pods read as they started. Both
approvals of that first run were given in the orchestrator's UI: the
execution had been created when the nodes named the `admins` group alone,
and a service account is not a user the orchestrator will list as an
approver, which is why the shipped definitions now name an `approvers`
group as well.

The run was repeated once the definitions were republished with that group
and the bot logged in as a user in it. The failover approval was decided
from Slack within ten seconds of the card and recorded as that user with
the Slack name in the notes, and the switchover job then stopped at the
demotion: the first run had left `test.undo-demote.5` on array A, a
destroyed pod holding the pod's state from before that demotion, with 288
hours on its timer, and the pod cannot be demoted again while it exists
(the module refuses; docs/HARDWARE_NOTES.md). With the playbook listing
those pods and eradicating them when `eradicate_undo_demote` is set, which
the definition now does, the next run went through on two Slack clicks:
the failover eradicated that pod, demoted the source and promoted the
target in 77 seconds, and the failback eradicated array B's
`test-rl.undo-demote.21` from the first run's failback, demoted the target
and promoted the source again in 75 seconds, both pods and the link reading
as they started. Two things rode along. The platform's gateway answered
the failback's launch with a 503 after the controller had created the
job, so the orchestrator retried the node and launched the template a
second time 80 seconds later; the second job found the source demoted and
the target promoted, changed nothing and said so, and it is that job the
execution recorded. A launch refused with a 503 is not a launch that did
not happen, and the playbook's rule of leaving a side that is already
where it should be is what made the duplicate harmless. And the
replication rulebook, polling the link, saw it quiesced during the planned
demotion and posted a critical notify about it: to a monitor that does
not know about it, a planned switchover looks like a stalled link.

With the switchover marking its source pod as planned work and the
replication plugin carrying the mark to the link's other side, the same
failover and failback ran again on two Slack clicks (2026-11-12): the
failover eradicated `test.undo-demote.6`, demoted the source and promoted
the target, the failback did the same the other way, and for the whole
window the replication rulebook recorded only "Replica link changing
during planned work - log only", twice per poll, with no critical notify;
the Alertmanager silence nodes ran before and after each step and
recorded the Alertmanager as unreachable rather than failing, that
instance listening on its loopback alone. The first attempt had tagged
the target pod as well and stopped there: a demoted pod refuses tag
writes (docs/HARDWARE_NOTES.md).

The alert lifecycle ran against live alerts the same day. Array B's
alert 13 went to `closing` after its NTP servers were set again and the
alerts activation posted "alert 13 resolved" at info, one step below
the alert's warning, for each of the two ntpd alerts that closed. A pod
stretched from array A to B and back raised alerts 120 and 152 that
opened and resolved within seconds, inside one poll: the activation
routed them as opened (the critical runbook ran for an alert already
resolving) and never saw them close, which is why the alerts rulebook
now notifies once for an alert first seen in the `closing` state instead
of routing it. Standalone replays against the arrays drove the two
actuators: the health fixtures lit the identify LED on array A's CT1 at
the critical event and cleared it at the recovered event (both reported
as changed, the array reading the LED off afterwards), and an alert 13
opened event for array B, with the site map routing code 13 to tasks,
ran runbook 13 and put the baseline NTP servers back in place of the
unroutable address the test had set.

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
The bot runs under identities made for it: an orchestrator user in the
`approvers` group the approval nodes name, the group holding the
project-user role on the project (a service account with the same role
lists the approvals but is refused the decision, 403, because approver
names resolve to users and groups), and a platform user with the Approve
role on the workflow; it polls both queues with them, and the orchestrator
records each decision as that user with the Slack name in the notes.

`tools/activation_audit.py` run against the platform found what it was
written for: nine of the ten activations carried the vars of the day they
were created (no orchestrator keys, no `syslog_programs`, one with the
lab's 60-second drill interval), and `--recreate` rebuilt them with the
current site vars, printing each old and new id. The controller refused
one create with "activation with this name already exists" after the
old id had left the list, which the tool now waits out; a second run
finished the fleet, and a read-only run then reported all ten clean.
`tools/ao_sync.py --check` against the live orchestrator exits 0 with the
nine definitions unchanged.

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
- The ansible-builder images under CI: CI runs the static checks only; the
  three definitions build on the lab host's `vfs` image store (rootless
  podman with native overlay there fails every multi-stage `COPY --from`,
  [PACKAGING.md](PACKAGING.md)) and the two supported-base images have run
  on the platform (above). The upstream-based definitions pin
  `quay.io/ansible/ansible-rulebook:v1.3.2`, the tested ansible-rulebook;
  `latest` on that repository is not guaranteed to be the newest release.
- A rulebook activation launching the controller-tier approval workflow:
  the platform capacity rulebook hands the event to the orchestrator
  (verified above) or launches the request job; it does not launch the
  controller workflow.
- `tools/ao_sync.py` deleting or unpublishing anything: by design it never
  does, so the live run exercised create, update and unchanged only.
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
