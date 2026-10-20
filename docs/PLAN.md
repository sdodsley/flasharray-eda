# Event-Driven Ansible for standalone FlashArrays: evaluation and plan

Date: 2026-10-20. Grounded in py-pure-client 1.94.1 (REST 2.57),
ansible-rulebook 1.3.x, ansible.eda 2.13, AAP 2.5 automation decisions, and
the Fusion MCP -> EDA loop proven on lab arrays on 2026-10-20.

## 1. Verdict

A standalone FlashArray is a better EDA source than Fusion was. The array
already has:

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
| Alerts | `/alerts/events` (cursor on `time`), fallback `/alerts` (cursor on `updated`) | code, severity, state, component_type/name, actual, expected, issue, summary, action, knowledge_base_url | 2.50 / 2.0 |
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
| Pull | one asyncio source plugin, N arrays, per-signal intervals, py-pure-client | poll interval | an API token per array |
| Push | syslog listener plugin (TCP/UDP 514) parsing `purity.audit [user]` and `purity.alert` | sub-second | a syslog target on the array |
| Zero custom code | native OpenMetrics exporter -> Prometheus rules -> Alertmanager -> `ansible.eda.alertmanager` | scrape + rule eval | an existing Prometheus |

`ansible.eda` ships alertmanager, webhook, kafka, journald, file_watch,
url_check, tick, generic, pg_listener and cloud queue sources. There is no
syslog or SNMP source, so push needs our own plugin. SNMP adds nothing over
syslog here because syslog also carries audits.

## 4. Ideas by domain

### 4.1 Drift control becomes change control

- Rule fires on the audit record itself, not on the next poll.
- Governed objects + automation service account: any write by another
  identity is reverted from the git baseline and reported with user, interface
  and command.
- Git webhook source: merges apply forward. Audit stream reconciles backward.
  A Kubernetes-style reconciler with the collection as actuator.
- Security posture in the baseline: management access, network access and TLS
  policies, SSO/SAML, lockout, eradication config, SafeMode retention. Scored
  against secure-by-default.
- Scheduled full reconcile: `tick` -> collection playbook in check/diff mode
  against desired state; changed tasks become drift events.

### 4.2 Let the array hold the thresholds and the baseline

- `/alerts/rules` sets capacity warning levels on the array. EDA receives a
  clean alert rather than comparing numbers.
- Performance abnormality = current sample vs the same window last week from
  `/arrays/performance`. No TSDB needed.
- Top-N volumes via REST `sort` and `limit`, not pagination.

### 4.3 Hardware events get hands

- Failed drive/PSU/fan: identify LED on (`purefa_hardware`), time-boxed remote
  assist (`purefa_ra`), ticket with action text and KB link.
- Controller failover / port down: host-side checks on Linux multipath,
  vSphere paths, Cinder backends. Switch-side checks via network collections.

### 4.4 Replication becomes an RPO contract

- Pod replica link lag > RPO: escalate; recovery_point tracked as a fact.
- Pgroup transfer whose `progress` is unchanged over N polls: replicate-now
  snapshot, or pause/resume the link.
- Array connection `partially_connected`: run `/array-connections/path` and
  ticket the failing path.
- ActiveCluster: pod resyncing on one side flips host `preferred_array` to the
  healthy side; flips back when online.
- Tick-driven DR drill: clone latest replicated snapshot on the target, mount
  and verify from a test host, destroy.

### 4.5 Capacity turns into trend and approval

- Days-to-full from space history; top growers from volume space deltas.
- Snapshot sprawl vs policy; pending-eradication reclaim.
- All destructive actions route to an approval node in an EDA Controller
  workflow (`run_workflow_template`).

### 4.6 Performance gets a safe actuator with hysteresis

- Array latency breach + one volume holding a large IOPS share: temporary QoS
  cap (`purefa_volume` / `purefa_qos_policy`).
- Second rule lifts the cap after N normal polls; state carried with
  `set_fact` / `retract_fact`.
- Cinder-backed volumes: retype to another backend instead.

### 4.7 Composite rule for the bad day

`all` condition with `timeout`: DRR drop + snapshot space growth + write
bandwidth surge + audit of snapshot destroys or retention edits. Response:
protection snapshot across every pgroup now, longer eradication delay
(ratchet-safe direction), page. Never shorten anything.

### 4.8 Suppression and a dead man's switch

- Active maintenance window is a fact every remediation rule checks.
- Maintenance window created (audit) -> silence Alertmanager / ServiceNow.
- Poll errors + management VIP `url_check` distinguish dead network from dead
  array.

## 5. Phased plan

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| 0 | Fork fusion-eda. One `purefa_events` source with signals alerts, audits, health, telemetry, replication. Port the three rulebooks. | Lab: replica link pause, NTP drift, alert-watcher test, identify LED, maintenance window, QoS cap under fio all fire and act |
| 1 | Audit-triggered drift, service account, git baseline repo. Maintenance-window suppression. Alert-code -> runbook map with action/KB forwarding. | Unsanctioned NTP change reverted within one poll; window suppresses it |
| 2 | Replication RPO engine, stuck transfer handling, path tests, ActiveCluster preferred-array shift, scheduled DR drill. | Lab two-array pair exercised end to end |
| 3 | Capacity trend + approval workflows in EDA Controller. QoS hysteresis loop. Composite anomaly rule. | Approval node proven; cap applied and lifted automatically |
| 4 | Syslog listener source. Prometheus rules file + Alertmanager rulebook. Package as `extensions/eda` in the collection with filters, fixtures, decision environment. Red Hat EDA certification track. | Collection release with EDA content |

## 6. Lab test matrix

Inducible safely: NTP/DNS drift, replica link pause/resume
(`purefa_pod_replica`), array connection throttle change, pgsnap replicate-now,
test alert (`/alert-watchers/test` -> `purity.test`), identify LED,
maintenance window create/delete, QoS cap on a test volume with fio, volume
create + write for capacity.

Not inducible: drive/controller/PSU failure, real degradation. Test those from
recorded fixtures through the `generic` source.

## 7. Traps and open questions

- ansible-rulebook: `throttle.once_within` must be a literal; `run_playbook`
  copies playbooks so pass everything as extra_vars; declare `localhost` in
  the inventory.
- Older Purity lacks `/alerts/events`: fall back to `/alerts` with an
  `updated` cursor.
- Per-array tokens: read-only role for the source, write role only for the
  remediation credential, both in the Controller credential store.
- Approval nodes should pause a Controller workflow launched from a rulebook
  exactly as a manual launch would. Not stated in the AAP 2.5 docs read;
  confirm first thing in Phase 3.
- Scale: one plugin, N arrays, reuse the REST client per array, stagger polls.

## 8. References

- Rulebook conditions: https://docs.ansible.com/projects/rulebook/en/latest/conditions.html
- ansible.eda sources: https://github.com/ansible/event-driven-ansible/tree/main/extensions/eda/plugins/event_source
- AAP 2.5 automation decisions: https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.5/html-single/using_automation_decisions/index
- EDA certified content: https://www.redhat.com/en/blog/event-driven-ansible-partner-ecosystem
- FlashArray OpenMetrics spec: https://github.com/PureStorage-OpenConnect/pure-fa-openmetrics-exporter/blob/master/specification/metrics/purefa-metrics.md
- Purity syslog message types: https://docs.cyderes.cloud/parser-knowledge-base/pure_storage/
