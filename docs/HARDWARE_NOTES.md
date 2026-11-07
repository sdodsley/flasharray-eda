# Hardware notes

Facts about the array, the SDK, the collection, ansible-rulebook and the
state model that shape the code, grouped by topic; [README.md](../README.md)
carries the short form. Array facts are for Purity//FA 6.10.6 (REST 2.46)
unless stated.

## REST API

- `/audits` rejects a filter on `time` ("Unknown attribute"). The audits
  signal fetches records newest first in pages of `audit_batch` (default
  200, up to ten pages per poll) and applies the time cursor locally; more
  than that since the last poll is reported as a `purefa_poll_error`
  ("audit_batch overflow") and the oldest records are skipped.
- `/alerts/events` needs REST 2.50; on REST 2.46 it answers "Must specify an
  alert ID(s)". The alerts signal diffs `/alerts` with `state!='closed'`
  and derives the opened / updated / closing / closed transitions itself.
- History resolutions are fixed: 30 s, 5 min, 30 min, 2 h, 8 h, 1 day.
  Anything else is "Invalid resolution".
- Maintenance windows accept 3600 to 172800 seconds, not minutes.
- `GET /alert-watchers/test` needs explicit watcher names.
- A protection group's `targets` are not on the group object at REST 2.46;
  `/protection-groups/targets` is one extra call per poll.
- Transfers that completed before the poller started have no `started`; the
  DR drill picks the latest `completed`.
- Connections to a stretched pod's volumes list the peer array's hosts as
  `<array>:<host>`; only unqualified names are local and changeable.
- Volume and array performance samples are not taken at the same instant;
  a volume's IOPS share of the array is clamped to 100 %.
- Purity logs NTP changes as `purearray setattr --ntpserver`, not `purentp`.
- A pod's `link_source_count` counts the replica links that feed it (the pod
  is their target) and `link_target_count` the links it feeds. `dr_promote.yml`
  and `dr_demote.yml` therefore accept a pod only with a source count above
  zero and an inbound link. Demoting with quiesce discards the writes made
  while the pod was promoted; the source pod is never touched.

## OpenMetrics exporter (Purity//FA 6.7+)

The native exporter the Prometheus rules are written for, as served on
6.10.6:

- Every endpoint needs the query parameter `namespace=purefa` and the array
  API token as `Authorization: Bearer <token>`; `/metrics` serves everything,
  `/metrics/array`, `/metrics/volumes`, `/metrics/hosts`, `/metrics/pods` and
  `/metrics/directories` a subset. A full scrape is about 3,800 series per
  array and completes in under a second.
- Only `purefa_info` carries `array_name`; the scrape job sets `array_name`
  as a target label so that every series and every alert carries it.
- Status is a label with value 1
  (`purefa_hw_component_status{component_status="ok"} 1`; drive status sits
  on `purefa_drive_capacity_bytes`), so the rules select the label rather
  than compare the value.
- `purefa_array_space_utilization` is the used percentage; latency series
  carry a `dimension` label (`usec_per_write_op` and the like); replica links
  carry `local_pod`, `remote`, `remote_pod`, `status` and `direction` on
  `purefa_pod_replica_links_lag_max_msec`.
- There is no family for the array's open alerts and none for array
  connections; those come through the REST plugin or syslog.

## py-pure-client SDK

- SDK models raise `AttributeError` for fields the array did not return
  (`volume.qos.iops_limit` on an uncapped volume); read them through
  `to_dict()`.
- `host.preferred_arrays` comes back as references; serialise the names
  before comparing or templating them.

## everpure.flasharray modules

- `purefa_pgsnap` forbids `now` together with `remote`; `remote: true` on
  its own is the immediate replicated snapshot.
- `purefa_volume` takes `iops_qos: 0` to clear a limit and rejects the
  array's own "no limit" value 100000000.
- A protection group snapshot on a group with no members fails;
  `protect_now.yml` reports those groups as failures. Eradicating a snapshot
  in a SafeMode-protected group is refused by the array; the reclaim reports
  the refusal and never forces it.

## Identity and audit attribution

- Any other automation that shares the array account appears in the audit
  log exactly as an operator would; change control cannot tell them apart.
  Name the automation account in `automation_users` so its records are
  logged and not acted on. Running the automation as a dedicated service
  account is not optional. [SECURITY.md](../SECURITY.md) states the
  two-credential policy.

## ansible-rulebook 1.3.2

- Conditions cannot use Jinja filters (`| length`) or arithmetic
  (`lag_ms > rpo * 1000`); actions can. The plugin therefore emits the
  booleans and counts the rules need: `drive_issue_count`, `rpo_breached`,
  `days_to_full_known`, `baseline_known`.
- `--vars` takes one file and `-e` is that file; values in it are not
  templated. The e2e scripts therefore concatenate the vars file and a
  fixture into one document and substitute the fixture placeholders with
  `sed`.
- `throttle.once_within` must be a literal and is rejected on a multi-event
  (`all`) condition; `anomaly.yml` carries no throttle and its 15 minute
  `timeout` bounds re-firing. A multi-event `all` condition with
  `events.x <<` assignment, an `or` across two event types and a `timeout`
  parses and fires.
- `run_playbook` copies the playbook's directory into a temporary project,
  not the repository: files outside `playbooks/` are absent, everything
  arrives as `extra_vars`, and relative includes under `playbooks/` resolve.
- `localhost` must be in the inventory.
- The stock `ansible.eda` sources (webhook, generic, tick, alertmanager)
  resolve only from `ANSIBLE_COLLECTIONS_PATH`; `env.sh` puts
  `~/.ansible/collections` on it, with `./collections` in front when that
  directory exists.

## State

- State that must survive a rulebook restart lives on the array or in a
  file, never in a rulebook fact: the QoS cap is a pair of volume tags in
  namespace `eda` (`qos_cap`, `qos_prev_iops`, read back as `eda_capped`),
  the approval request is a file under `request_dir`.
