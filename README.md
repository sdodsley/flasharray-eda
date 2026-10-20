# flasharray-eda

Event-Driven Ansible for standalone Everpure FlashArrays.

A FlashArray already exposes the three things an event-driven loop needs:
a cursorable alert log, an attributable audit log, and its own performance
and capacity history. This project turns those into `ansible-rulebook`
events and acts on them through the `everpure.flasharray` collection. No
Pure1, no Fusion and no external monitoring stack are required; a
Prometheus/Alertmanager path exists for sites that already run one.

Status: all five phases of [docs/PLAN.md](docs/PLAN.md) are implemented and
exit-tested against two Purity//FA 6.10.6 arrays (2026-10-20). Records per
phase: [PHASE0](docs/PHASE0.md) sources and rulebooks, [PHASE1](docs/PHASE1.md)
change control, [PHASE2](docs/PHASE2.md) replication, [PHASE3](docs/PHASE3.md)
capacity, QoS and the anomaly rule, [PHASE4](docs/PHASE4.md) push, Prometheus
and packaging. This is a prototype, not a supported product.

## What it does

| Rulebook | Source | Fires on | Action |
| --- | --- | --- | --- |
| `alerts.yml` | alerts | alert opened / closed | routes through the alert-code runbook map: notify, ticket, or per-code tasks, with the array's own action text and KB link |
| `change_control.yml` | audits, webhook | a governed setting changed by anyone but the automation account; a destructive command by a human; a git push | reconcile from the git baseline (NTP, DNS) and report who; notify; apply the baseline forward |
| `config_drift.yml` | config | NTP, DNS, LDAP, SafeMode/eradication, admin auth settings vs baseline (poll-based fallback) | NTP and DNS reapplied; the rest notify |
| `health.yml` | health | hardware component not ok, drive not healthy, controller failover, maintenance window, remote assist | identify LED on the component + notify, suppressed inside a window |
| `replication.yml` | replication, tick | connection down or paths failed; replica link paused, unhealthy, lag over RPO (escalates after N polls); stretched pod member offline / recovered; protection group overdue or stuck; a schedule | notify; move host preferred arrays to the online side and back; replicate now; DR drill (copy a volume from the latest replicated snapshot on the target, verify, destroy) |
| `capacity.yml` | telemetry, webhook | used % over threshold; days-to-full from the array's 7-day history; an approval | approval request with request id; reclaim pending-eradication objects only when approved |
| `performance.yml` | telemetry | noisy neighbour on a hot array; capped volume back to normal; array write latency vs the same half hour last week | temporary IOPS cap with an `eda/qos_cap` tag on the array; cap lifted; notify |
| `anomaly.yml` | telemetry + audits | data reduction drop + snapshot or write surge + a human destroying snapshots, all on one array inside 15 minutes | snapshot every local protection group now, lengthen the eradication delay, page |
| `syslog.yml` | purefa_syslog | `purity.audit` / `purity.alert` / `purity.test` lines pushed by the array | fast notification seconds after the fact |
| `alertmanager.yml` | ansible.eda.alertmanager | alerts from `prometheus/flasharray.rules.yml` on the native OpenMetrics exporter | the same playbooks, no code of ours on the array side |

Every remediation is gated: `maintenance_active` on the event (one cached
`/maintenance-windows` read per array), an `automation_users` list so the
automation never reacts to itself, and per-site switches
(`qos_auto_cap`, `activecluster_auto_prefer`, `replication_auto_replicate_now`)
that fall back to notify.

## Layout

```
extensions/eda/plugins/event_source/   purefa_events.py (REST pull, six signals), purefa_syslog.py (push)
extensions/eda/rulebooks/              one rulebook per domain (table above)
playbooks/                             notify, remediate_*, reconcile, apply_baseline, identify_led, replicate_now,
                                       preferred_array, dr_drill, capacity_request/reclaim, qos_cap, protect_now,
                                       alert_runbook (+ runbooks/<code>.yml), set_dns
baseline/                              desired state per array (the git baseline; README has the schema)
prometheus/flasharray.rules.yml        alert rules for the native exporter
decision-environment/                  Containerfile, ansible-builder definition, requirements
vars/example.yml, inventory/example.yml  copy to vars/local-<site>.yml and inventory/local-<site>.yml (gitignored)
tests/                                 lab induction playbooks, fixtures, replay rulebooks, e2e_phase*.sh
docs/                                  PLAN.md and the PHASE records
run.sh                                 launcher
```

## The source plugins

`purefa_events` polls one or more arrays with py-pure-client and emits plain
fact events; policy lives in the rulebooks. One instance can run several
signals, each on its own interval:

| Signal | Endpoints | Events |
| --- | --- | --- |
| alerts | `/alerts` (state != closed), diffed locally | `purefa_alert` with transition initial / opened / updated / closing / closed |
| audits | `/audits` newest N, cut at a local time cursor | `purefa_audit` with user, command, subcommand, arguments, by_automation |
| health | `/hardware`, `/drives`, `/controllers`, `/maintenance-windows`, `/support` | `purefa_health`, `purefa_hardware` per issue, `purefa_controller_failover` |
| telemetry | `/arrays/space` (+ 7-day history), `/volumes/space`, `/arrays/performance` (+ same half hour last week), `/volumes/performance` top N, volume QoS and tags | `purefa_capacity` (days_to_full, top growers, pending eradication, drr_drop, snapshot_surge), `purefa_array_performance` (baseline ratios, write_surge), `purefa_volume_performance` (streak counters, iops share, QoS limit, eda_capped) |
| replication | `/array-connections` (+ paths), `/pod-replica-links`, `/pods` (stretched), `/protection-groups` (+ targets, transfers) | `purefa_array_connection` (failed paths), `purefa_replica_link` (rpo_breached, breach streak, paused_for_s), `purefa_stretched_pod` (online/offline arrays, recovered), `purefa_pg_replication` (overdue, stuck) |
| config | `/arrays`, `/dns`, `/directory-services`, `/admins/settings`, `/syslog-servers`, `/smtp-servers`, `/alert-watchers`, `/support` | `purefa_config` |

Every event carries `array`; alert, audit, hardware and replication events
also carry `maintenance_active`; poll failures arrive as `purefa_poll_error`
and polling continues. Full argument and field lists are in the plugin
docstrings.

`purefa_syslog` listens on UDP and TCP for the array's own syslog and emits
`purefa_syslog` with the audit fields parsed.

## Running it

```
python3 -m venv venv && . venv/bin/activate
pip install ansible-rulebook ansible-core py-pure-client
ansible-galaxy collection install everpure.flasharray ansible.eda
cp vars/example.yml vars/local-lab.yml        # edit: arrays, baselines, thresholds, switches
cp inventory/example.yml inventory/local-lab.yml
export ARRAY_A_URL=https://... ARRAY_A_TOKEN=...   # or an env file, see run.sh
./run.sh extensions/eda/rulebooks/alerts.yml -v
```

Java 17+ must be on the path for ansible-rulebook. Tokens and URLs reach
the plugin through `api_token_env` / `fa_url_env`, so no vars file holds a
secret. Give the polling token a read-only array role; the plugin only
issues GETs. The remediation playbooks use the inventory credential, which
should be a dedicated service account named in `automation_users`.

In Ansible Automation Platform the rulebooks run as activations with the
decision environment under `decision-environment/`; replace `run_playbook`
with `run_job_template` and point the capacity rule at a workflow whose
first node is a manual approval (the webhook approval ruleset is the
stand-in for that).

## Things the hardware taught us

- `/audits` rejects a filter on `time`; `/alerts/events` needs REST 2.50;
  history resolutions are fixed (30 s, 5 min, 30 min, 2 h, 8 h, 1 day);
  maintenance windows take 3600 to 172800 seconds; the alert watcher test
  needs watcher names.
- Purity logs NTP changes as `purearray setattr --ntpserver`, not `purentp`.
- `purefa_hardware` (py-pure-client 1.94.1) sends an empty PATCH body for
  the identify LED, and `purefa_dns` 1.44.0 refuses a management DNS
  configuration that also serves file DNS. Both are called through the SDK
  here until the collection fixes ship.
- ansible-rulebook: no filters or arithmetic in conditions (the plugin emits
  the booleans), `once_within` must be a literal, `run_playbook` copies the
  playbook alone (absolute paths for included task files), `--vars` takes one
  untemplated file, `localhost` must be in the inventory.
- State that must survive a rulebook restart lives on the array: the QoS
  cap is a volume tag in namespace `eda`, the approval request is a file.

## Testing

`tests/e2e_phase0.sh` to `tests/e2e_phase3.sh` run each phase against the
lab, inducing what can be induced (NTP drift, a second user's changes, a
paused replica link, a maintenance window, a pending-eradication volume, a
busy volume capped and uncapped) and replaying recorded fixtures for what
cannot (hardware failures, a degraded stretched pod, the anomaly signals);
the replayed events still drive the real actions on the array.
`tests/syslog_send.py` exercises the syslog listener.

## Related

- Predecessor: Fusion MCP -> EDA, the runnable follow-up to the
  "Fusion MCP + Ansible thought experiment" blog.
- Collection: https://github.com/Everpure-Ansible/FlashArray-Collection
- Rulebook docs: https://docs.ansible.com/projects/rulebook/en/latest/

## Licence

Apache 2.0, see [LICENSE](LICENSE).
