# Phase 4 record: push path, zero-code path, packaging

Date: 2026-10-20. Same lab and tooling as [PHASE0.md](PHASE0.md).

## What was built

- **Syslog listener** `extensions/eda/plugins/event_source/purefa_syslog.py`:
  UDP and TCP on one port, RFC 3164 and 5424 headers, `purity.alert`,
  `purity.audit` and `purity.test` recognised, audit `Key: 'value'` pairs
  parsed into fields (user, action, result, interface, session, location).
  `extensions/eda/rulebooks/syslog.yml` raises the fast notification for a
  human's action and for alerts; the REST plugin still owns the full record.
- **Prometheus rules** `prometheus/flasharray.rules.yml` for the native
  OpenMetrics exporter (Purity//FA 6.7+): open alerts, hardware status,
  capacity and days-to-full, data reduction drop, write latency vs last
  week, replica link lag, array connection, scrape failure. Each rule carries
  a `domain` label.
- **Alertmanager rulebook** `extensions/eda/rulebooks/alertmanager.yml` on
  the stock `ansible.eda.alertmanager` source: hardware alerts drive the
  identify-LED playbook, capacity alerts raise the approval request, the rest
  notify, resolved alerts are logged. No code from this project is needed on
  the array side of that path.
- **Decision environment** under `decision-environment/`: a Containerfile
  on `quay.io/ansible/ansible-rulebook`, an `ansible-builder` v3 definition,
  Python and collection requirements.

## Test results

| Step | What was done | Result |
| --- | --- | --- |
| syslog parser | representative audit, alert, test and garbage lines through `purefa_syslog.py parse` | audit: user, action, result and twelve fields parsed; alert and test recognised; garbage returns `kind: other` |
| syslog listener | `syslog.yml` running, four lines sent over UDP then TCP with `tests/syslog_send.py` | both transports delivered; the human's `purevol destroy` and the alert produced notifications, the test line was logged |
| Alertmanager | `alertmanager.yml` running, one Alertmanager webhook payload POSTed with a replica-link alert, a hardware alert and a resolved capacity alert | hardware alert lit the identify LED on CT1 of the real array (cleared afterwards); the others notified and logged |

## Not yet proven on hardware

- The arrays in this lab cannot reach the EDA host, so no live
  `purity.audit` line has been received. The parser follows the layout
  published by Pure's SIEM partners; the first real line may need a
  regex adjustment. `tests/syslog_send.py` is the replay.
- The Prometheus rules were written against the exporter's metric
  specification and not evaluated against a live Prometheus. Metric and
  label names should be checked against the exporter version in use.
- The decision environment was not built here (no container runtime in
  this WSL). The Containerfile and the builder definition are straight
  forward and the requirements match what the lab venv runs.

## Packaging

The layout already matches an Ansible collection's `extensions/eda`
convention: source plugins under `extensions/eda/plugins/event_source`,
rulebooks under `extensions/eda/rulebooks`. Moving into `everpure.flasharray`
means copying those two directories, renaming the sources in the rulebooks
to `everpure.flasharray.purefa_events` and `everpure.flasharray.purefa_syslog`,
and shipping the playbooks as the collection's `playbooks/`. The
certification track then needs the event source documentation blocks and
the tests Red Hat asks for.
