# Phase 0 record: one source plugin, six signals

Date: 2026-10-20. Arrays: two Purity//FA 6.10.6 systems (REST 2.46), one
pair connected for sync replication, with one async pod replica link, one
stretched (ActiveCluster) pod and one replicating protection group between
them. Tooling: ansible-rulebook 1.3.2, ansible-core 2.17, py-pure-client
1.94.1, everpure.flasharray 1.44.0, ansible.eda 2.13.0.

## What was built

- `extensions/eda/plugins/event_source/purefa_events.py`: REST pull, six
  signals (alerts, audits, health, telemetry, replication, config), one
  asyncio task per signal, SDK calls in a thread pool, one client per array
  rebuilt after a failure, per-signal intervals.
- Six rulebooks under `extensions/eda/rulebooks/`, five playbooks, example
  vars and inventory, a launcher and the exit test.

## Exit test results

| Step | What was done | Result |
| --- | --- | --- |
| alerts steady state | 45 s run against both arrays | five alerts already open reported as `initial`, no false `opened` |
| config drift | `tests/induce_ntp_drift.yml` added a server to the NTP list | next poll emitted the drift, `remediate_ntp.yml` reapplied the baseline, `changed: true`, following poll clean |
| replication | `tests/replica_link_pause.yml` paused the pod replica link | paused rule fired on the source array (recovery point 5 s old) and on the target (inbound view); link resumed afterwards |
| health, maintenance window | `tests/maintenance_window.yml` opened a 3600 s window | health event reported `maintenance_active: true` with the window's expiry; window removed afterwards |
| health, hardware replay | three fixture events (critical controller, unhealthy bay inside a window, controller failover) | identify LED turned on for CT1 on the real array, the in-window bay was logged not actioned, the failover notified; LED cleared afterwards |
| audits | the audits signal with a 10 min lookback | every change made by the tests appeared with user, command, subcommand and arguments |
| test alert | `tests/send_test_alert.py` | array sent its test alert to the configured watcher |
| capacity, performance steady state | 45 s each | events flowed, nothing fired (lab at 2.6 % used, idle) |

## What the hardware taught us

- `/audits` rejects a filter on `time` ("Unknown attribute"); the cursor is
  applied locally to the newest `audit_batch` records sorted by time.
- `/alerts/events` on REST 2.46 answers "Must specify an alert ID(s)"; the
  alerts signal diffs `/alerts` with `state!='closed'` instead and derives
  opened / updated / closing / closed transitions itself.
- History resolutions are fixed: 30 s, 5 min, 30 min, 2 h, 8 h, 1 day.
  Anything else is "Invalid resolution".
- Maintenance windows accept 3600 to 172800 seconds, not minutes.
- `GET /alert-watchers/test` needs explicit watcher names.
- `everpure.flasharray.purefa_hardware` sends an empty PATCH body with
  py-pure-client 1.94.1 (the `Hardware` model drops `identify_enabled`), so
  the array answers 400 "No operation was specified". A dict body works; the
  identify-LED playbook calls the SDK directly until the module is fixed.
- Another automation identity was active on the lab array during the run
  (a QA harness using the same user). Its audit records showed up exactly as
  change control would see an operator's: the service-account split matters.

## ansible-rulebook constraints met

- Conditions cannot use Jinja filters (`| length`) or arithmetic
  (`lag_ms > rpo * 1000`). The plugin emits the booleans and counts the
  rules need: `drive_issue_count`, `rpo_breached`, `days_to_full_known`,
  `baseline_known`.
- `--vars` takes one file and `-e` is that file; values in it are not
  templated. Replay tests build a single-document vars file with `sed`.
- `throttle.once_within` must be a literal; `run_playbook` copies the
  playbook alone, so everything arrives as `extra_vars`; `localhost` must be
  in the inventory.
