# flasharray-eda

Event-Driven Ansible for standalone Everpure FlashArrays.

A FlashArray already exposes the three things an event-driven loop needs:
a cursorable alert event log, an attributable audit log, and its own
performance and capacity history. This project turns those into
`ansible-rulebook` events and acts on them through the `everpure.flasharray`
collection. No Pure1, no Fusion and no external monitoring stack are required,
although a Prometheus/Alertmanager path is supported for sites that already
run one.

Status: **planning / Phase 0**. See [docs/PLAN.md](docs/PLAN.md) for the
evaluation, the ideas and the phased plan. Nothing here has run against an
array yet; the Fusion MCP predecessor has, and its lessons are folded in.

## What it will do

| Domain | Trigger | Action |
| --- | --- | --- |
| Drift / change control | `purity.audit` line or `/audits` record by a non-automation user | Revert from the git baseline, report who and how |
| Alerts | `/alerts/events` by code and severity | Runbook per alert code; ticket carries the alert's own action text and KB link |
| Hardware | `/hardware`, `/drives`, `/controllers` status | Identify LED, time-boxed remote assist, host-side checks |
| Capacity | Days-to-full from `/arrays/space` history, top growers | Approval-gated reclaim, snapshot policy enforcement |
| Performance | `/arrays/performance` vs the array's own 7-day baseline, top-N volumes | Temporary QoS cap with hysteresis, Cinder retype |
| Replication | Array connections, pod replica link lag, stuck pgroup transfers | RPO escalation, replicate-now, preferred-array flip, scheduled DR drill |
| Anomaly | DRR drop + snapshot growth + write surge + retention edits within a window | Protection snapshot everywhere, longer eradication delay, page |

## Transports

1. **Pull**: one asyncio event-source plugin polling N arrays over REST with
   py-pure-client. Zero infrastructure.
2. **Push**: a syslog listener source for `purity.alert` / `purity.audit`.
   Sub-second, user already parsed.
3. **Zero custom code**: the native OpenMetrics exporter (Purity 6.7+) into
   Prometheus and Alertmanager, consumed by the stock `ansible.eda.alertmanager`
   source.

## Layout (target)

```
extensions/eda/plugins/event_source/   purefa_events.py, purefa_syslog.py
extensions/eda/plugins/event_filter/   normalise alert / audit payloads
extensions/eda/rulebooks/              one rulebook per domain
playbooks/                             remediation and notify playbooks
vars/                                  baselines, thresholds, intervals
inventory/                             one host per array, creds from env/vault
prometheus/                            rules file for the native exporter
tests/fixtures/                        recorded events for replay testing
tests/                                 lab induction playbooks
docs/                                  PLAN.md and design notes
```

## Related

- Predecessor: Fusion MCP -> EDA, the runnable follow-up to the
  "Fusion MCP + Ansible thought experiment" blog.
- Collection: https://github.com/Everpure-Ansible/FlashArray-Collection
- Rulebook docs: https://docs.ansible.com/projects/rulebook/en/latest/
