# Hardware notes

Verified facts about Purity, the REST API, the SDK, the collection modules,
ansible-rulebook and the containerized platform that shape the code, grouped
by topic. Each bullet states the fact and, where it matters, the consequence
for this project; [VERIFICATION.md](VERIFICATION.md) is the dated ledger of
how each was proven. Array facts are for Purity//FA 6.10.6 (REST 2.46)
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
  derives the opened / updated / reopened / closing / closed transitions
  itself.
- A resolved alert stays `closing` for 24 hours before the array marks it
  `closed` (its `closed` and `updated` fields are set when it resolves), so
  the closed transition, and anything a rulebook does with it, comes a day
  after the fault cleared.
- Stretching an empty pod to a second array raises alert 152 (info, "pod
  is now online"; its resolution reads "resync completed") on both arrays
  and alert 120 (critical, "offline on peer") on the source. Both open and
  resolve within seconds, inside one 30 s alerts poll, so the plugin first
  sees them already `closing`. This is the quick, safe way to make an
  array raise real alerts of two severities for a test.
- `POST /pods?with_default_protection=false` still receives the array's
  default protection group (retention lock `ratcheted`) at REST 2.46, and
  a pod cannot be destroyed while that group is in its default-protection
  list. Clear the list (`PATCH /container-default-protections` with an
  empty `default_protections`), destroy and eradicate the group (empty, so
  the ratchet is dormant), then destroy and eradicate the pod.
- History resolutions are fixed: 30 s, 5 min, 30 min, 2 h, 8 h, 1 day.
  Anything else is "Invalid resolution".
- Maintenance windows accept 3600 to 172800 seconds, not minutes.
- The self-test endpoints (`playbooks/self_check.yml`) answer with
  `TestResult` rows: at REST 2.54 `component_name`, `component_address`,
  `destination`, `test_type`, `success`, `result_details`, `description`,
  `enabled` (older releases say `result`).
  - `GET /alert-watchers/test` needs explicit watcher names and tests a
    named watcher even when it is disabled (`test_type`
    `send-test-email`); an array with no SMTP relay answers 400 "No SMTP
    relay host configured." for any watcher, so an enabled watcher on
    such an array has never received a mail.
  - `GET /syslog-servers/test` answers one row per server, sent by one
    controller, with "verification of successful transmission is done at
    the destination." The array does not confirm receipt; the listener's
    test-line rule is the confirmation, and the line arrives within a
    second.
  - `GET /support/test` answers two rows with no `destination`,
    `test_type` `phonehome` and `remote_assist`, "All tests passed." from
    CT0.
- A protection group's `targets` are not on the group object at REST 2.46;
  `/protection-groups/targets` is one extra call per poll.
- A protection group's `replication_schedule` and `snapshot_schedule` keep
  their `frequency` when switched off (`enabled: false`), so a reader that
  looks at the frequency alone takes a disabled schedule for a live one.
- A protection group's members come from three endpoints
  (`/protection-groups/volumes`, `/protection-groups/hosts`,
  `/protection-groups/host-groups`), one call each for every group at once;
  a volume connected through a host or a host group that is a member is
  protected without appearing in the volume list. `/connections` carries
  both `host` and `host_group` for a host-group connection.
- A host's `port_connectivity.status` is `healthy`, `unhealthy` (details
  such as "Uneven") or `critical` (details often empty); a host object with
  no volumes connected (`connection_count` 0) reads `critical` too, which is
  why the host signal considers hosts with connections only.
- A certificate's `valid_to` is epoch milliseconds; `status` is
  `self-signed` or `imported`, and the two VASA certificates sit beside the
  management one. `/kmip/test` answers per server; an array with no KMIP
  server configured returns an empty list, not an error.
- An alert watcher's `minimum_notification_severity` is absent from the
  model at REST 2.46 and present on newer arrays, so the config event carries
  it as null there; the watcher rules use the counts and the names only.
- `match_multiple_rules: true` on a ruleset has no effect on the platform:
  its engine fires the first matching rule per event only, so a signal
  whose poll can show several findings emits one event per finding.
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
- Pods take tags on Purity//FA 6.10.6 (REST 2.46) through `GET /pods/tags`,
  and the SDK carries `put_pods_tags_batch` and `delete_pods_tags` from
  REST 2.39 (an older array polls without the marker). The planned-work
  marker the switchover sets (`eda/planned_work`, an ISO time, beside
  `eda/planned_work_note`) lives there, and the plugin reads the `eda`
  namespace once per replication poll.
  - A demoted pod refuses tag changes (`PUT /pods/tags/batch` on the
    replica link's target answers "Invalid operation on demoted pod"), so
    the marker goes on the promoted source pod and the plugin applies it
    to the link's other side itself. Once the switchover has demoted that
    pod the tag cannot be cleared either, so the plugin stops honouring it
    after `planned_work_max_age` (15 minutes).
  - Pod tags travel with the replica link: the demoted side shows the
    marker without being written, and clearing the tags on the promoted
    pod clears the peer's copy within a minute.
  - During a planned switchover both arrays report the link `quiescing`
    and then `quiesced` for about a minute, the state the replication
    rulebook pages on when nothing marks the work as planned.
- Demoting a pod leaves `<pod>.undo-demote.<n>` behind: a destroyed pod
  holding the pod's state from before the demotion, found with
  `GET /pods?names=<pod>.undo-demote.*&destroyed=true` (a pattern that
  matches nothing is a 400 "No matching pods found", not an empty list).
  - Its eradication timer is the array's `disabled_delay` from
    `eradication_config` (288 hours on one verified array, 192 on another)
    even though the pod reports manual eradication enabled, where a
    `<pod>.relink` pod gets the 24-hour `enabled_delay`. An undo-demote
    pod left by demoting a promoted target pod can carry the 24-hour delay
    instead; what decides it is not established.
  - The array allows a second demotion while such pods exist and adds
    another; the `purefa_pod` module of everpure.flasharray 1.46 refuses it
    ("undo-demote pod not being eradicated"), which every repeated
    switchover inside that window meets. `dr_switchover.yml` and
    `dr_demote.yml` eradicate them first when `eradicate_undo_demote` is
    true (both orchestrator workflows set it). everpure.flasharray 1.47.0
    gives the module an `eradicate_undo` option that does the same inside
    the demote; once a release carries it the playbook's own step can give
    way to it.
- NTP faults are slow to alert on 6.10.6. NTP servers pointed at an
  unroutable address raise no alert for at least 26 minutes, and pausing
  and resuming a pod replica link raises none at all; alert 13 ("Time sync
  failure", "server not synchronized") comes hours later and stays open
  while a controller's ntpd is down. A test that needs a real alert needs
  a fault the array treats as one; the pod stretch above is the quick one.
- Setting the NTP servers to another address and back restarts ntpd and
  takes an open alert 13 to `closing` within a minute. `purefa_ntp` makes
  no change when the servers already match, so runbook 13 as written
  cannot give a stuck daemon that kick.

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
- The `purity.test` line carries the array's own `UTC Time`, so the
  receipt record the syslog rulebook writes shows the array's clock
  against the listener's; an array with a stuck ntpd stamps its test lines
  behind the time they arrive.
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
  one line per alert. The line's `UTC Time` is the array's clock, so a
  skewed array stamps it behind the arrival time, and the line for an
  alert that closes within milliseconds (pod offline on peer, critical)
  does not always arrive. The listener parses the layout into fields and
  grades the event by the `Severity` word.
- A PATCH of a syslog server's URI from `tcp://` to `udp://` leaves the
  controllers' TCP connections up; delete and recreate the server to make
  the array switch transport.
- The array queues syslog messages while a TCP target is down and replays
  them when the connection is back: the lines from an 80-minute listener
  outage arrive within two minutes of the listener's return, in order,
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
- Volume QoS and volume group QoS are exclusive. A member of a group with
  a limit is refused its own ("Cannot set rate limits on individual volumes
  inside volume groups with rate limit."), a group with a limited member
  cannot be given one, and a limited volume cannot move into a limited
  group; an explicit 100000000 counts as a limit, unlimited reads back as
  null. `/volume-groups/tags` exists at REST 2.54 (the SDK has get, put
  batch and delete for them), so a group cap keeps its state on the group
  the way a volume cap does on the volume.
- `purefa_volume` takes `iops_qos: 0` to clear a limit and rejects the
  array's own "no limit" value 100000000. Through 1.46 it sends that
  maximum for 0, so the volume reads `iops_limit: 100000000` (100M in the
  GUI) rather than the `null` a volume that never had a limit reads;
  1.47.0 sends the empty string through the SDK's `QosPatch` model
  (py-pure-client 1.86.0 or later), which removes the limit. On 6.10.6,
  `PATCH /volumes` with `qos.iops_limit` set to `""` removes the limit
  (reads null again), `null` is ignored, `0` is refused ("IOPS limit or
  floor must be between 100 and 100M"). `qos_cap.yml` treats 100000000 and
  null alike as "no previous limit" and removes a limit through the SDK
  with `Qos.construct(iops_limit="")` (construct, because the client's own
  validation refuses the empty string) until a release of the collection
  carries the module fix.
- A volume's `usec_per_read_op` is the array's service time, not the
  initiator's: 4k random reads that take 2.7 ms on a VM over one iSCSI
  path read 69 µs on the array, and 1 MiB reads that take 16 ms read 200 to
  330 µs. A load generator on a slow link cannot make a volume look hot
  to the telemetry signal; an IOPS cap on that volume is visible at once
  in the volume's own `reads_per_sec` (1000 to 500 to 1000 as the limit is
  set and cleared).
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
  its next event: three audit lines three seconds apart become three notify
  jobs about a minute apart on the platform. A rule that must keep up with
  a burst should launch something short.
- The stock `ansible.eda` sources (webhook, generic, tick, alertmanager)
  resolve only from `ANSIBLE_COLLECTIONS_PATH`; `env.sh` puts
  `~/.ansible/collections` on it, with `./collections` in front when that
  directory exists.

## Containerized Ansible Automation Platform

Facts about the 2.7 containerized topology the seed and the activations
are written around; [aap/README.md](../aap/README.md) installs and operates
the deployment.

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
- The EDA controller keeps a deleted activation's name for a while after
  the id has left the activations list: a create with the same name inside
  that window answers 400 "activation with this name already exists". A
  tool that deletes and recreates waits out the name, not just the id.
- While activations restart the gateway answers an occasional 5xx to a
  create the controller has in fact carried out, so a retry's "already
  exists" can be the activation it just made under a new id rather than
  the name window; a tool looks the name up after every failed create.
  The controller hands extra_var back re-serialised with sorted keys and
  without the credential-injected vars, so compare parsed vars, never the
  text.
- A delete can lose its answer the same way, and its retry is refused
  with 404 or 409; judge a delete by the id leaving the list. The
  activations list answers the same occasional 5xx; retry it like a 502.
- An activation created while several others are still starting can fail
  its readiness check ("Activation is unresponsive. Readiness check for
  ansible-rulebook timed out") a few times before it runs; the on-failure
  restart policy carries it, sometimes through several instances.
- The replicate-now and protect-now cooldowns compare a snapshot's
  array-side `created` with the controller's clock, so an array whose clock
  is off shifts them by the skew (an array with an open NTP alert can run
  36 minutes behind). Keep the arrays on NTP, which the config-drift loop
  and alert 13 watch.
- A decision environment created through the API defaults to the pull
  policy `always`; an activation on a locally loaded image then fails to
  start with "x509: certificate is valid for <platform host>, not
  localhost", the worker's pull of `localhost/...` meeting the registry's
  certificate. `pull_policy: missing`, which the seed sets, uses the
  loaded image.
- `ansible.eda.rulebook_activation` cannot modify an activation; the
  enabled flag is the one thing a seed can set on an existing one, so the
  seed enforces it on every run and a site keeps it true once the
  activations run. An activation keeps the rulebook and the extra vars it
  was created with, and a project is synced when it is created, not on
  later seed runs. What reaches the platform, and when:
  - a changed playbook reaches a job template after a project sync;
  - a changed rule or a new vars key reaches an activation after it is
    recreated;
  - a changed event source plugin reaches an activation only after the
    decision environment is rebuilt and loaded again. An activation
    recreated on the old image runs the new rulebook against the old
    events, and a condition that reads a field the old plugin never
    emitted is simply never true ([VERIFICATION.md](VERIFICATION.md)
    has the proof).
- The EDA server runs at most twelve activations at once
  (`MAX_RUNNING_ACTIVATIONS`, the containerized installer's
  `eda_max_running_activations`); the next one waits as `pending` with
  "Wait for a worker to be available", and the cap is shared by every
  project on the platform. An activation must connect its rulebook
  within `RULEBOOK_READINESS_TIMEOUT_SECONDS` (60); on a host whose load
  average is well above its CPU count the start fails with "Activation is
  unresponsive. Readiness check for ansible-rulebook timed out" and the
  restart policy tries again, each attempt holding a slot. While the host
  is that busy the gateway answers API calls with 502, 503 and also 403,
  so a tool that reads the platform retries all three.
- A rootless container's restart policy (`always`, `unless-stopped`) is
  applied after a reboot only by the user's `podman-restart.service`
  (`podman start --all --filter should-start-on-boot=true`); with the
  service disabled every such container stays exited after a reboot,
  which is how the approval bot, Prometheus, Alertmanager and the
  automation orchestrator's own containers were found. The platform's
  containers have systemd user units of their own and come back without
  it.
- The EDA API's activation list declares one name filter, `name=`, and it
  is a case-insensitive substring match (`django_filters.CharFilter` with
  `lookup_expr="icontains"` in eda-server's
  `src/aap_eda/api/filters/activation.py`), not an exact one;
  `name__startswith`, `name__istartswith` and `search` are undeclared and
  ignored, so they return every activation on the platform. A reader that
  asks for a name keeps only the rows named exactly so, and a loop that
  enables, disables or deletes "our" activations picks them out by name
  itself. On a platform shared with another project, a seed that enforces
  its enabled flag over the unfiltered list disables the other project's
  activations too. The list also takes about ten seconds whatever the
  page size (no database time: the view's own work, with 25 activations),
  at the gateway's limit, and a list the gateway times out ejects the API
  upstream for a while, so every reader gets 503s; a `name=` query and a
  single-object read answer in about a second, which is how the seed and
  the audit read activations.
- After a host reboot the EDA activation worker marks an instance that had
  already reached its restart ceiling as failed ("exited with code 143 ...
  has reached the maximum number of restarts, restart policy is not
  applicable") and does not restart it, and the restarts it does attempt
  can meet the websocket proxy before it is ready ("server rejected
  WebSocket connection: HTTP 502") and fail; a restart by id afterwards
  succeeds. Containers started at boot by the user's units also lose
  podman's view of their root mount (`podman inspect` shows no MergedDir,
  `podman exec --user <name>` fails with "no matching entries in passwd
  file", a healthcheck that runs as a named user fails) while the
  containers themselves run; a numeric user or no user works, and
  `podman mount <container>` restores the view, until the next time.
  containers/storage counts the mounted layers in
  `overlay-layers/mountpoints.json` under the run root; when that file
  shows none for a store, every podman command that exits runs the overlay
  driver's cleanup, which lazily unmounts the storage home and with it
  every container root mount beneath it in the rootless pause namespace
  (podman issue 30006, open, unfixed through podman 6.2.0). The containers
  run on; podman has lost their root. `skip_mount_home = "true"` under
  `[storage.options.overlay]` in a store's configuration leaves the cleanup
  nothing to unmount for that store's processes; in the user's
  `~/.config/containers/storage.conf` it covers the default store, which
  the user's units and shell use.
- After a boot the EDA activation worker reconciles the instances that did
  not survive it on its own schedule, seconds or minutes after the API first
  answers; until then every such activation still shows its pre-boot
  `running`, so a reader must keep watching rather than act on one pass. The
  API refuses connections for the first minutes while the platform's
  containers start, and the first activation started then can fail the 60 s
  readiness check and need a second attempt.
- The host keeps no per-user journal (`journalctl --user` finds no files);
  a user unit's output is in the system journal under
  `_SYSTEMD_USER_UNIT=<unit>`, and its result in
  `systemctl --user show <unit> -p ExecMainStatus -p ExecMainExitTimestamp`.
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
