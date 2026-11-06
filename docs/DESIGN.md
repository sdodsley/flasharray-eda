# Design

Why a standalone FlashArray is an event source, what it emits, how events
reach ansible-rulebook, and the ideas each rulebook implements.

Versions this design assumes: py-pure-client 1.94.1 (REST 2.57 API ceiling;
the arrays it was verified against speak REST 2.46), ansible-rulebook 1.3.x,
ansible.eda 2.13, and the Ansible Automation Platform 2.5 automation
decisions documentation.

## 1. Why the array is the event source

A standalone FlashArray is a complete event source on its own; no control
plane, cloud service or monitoring stack is needed. The array already has:

- a cursorable alert event log (`/alerts/events`, REST 2.50+),
- an attributable change log (`/audits`: user, interface, command,
  subcommand, arguments),
- its own time series (`/arrays/performance` and `/arrays/space` accept a
  resolution and a start time, so the last week is queryable),
- native push paths (syslog `purity.alert` / `purity.audit` / `purity.test`,
  SNMP trap/inform v2c/v3) and, on Purity 6.7+, a native OpenMetrics exporter.

The leverage is not drift polling. It is three ideas:

1. treat the audit stream as the drift trigger (react to the change, with the
   user attached, seconds later),
2. let the array do its own thresholding (`/alerts/rules`) and baselining (its
   own history), so rules compare against something meaningful,
3. make the remediation cross-domain: hosts, QoS, tickets, replication,
   Cinder, not just email.

## 2. What the array can emit

| Signal | Endpoint(s) | Event fields | Min REST |
| --- | --- | --- | --- |
| Alerts | `/alerts/events` (cursor on `time`, REST 2.50+); on older Purity the plugin diffs `/alerts` with `state != closed` and derives opened / updated / closing / closed itself | code, severity, state, component_type/name, actual, expected, issue, summary, action, knowledge_base_url | 2.50 / 2.0 |
| Config changes | `/audits` (cursor on `time`) | user, user_interface, origin, command, subcommand, arguments, system_event | 2.0 |
| Hardware | `/hardware`, `/drives`, `/controllers` | status != ok, temperature, voltage, speed, identify_enabled, controller mode/mode_since | 2.0 |
| Capacity | `/arrays/space` (history), `/volumes/space` | used, data_reduction, snapshot space, per-volume delta | 2.0 |
| Performance | `/arrays/performance` (resolution, start_time), `/volumes/performance` (sort, limit) | latency, IOPS, bandwidth, queue depth; top-N volumes without paginating everything | 2.0 |
| Replication | `/array-connections` (+ `/path`), `/pod-replica-links` (+ `/lag`), `/protection-group-snapshots/transfer`, `/pods/performance/replication` | connection status, replication_transport, throttle; link status, lag, recovery_point, paused, direction; transfer progress, started, completed | 2.0 - 2.52 |
| Suppression | `/maintenance-windows` | created, expires | 2.2 |
| Self-checks | `/alert-watchers/test`, `/log-targets/syslog/test`, `/snmp-managers/test`, `/support/test` | pass/fail per destination | various |

Alert payloads carry the recommended `action` text and a `knowledge_base_url`.
Forward both into tickets.

## 3. Transports

| Transport | How | Latency | Customer needs |
| --- | --- | --- | --- |
| Pull | `purefa_events`: one asyncio source plugin, N arrays (verified with two), six signals each on its own interval, one asyncio task per signal, SDK calls in a thread pool, one py-pure-client client per array rebuilt after a failure, every REST call bounded by `request_timeout` | poll interval | a read-only API token per array |
| Push | `purefa_syslog`: UDP and TCP on one port (default 5140; 514 needs root or a capability), RFC 3164 and 5424 headers, `purity.audit`, `purity.alert` and `purity.test` recognised, audit `Key: 'value'` pairs parsed into fields (user, action, result, interface, session, location); `syslog.yml` raises the fast notification for a human's action and for alerts while the REST plugin owns the full record | sub-second | a syslog target on the array |
| Zero custom code | native OpenMetrics exporter (Purity//FA 6.7+) -> `prometheus/flasharray.rules.yml` -> Alertmanager -> `ansible.eda.alertmanager` -> `alertmanager.yml` | scrape + rule eval | an existing Prometheus |

The Prometheus rules cover hardware and drive status, capacity and
days-to-full, data reduction drop, read and write latency against last
week, replica link lag and state, stretched-pod mediator state and scrape
failure; each carries a `domain`
label. `alertmanager.yml` maps them to the same playbooks: hardware alerts
drive the identify-LED playbook, capacity alerts raise the approval request,
the rest notify, resolved alerts are logged. No code from this project runs
on the array side of that path.

`ansible.eda` ships alertmanager, webhook, kafka, journald, file_watch,
url_check, tick, generic, pg_listener and cloud queue sources. There is no
syslog or SNMP source, so the push path needs a plugin of its own
(`purefa_syslog`). SNMP adds nothing over syslog here because syslog also
carries audits.

All three transports feed the same rulebooks and playbooks; the diagram at
the top of [README.md](../README.md) shows the paths, and
[The source plugins](../README.md#the-source-plugins) lists the arguments
and event fields.

## 4. Ideas by domain

Each bullet names the rulebook or playbook that implements it, or is marked
design intent.

### 4.1 Drift control becomes change control

- The rule fires on the audit record itself, not on the next poll
  (`change_control.yml`, audits signal).
- Governed objects and an automation service account: a governed command by
  any identity not in `automation_users` is reconciled from the git baseline
  (`playbooks/reconcile.yml`; schema in `baseline/README.md`) and reported
  with user, interface and command; destructive commands by humans are
  reported; the automation's own changes are logged.
- Git webhook source: a signed push applies the baseline forward to every
  array (`playbooks/apply_baseline.yml`); the audit stream reconciles
  backward. A Kubernetes-style reconciler with the collection as actuator.
- Security posture in the baseline: management access, network access and TLS
  policies, SSO/SAML, lockout, eradication config, SafeMode retention, scored
  against secure-by-default. `config_drift.yml` ships the poll-based form for
  NTP, DNS, LDAP, SafeMode/eradication and admin auth settings (NTP and DNS
  reapplied, the rest notify); the wider posture score is design intent.
- Scheduled full reconcile: `tick` -> collection playbook in check/diff mode
  against desired state, changed tasks becoming drift events. Design intent;
  not in the shipped rulebooks.

### 4.2 Let the array hold the thresholds and the baseline

- Performance abnormality = the current sample against the same half hour
  last week from `/arrays/performance` (`performance.yml`, array write
  latency). No TSDB needed.
- Top-N volumes via REST `sort` and `limit`, not pagination (the telemetry
  signal's `/volumes/performance` top N; a volume tagged `eda/qos_cap` is
  always included whatever its rank).
- Capacity: days-to-full from the array's own 7-day space history and top
  growers from per-volume space deltas (telemetry signal); `capacity.yml`
  compares used % with a per-site threshold.
- `/alerts/rules` setting capacity warning levels on the array, so EDA
  receives a clean alert rather than comparing numbers: design intent; the
  shipped thresholds live in the vars file.

### 4.3 Hardware events get hands

- A hardware component not ok: identify LED on (`purefa_hardware`, via
  `identify_led.yml`) and notify, logged only inside a maintenance window; a
  drive not healthy: notify. Opened alerts (`alerts.yml`) route through the
  alert-code runbook map (`alert_runbooks`, `alert_runbook.yml`) to notify,
  ticket, or
  ticket plus per-code tasks under `playbooks/runbooks/<code>.yml`, carrying
  the array's own action text and knowledge base link; unknown codes take the
  default entry.
- Time-boxed remote assist (`purefa_ra`): design intent; `health.yml` logs
  remote-assist events.
- Controller failover or port down: `health.yml` notifies. Host-side checks
  on Linux multipath, vSphere paths and Cinder backends, and switch-side
  checks via network collections, are design intent.

### 4.4 Replication becomes an RPO contract

Every action sits behind a per-site switch (`replication_auto_replicate_now`,
`activecluster_auto_prefer`) that falls back to notify.

- Pod replica link lag over the RPO: escalation after N breached polls; the
  recovery point, the breach streak and the pause duration travel on the
  event.
- A replicating protection group that is overdue, or whose transfer is stuck
  (progress unchanged over N polls): replicate now (`replicate_now.yml`,
  `purefa_pgsnap` with `remote: true`, the immediate replicated snapshot).
- Array connection `partially_connected`: the replication signal reads
  `/array-connections/path` and the failed paths are notified; paused or
  unhealthy links likewise.
- ActiveCluster: a stretched pod losing a member moves host `preferred_array`
  to the online side and back on recovery (`preferred_array.yml`): hosts are
  discovered from the pod's volume connections and changed with
  `purefa_host`; hosts with an existing preference are left alone and
  reported; the peer array's hosts (listed as `<array>:<host>`) are skipped.
- Tick-driven DR drill (`dr_drill.yml`, `dr_drill_one.yml`): copy one member
  volume out of the latest fully transferred snapshot on the target, verify
  its size against the snapshot, destroy and eradicate the copy. Mounting the
  copy from a test host is design intent.
- ActiveDR failover test (`dr_promote.yml`, `dr_demote.yml`, driven by
  `orchestrator/dr_failover_test.json`): behind an approval, promote the
  replica link's target pod on the DR array, run the application test
  outside the workflow, then behind a second approval demote it again;
  writes made during the test are discarded and the source pod is never
  touched.

### 4.5 Capacity turns into trend and approval

- Days-to-full from space history; top growers from volume space deltas;
  what is pending eradication as the reclaim candidates.
- Snapshot sprawl against policy: design intent.
- Every destructive action sits behind an approval. Standalone, a capacity
  breach writes a request file (`capacity_request.yml`: request id
  `<array>-<timestamp>`, the days-to-full estimate, the top growers, what is
  pending eradication, the arrays with headroom) and a webhook ruleset takes
  `{request_id, approve, approver, action}` under a bearer token; only an
  approved request whose action was proposed runs, and `eradicate_pending`
  (`capacity_reclaim.yml`) eradicates destroyed volumes and snapshots,
  reporting anything the array refuses. On Ansible Automation Platform the
  webhook ruleset gives way to a workflow whose first node is a manual
  approval (`run_workflow_template`; `aap/seed.yml` creates it), and
  `orchestrator_enabled` hands the event to the automation orchestrator
  instead ([ORCHESTRATOR.md](ORCHESTRATOR.md)). Both platform paths are
  exercised, the workflow by a launch through its approval node and the
  handoff end to end against a live orchestrator; the launch from a
  running rulebook activation is not ([VERIFICATION.md](VERIFICATION.md)).

### 4.6 Performance gets a safe actuator with hysteresis

- An array latency breach with one volume holding a large share of its IOPS
  for N polls: a temporary IOPS cap (`qos_cap.yml`, `purefa_volume`), behind
  `qos_auto_cap`.
- A second rule lifts the cap after M normal polls (`qos_lift_normal_polls`)
  and restores the previous limit.
- State lives on the array, not in rulebook facts: the cap writes volume tags
  in namespace `eda` (`qos_cap` = when it was applied, `qos_prev_iops` = the
  limit before), the telemetry signal reads them back as `eda_capped`, and
  the lift removes them, so a rulebook restart loses nothing.
- Cinder-backed volumes: retype to another backend instead. Design intent.

### 4.7 Composite rule for the bad day

`anomaly.yml`: one ruleset, two signals (telemetry and audits), an `all`
condition with a 15 minute `timeout` joining a data reduction drop, a snapshot
or write surge and a human's destructive snapshot command on the same array.
Response (`protect_now.yml`): a protection snapshot across every local
protection group now, a longer enabled eradication delay (ratchet-safe
direction) and a page, which is a critical record in the alert log. Nothing is
shortened. The rule carries no throttle: ansible-rulebook rejects
`once_within` on a multi-event condition, so the timeout bounds re-firing
([HARDWARE_NOTES.md](HARDWARE_NOTES.md)).

### 4.8 Suppression and a dead man's switch

- An active maintenance window is stamped on every alert, audit, hardware and
  replication event as `maintenance_active`, from one cached
  `/maintenance-windows` read per array, so every remediation rule gates on
  it without cross-signal facts.
- A maintenance window created (seen in the audit stream) silencing
  Alertmanager or ServiceNow: design intent.
- Poll failures arrive as `purefa_poll_error` and polling continues; a
  management VIP `url_check` to tell a dead network from a dead array is
  design intent.

## 5. Gates

Every remediation passes the same gates; the table in
[What it does](../README.md#what-it-does) shows them per rulebook.

- `maintenance_active` on the event, from one cached `/maintenance-windows`
  read per array.
- `automation_users`, so the automation never reacts to itself.
- Per-site switches `qos_auto_cap`, `activecluster_auto_prefer`,
  `replication_auto_replicate_now` and `orchestrator_enabled` that fall back
  to notify.
- State that must survive a restart lives on the array or in a file, never
  in a rulebook fact.

## 6. Traps

- ansible-rulebook's own limits (no filters or arithmetic in conditions, a
  literal `once_within` that cannot sit on a multi-event condition, one
  untemplated `--vars` file, `run_playbook` copying only the playbook's
  directory, `localhost` in the inventory) are listed in
  [HARDWARE_NOTES.md](HARDWARE_NOTES.md).
- `/alerts/events` needs REST 2.50; on older Purity the alerts signal diffs
  `/alerts` with `state != closed` and derives the opened / updated / closing
  / closed transitions itself.
- Per-array tokens: a read-only role for the source plugin, a write role only
  for the remediation credential, both in the platform's credential store
  ([SECURITY.md](../SECURITY.md)).
- Whether a Controller workflow approval node pauses a workflow launched from
  a rulebook exactly as a manual launch does is not stated in the AAP 2.5
  documentation; the manual launch is verified, the rulebook launch is not
  ([VERIFICATION.md](VERIFICATION.md)).
- Scale: one plugin instance polls N arrays with one REST client per array;
  verified with two arrays, larger fleets untested.

## 7. Deployment

`decision-environment/` holds a Containerfile and an `ansible-builder` v3
definition, both on `quay.io/ansible/ansible-rulebook:v1.3.2`, that produce
the same tree: the collection `sdodsley.flasharray_eda` built from the
checkout and installed with its dependencies, py-pure-client, and the
rulebooks and playbooks as plain files. The base image ships
ansible-rulebook, ansible-core and aiohttp. EDA Controller activations
resolve custom event sources from the collections in the image, so the
rulebooks name theirs by FQCN; on the platform they run as the generated
variants under `extensions/eda/rulebooks/platform/`, where `run_playbook`
becomes `run_job_template` (`aap/README.md`, [PACKAGING.md](PACKAGING.md)).

## 8. References

- Rulebook conditions: https://docs.ansible.com/projects/rulebook/en/latest/conditions.html
- ansible.eda sources: https://github.com/ansible/event-driven-ansible/tree/main/extensions/eda/plugins/event_source
- AAP 2.5 automation decisions: https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.5/html-single/using_automation_decisions/index
- EDA certified content: https://www.redhat.com/en/blog/event-driven-ansible-partner-ecosystem
- FlashArray OpenMetrics spec: https://github.com/PureStorage-OpenConnect/pure-fa-openmetrics-exporter/blob/master/specification/metrics/purefa-metrics.md
- Purity syslog message types: https://docs.cyderes.cloud/parser-knowledge-base/pure_storage/
