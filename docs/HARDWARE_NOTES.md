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
- `/alerts/events` is not an event log: it answers "Must specify an alert
  ID(s)" on REST 2.46 and on REST 2.59 (Purity//FA 6.12.4) alike. The
  alerts signal diffs `/alerts` with `state!='closed'` on every version and
  derives the opened / updated / closing / closed transitions itself.
- Stretching an empty pod to a second array raises alert 152 (info, "pod
  is now online") on both arrays and alert 120 (critical, "offline on
  peer") on the source, which opens and closes within milliseconds: a safe
  way to make a lab array raise real alerts of two severities.
- `POST /pods?with_default_protection=false` still receives the array's
  default protection group (retention lock `ratcheted`) at REST 2.46, and
  a pod cannot be destroyed while that group is in its default-protection
  list. Clear the list (`PATCH /container-default-protections` with an
  empty `default_protections`), destroy and eradicate the group (empty, so
  the ratchet is dormant), then destroy and eradicate the pod.
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
- Demoting a pod leaves `<pod>.undo-demote.<n>` behind: a destroyed pod
  holding the pod's state from before the demotion, found with
  `GET /pods?names=<pod>.undo-demote.*&destroyed=true` (a pattern that
  matches nothing is a 400 "No matching pods found", not an empty list).
  On the lab arrays its timer is the array's `disabled_delay` from
  `eradication_config` (288 hours on one, 192 on the other) even though
  the pod reports manual eradication enabled, where a `<pod>.relink` pod
  gets the 24-hour `enabled_delay`. The pod cannot be demoted again while
  such a pod exists (the `purefa_pod` module refuses before asking the
  array), which every repeated switchover inside that window meets;
  `dr_switchover.yml` eradicates them first when `eradicate_undo_demote`
  is true.
- Twenty-six minutes with the NTP servers pointed at an unroutable address
  raised no alert on 6.10.6 within the test window, and neither did pausing
  and resuming a pod replica link; array B raised alert 13 ("Time sync
  failure") hours later and keeps it open while a controller's ntpd is
  down. A test that needs a real alert now needs a fault the array treats
  as one; the pod stretch above is the quick one.

## Syslog

What a syslog target receives from Purity//FA 6.10.6 (`/syslog-servers`
with a `tcp://host:port` or `udp://host:port` URI; `/syslog-servers/test`
sends a `purity.test` line):

- The controllers' whole system log, not only the Purity messages: sshd,
  sudo, bash, kernel, dbus and cron lines arrive at a rate of hundreds a
  minute on an idle array. The listener keeps the `purity.*` programs and
  drops the rest (`syslog_programs`).
- Lines have no `<PRI>`, so no syslog severity or facility; the hostname is
  the controller's (`<array>-ct0`), and the program carries a pid
  (`purity.audit[211234]:`). The array name is in the message text.
- Every CLI, GUI and REST command is one `purity.audit` line in the layout
  `[user (local)]@array(array-id) command arguments. Message ID: N UTC
  Time: <ISO> Array Name: <array>`; REST calls appear as the CLI command
  they map to (`purevol create --size 1G ...`). Sessions are a second
  layout, `(login message ID: N) Array name: '...' ... User: '...' Action:
  'session request' | 'operation request' | 'session end' ...`, one or more
  per API call, which is the layout Pure's SIEM partners publish. The
  rulebook ignores the session records.
- The array's own services write audit lines too: `purity` with auth source
  `system` (scheduled protection group snapshots and their replication) and
  the local account `pure_datainsights` (volume tags in the
  `datainsights.purestorage.com` namespace, every few minutes). The listener
  counts both as automation (`system_users`, auth source `system`), so the
  human-action rule stays quiet for them.
- `purity.test` lines read `INFO [pure_middleware] This is a test message
  generated by Pure Storage FlashArray. UTC Time: <date> Array Name: <array>`.
- `purity.alert` lines read `<LEVEL> <subject> [<code>]<description>  A Pure
  Storage Support case has not been created for this alert. Please contact
  Pure Storage Support if you need further assistance.  Severity: <Word>
  UTC Time: <Mon DD HH:MM:SS> Array Time: <local> Array Name: <array>
  Domain: <domain> Knowledge Base Article: <url> Suggested Action: <text>`,
  one line per alert; the line for an open alert arrived 36 minutes after
  the alert's own UTC time, and the line for an alert that closed within
  milliseconds (pod offline on peer, critical) arrived in one run and not
  within the two minutes watched in another. The listener parses the
  layout into fields and grades the event by the `Severity` word.
- A PATCH of a syslog server's URI from `tcp://` to `udp://` leaves the
  controllers' TCP connections up; delete and recreate the server to make
  the array switch transport.
- The array queues syslog messages while a TCP target is down and replays
  them when the connection is back: alert lines from an 80-minute listener
  outage arrived within two minutes of the listener's return, in order,
  with their original `UTC Time` fields.
- The header timestamp is the array's local time without a year; the
  message's `UTC Time` is the one to record.

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
- `run_job_template` waits for the job to finish before the ruleset takes
  its next event. Three audit lines three seconds apart became three notify
  jobs about a minute apart on the platform; a rule that must keep up with
  a burst should launch something short.
- The stock `ansible.eda` sources (webhook, generic, tick, alertmanager)
  resolve only from `ANSIBLE_COLLECTIONS_PATH`; `env.sh` puts
  `~/.ansible/collections` on it, with `./collections` in front when that
  directory exists.

## Containerized Ansible Automation Platform

Facts about the 2.7 containerized topology the seed and the activations
are written around; [aap/README.md](../aap/README.md) has the longer form.

- A job's execution environment reaches a service on the platform host
  only as `host.containers.internal`; the host's own address and loopback
  point back at the container. The orchestrator credential carries that
  name when the orchestrator runs on the platform host.
- Activations run with host networking: a listener inside a rulebook
  activation (the syslog and Alertmanager sources) listens on the host's
  port, so the arrays and Alertmanager address the platform host itself.
- The controller's task container and the activation workers keep image
  stores of their own, separate from the host user's command line; an
  image built on the host is loaded into them or pulled from a registry.
- `ansible.eda.rulebook_activation` cannot modify an activation; the
  enabled flag is the one thing a seed can still set, so the seed enforces
  it on every run and a site keeps it true once the activations run. An
  activation keeps the rulebook and the extra vars it was created with,
  and a project is synced when it is created, not on later seed runs: a
  changed playbook reaches a job template after a project sync, a changed
  rule or a new vars key reaches an activation after it is recreated.
- A rootless container's non-root user cannot read a 600-mode file
  bind-mounted from the host (the host user maps to the container's root,
  not to the image's user); a podman secret is mounted readable under
  `/run/secrets`, which is how the approval bot gets its tokens.

## Ansible templating

- Inside a Jinja expression Ansible doubles backslashes before Jinja parses
  the string, so `'\n'` written in `{{ }}` is a backslash and an `n`, not a
  line break; a real one belongs to the YAML (a block scalar, or `"\n"` in
  a double-quoted scalar). Regex arguments are the exception: the doubling
  is what carries `'\r'` to the regex engine as a carriage return. A unit
  test refuses such literals outside `regex_*` filters.

## State

- State that must survive a rulebook restart lives on the array or in a
  file, never in a rulebook fact: the QoS cap is a pair of volume tags in
  namespace `eda` (`qos_cap`, `qos_prev_iops`, read back as `eda_capped`),
  the approval request is a file under `request_dir`.
