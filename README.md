# flasharray-eda

Event-Driven Ansible for standalone Everpure FlashArrays.

A FlashArray already exposes the three things an event-driven loop needs:
a cursorable alert log, an attributable audit log, and its own performance
and capacity history. This project turns those into `ansible-rulebook`
events and acts on them through the `everpure.flasharray` collection. No
Pure1, no Fusion and no external monitoring stack are required, although a
Prometheus/Alertmanager path is planned for sites that already run one.

Status: **Phase 0 complete** (one source plugin, six signals, six rulebooks,
exit-tested against two Purity//FA 6.10.6 arrays on 2026-10-20). See
[docs/PLAN.md](docs/PLAN.md) for the evaluation and the phases, and
[docs/PHASE0.md](docs/PHASE0.md) for the test record.

## What runs today

| Rulebook | Signal | Fires on | Action |
| --- | --- | --- | --- |
| `alerts.yml` | alerts | alert opened / closed, with the array's own action text and KB link | notify |
| `config_drift.yml` | config | NTP, DNS, LDAP, SafeMode/eradication, admin auth settings vs baseline | NTP and DNS reapplied from baseline; the rest notify only |
| `health.yml` | health | hardware component not ok, drive not healthy, controller failover, maintenance window, remote assist | identify LED on the component + notify, suppressed inside a maintenance window |
| `capacity.yml` | telemetry | used % over threshold, days-to-full from the array's 7-day space history | approval request (nothing is provisioned) / notify |
| `performance.yml` | telemetry | slowest volumes over N polls on a hot array, array write latency vs the same half hour last week | notify |
| `replication.yml` | replication | array connection down or paths failed, replica link paused / unhealthy / lag over RPO, stretched pod degraded, protection group transfer overdue or stuck | notify |

## Layout

```
extensions/eda/plugins/event_source/purefa_events.py   the source plugin (REST pull, six signals)
extensions/eda/rulebooks/                              one rulebook per domain
playbooks/                                             notify, remediate_ntp, remediate_dns, capacity_request, identify_led
vars/example.yml, inventory/example.yml                copy to vars/local-<site>.yml and inventory/local-<site>.yml (gitignored)
tests/                                                 lab induction playbooks, fixture replay, e2e_phase0.sh
docs/                                                  PLAN.md, PHASE0.md
run.sh                                                 launcher
```

## The source plugin

`purefa_events` polls one or more arrays with py-pure-client and emits plain
fact events; policy lives in the rulebooks. One instance can run several
signals, each on its own interval:

| Signal | Endpoints | Events |
| --- | --- | --- |
| alerts | `/alerts` (state != closed), diffed locally | `purefa_alert` with transition initial / opened / updated / closing / closed |
| audits | `/audits` newest N, cut at a local time cursor | `purefa_audit` with user, command, subcommand, arguments, by_automation |
| health | `/hardware`, `/drives`, `/controllers`, `/maintenance-windows`, `/support` | `purefa_health`, `purefa_hardware` per issue, `purefa_controller_failover` |
| telemetry | `/arrays/space` (+ 7-day daily history), `/volumes/space`, `/arrays/performance` (+ same half hour last week), `/volumes/performance` sorted top N | `purefa_capacity`, `purefa_array_performance`, `purefa_volume_performance` |
| replication | `/array-connections` (+ paths), `/pod-replica-links`, `/pods` (stretched), `/protection-groups` (+ targets, transfers) | `purefa_array_connection`, `purefa_replica_link`, `purefa_stretched_pod`, `purefa_pg_replication` |
| config | `/arrays`, `/dns`, `/directory-services`, `/admins/settings`, `/syslog-servers`, `/smtp-servers`, `/alert-watchers`, `/support` | `purefa_config` |

Every event carries `array`; poll failures arrive as `purefa_poll_error`
and keep polling. The full argument and field list is in the plugin
docstring.

Things learnt on hardware that shaped it:

- `/audits` rejects a filter on `time` ("Unknown attribute"), so the audit
  cursor is applied locally to the newest `audit_batch` records.
- `/alerts/events` needs REST 2.50; on 2.46 it demands alert ids. The plugin
  diffs `/alerts` instead, which also works everywhere.
- `/arrays/performance` and `/arrays/space` history accept only the fixed
  resolutions 30 s, 5 min, 30 min, 2 h, 8 h and 1 day.
- Server-side `sort` and `limit` on `/volumes/performance` make the top-N
  volume fetch two calls per array instead of a page walk.
- `not_installed` is the normal status of an empty bay; the health signal
  ignores it (and `unused`) by default.

## Running it

```
python3 -m venv venv && . venv/bin/activate
pip install ansible-rulebook ansible-core py-pure-client
ansible-galaxy collection install everpure.flasharray ansible.eda
cp vars/example.yml vars/local-lab.yml        # edit: arrays, baselines, thresholds
cp inventory/example.yml inventory/local-lab.yml
export ARRAY_A_URL=https://... ARRAY_A_TOKEN=...   # or an env file, see run.sh
./run.sh extensions/eda/rulebooks/alerts.yml -v
```

Java 17+ must be on the path for ansible-rulebook. Tokens and URLs reach
the plugin through `api_token_env` / `fa_url_env`, so no vars file holds a
secret. Give the polling token a read-only array role; the plugin only
issues GETs. The remediation playbooks use the inventory credential.

Known rulebook constraints (inherited from the Fusion prototype and still
true on ansible-rulebook 1.3.2): `throttle.once_within` must be a literal;
`run_playbook` copies playbooks into a temporary directory so everything
arrives as `extra_vars`; declare `localhost` in the inventory.

## Testing

`tests/e2e_phase0.sh` runs each rulebook against the lab, then induces NTP
drift, pauses a replica link, opens a maintenance window, replays recorded
hardware events through the identify-LED path and sends the array's test
alert. Hardware failures cannot be induced in a lab; those rules are
exercised from `tests/fixtures/` through `ansible.eda.generic`.

## Related

- Predecessor: Fusion MCP -> EDA, the runnable follow-up to the
  "Fusion MCP + Ansible thought experiment" blog.
- Collection: https://github.com/Everpure-Ansible/FlashArray-Collection
- Rulebook docs: https://docs.ansible.com/projects/rulebook/en/latest/

## Licence

Apache 2.0, see [LICENSE](LICENSE).
