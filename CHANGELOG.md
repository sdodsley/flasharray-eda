# Changelog

## Unreleased

- `dr_demote.yml` eradicates the undo-demote pod an earlier demotion left
  when `eradicate_undo_demote` is true, as `dr_switchover.yml` does, and
  the DR failover test workflow's revert node passes it; without the flag
  the play refuses and names the pods. Live, a failover test's revert after
  a switchover of the same link failed on the module's refusal and the pod
  stayed promoted until the undo-demote pod was eradicated by hand and the
  demote job relaunched. The record and the artifacts carry
  `undo_demote_eradicated`.

## 0.3.1 (2026-11-09)

The QoS cap holds up under a real load: `qos_cap.yml` caps only when it
tightens, changes nothing when run twice, and a lift leaves the volume as
the cap found it, limit or none; the telemetry source counts a lift's
normal polls from the cap; the pod guards render the array's message for
a missing pod. Verified against the two Purity//FA 6.10.6 arrays and
Ansible Automation Platform 2.7: the cap under a fio load through its
Slack approval, the platform's job-failure notification, and both
supported-base images running on the platform;
[docs/VERIFICATION.md](docs/VERIFICATION.md) has the ledger.

- The pod guards of `dr_promote.yml`, `dr_demote.yml` and
  `dr_switchover.yml` report the array's message for a missing pod ("Pod
  does not exist.") instead of an undefined-variable error: the message's
  default branch is evaluated even when the read returned an error, so the
  fields it names now tolerate absence; a unit test renders every guard
  message for a missing pod.
- Verified (VERIFICATION.md): the platform's Slack notification on a job
  failure, from a switchover launched against a pod that does not exist;
  the QoS cap under a real fio load from the platform host over iSCSI,
  twice through the day-2 route with its approval from Slack, on a volume
  with its own 1000-IOPS limit (recorded, enforced at 500, restored) and on
  one without (lifted to no limit, not the maximum); the two supported-base
  ansible-builder images running on the platform, the execution environment
  under a notify job and a guarded switchover job and the decision
  environment under the alertmanager activation taking an alert through its
  rule to a job.
- A lifted cap leaves the volume without a limit again: `qos_cap.yml`
  removes the limit through the API with an empty string, because
  `purefa_volume` sends the 100000000 maximum for `iops_qos: 0`, which
  reads back as a limit (100M in the GUI); a real previous limit is still
  restored through the module (HARDWARE_NOTES.md). The collection's
  master now clears a limit properly (`QosPatch`, 1.47.0); the API path
  stays until a release carries it.
- The lift's "normal for `qos_lift_normal_polls` polls" counts from the cap:
  the telemetry signal resets a volume's normal-poll counter when it
  becomes capped, so a cap requested through the day-2 route on a volume
  whose latency was already normal lasts those polls instead of being
  lifted at the next one (seen live: lifted after 34 seconds, "normal for
  10 polls").
- `qos_cap.yml` is idempotent: a cap on a volume that already carries the
  `qos_cap` tag and a lift on a volume without it change nothing. Live, two
  lift jobs ran 19 seconds apart; the second found the tags gone and
  cleared the limit the first had just restored.
- `qos_cap.yml` never loosens a limit: when the volume's own IOPS limit is
  already at or below `cap_iops` the cap run writes the tags and leaves the
  limit alone, so a lift restores the same value; `applied_iops` reports
  the limit in force. A unit test renders the condition and the artifact.
- The undo-demote refusal is the `purefa_pod` module's, not the array's:
  the array allows a second demotion and adds another undo-demote pod.
  everpure.flasharray's master now carries an `eradicate_undo` option on
  the module (1.47.0) that eradicates them inside the demote; the
  switchover's own step stays until a release carries it.

## 0.3.0 (2026-11-09)

The syslog path is live on the platform from both arrays, with the audit
and alert layouts parsed into fields; notifications reach Slack and
approvals come back from it through a bot with identities of its own; a
planned ActiveDR failover and failback runbook joins the orchestrator
workflows, now nine behind nineteen job templates, and `tools/ao_sync.py`
imports the definitions into a live orchestrator. Exercised against the
two Purity//FA 6.10.6 arrays, Ansible Automation Platform 2.7 and a live
orchestrator; [docs/VERIFICATION.md](docs/VERIFICATION.md) has the
ledger.

- Every approval node names the group `approvers` beside `admins`, so the
  people who decide and the Slack bot's orchestrator user can sit in a
  group of their own; the orchestrator resolves approver names when it
  creates an approval and takes users and groups, not service accounts.
- A planned ActiveDR failover and failback runbook. `playbooks/dr_switchover.yml`
  demotes the link's source pod with quiesce, waits for the link to report
  the target's copy complete, promotes the target pod and records both
  pods' states and the link's new direction; the same playbook with the
  sides swapped fails back. A pod cannot be demoted again while the
  undo-demote pod of an earlier demotion exists, so with
  `eradicate_undo_demote` true the playbook eradicates those pods first
  and reports them in `undo_demote_eradicated`; without it the play
  refuses and names them. `orchestrator/dr_failover_failback.json` runs
  it behind two approvals and sets the variable, and `aap/seed.yml`
  creates the `FlashArray - DR switchover` job template.
- `tools/ao_sync.py` asks the orchestrator for `/api/v1/service_accounts`
  (the path it used did not exist; the first live run found it) and
  compares a file with its export without `credential_names` and without an
  empty `extra_vars`, neither of which the export carries. Verified live as
  the service account: create, update and unchanged. The dry run says a
  changed definition would publish a new version and no longer guesses
  its number.
- `purefa_syslog` parses what Purity//FA 6.10.6 sends: lines without a
  `<PRI>`, a pid on the program, the command layout of `purity.audit`
  (`[user (local)]@array(id) command. Message ID: N UTC Time: ... Array
  Name: x`, giving `command`, `subcommand`, `arguments`, `action`,
  `utc_time` and `message_kind: command`) beside the session layout, and
  the severity from the text when there is no priority. New `programs`
  option (vars `syslog_programs`): the array forwards its controllers'
  whole system log, so only `purity.alert`, `purity.audit` and
  `purity.test` are emitted by default and the rest is dropped and
  counted. `syslog.yml` skips session records by `message_kind`.
- `tools/ao_sync.py` imports the orchestrator definitions into an
  orchestrator the way orchestrator/README.md ("Importing") describes:
  creates the workflows the project lacks, updates the existing ones by
  name, fills the `REPLACE_AT_SYNC` placeholders from `--aap-credential`,
  `--aap-integration` and `--service-account`, validates first, publishes,
  reports workflows no file defines and never deletes; `--dry-run` writes
  nothing. A changed definition is published as the next version, the
  live contract (a version's PATCH changes metadata only). Standard
  library only; `tests/unit/test_ao_sync.py` drives it against an
  in-process mock of the API.
- `tools/ao_lint.py` knows loop nodes: `iterate` and `complete` ports, the
  body's return edge (`to_port: iterate`) as the one allowed back edge
  (`loop_ports`), and the loop parameter keys.
- A second orchestrator route: a protection group whose replication is
  overdue or stuck is handed over when `orchestrator_enabled` is true, and
  `orchestrator/replication_overdue_approval.json` gates the immediate
  replicated snapshot behind an approval (`orchestrator_paths.replication`).
- `orchestrator_fallback_local` (default false): a capacity handoff that
  produced no execution raises the local request anyway.
- Slack: `notify.yml` posts every notification to an incoming webhook when
  `SLACK_WEBHOOK_URL` is set (env file, or the new `Slack webhook`
  credential the seed attaches to the notify template); the seed also
  creates a Slack notification template for job failures and the
  controller-tier workflow's approval events when a bot token and channel
  are set.
- `slack/approval_bot.py`: a Slack Socket Mode bot that posts pending
  orchestrator and controller approvals as cards with Approve and Reject
  buttons and carries the decision back with the Slack user's name
  (`SLACK_APPROVERS` limits who may click). Unit-tested with fakes.
- `replicate_now.yml` publishes job artifacts (`pgroup`, `snapshot_suffix`,
  `changed`, ...), and a unit test checks that every `${node.artifacts.key}`
  a definition reads is published by that template's playbook.
- `purefa_syslog` counts the array's own service accounts as automation
  (`system_users`, default `pure_datainsights`; `purity` writes with auth
  source `system`), so the human-action rule stays quiet for the array's
  scheduled snapshots and tag housekeeping.
- The ansible-builder definitions build: `python_interpreter` under
  `dependencies` pins the base image's Python 3.12, and each collection
  directory names its own `dest` under `additional_build_files`. The
  decision environment from `decision-environment/execution-environment.yml`
  is built on a `vfs` image store and runs `alertmanager.yml` through a
  posted alert to a notify record; the two supported-base definitions build
  there as well (PACKAGING.md has the rootless-overlay caveat).
- `aap/decision-environment.yml` excludes `systemd-python` from the pip
  stage: `ansible.eda`'s requirements name it, it has no wheel, and the
  supported base carries no Python headers or compiler to build it.
- Links people can open: `aap/seed.yml` sets the platform's base URL for
  notification links (`TOWER_URL_BASE`, from `aap_url_base`, the gateway
  URL by default), and the bot's controller cards link to `AAP_UI_URL`
  when set.
- `purefa_syslog` parses the `purity.alert` layout Purity//FA 6.10.6 sends
  (level word, subject, `[code]`, description, support-case sentence,
  `Severity:`, UTC and array time, array name, domain, knowledge base link,
  suggested action) into fields, grades the event by the severity word and
  stops the UTC trailer at `Array Time:`; the syslog alert rule grades an
  info alert as info, names the code in the subject and passes
  `alert_code`. The bot keeps the channel id Slack returns (chat.update
  refuses the channel name) and a failed card update no longer stops the
  poll.
- The Slack text of `notify.yml` carries a real line break between the
  subject line and the detail (a `'\n'` literal inside a Jinja expression
  is two characters under Ansible's backslash doubling), names the array
  once, drops carriage returns and escapes `&`, `<` and `>`; a unit test
  refuses escape literals inside Jinja expressions outside `regex_*`
  filters. The admin-authentication drift notice names the baseline values
  beside the observed ones.
- Verified since 0.2.0 (VERIFICATION.md): the syslog path live on the
  platform from both arrays; the Alertmanager chain into the platform's
  activation, with the capacity rule under a lab override and the
  replica-link rule on a real pause; the DR drill schedule firing on its
  own; the capacity handoff from a running activation through approval to
  the reclaim; the overdue-replication route through its approval to the
  replicated snapshot, once approved from Slack through the bot; the
  notify template posting to Slack from the platform; an approval nobody
  decided expiring and failing its execution at the approval node, with
  `fallback_decision` making no difference; live `purity.alert` lines from
  both arrays (a scratch pod stretched between them: an info line from
  each and a critical one, parsed into fields and graded by the rebuilt
  decision environment) and the array's test line over UDP, with the
  arrays replaying queued lines after an 80-minute listener outage; the scrape-failure rule through Alertmanager to the
  platform's activation, firing and resolved; the platform's Slack
  notification on a controller-tier approval and the bot's card for it,
  clicked from the channel;
  the alerts signal against a Purity//FA 6.12.4 array; the planned
  failover and failback on the lab's ActiveDR link, both ways, with the
  link reversed in between, then again with both approvals given from
  Slack by the bot's user, the undo-demote pods of the first run
  eradicated on the way and a duplicate launch of the failback (the
  gateway answered 503 after the job was created) absorbed by the
  playbook changing nothing the second time.

## 0.2.0 (2026-11-07)

The project runs on Ansible Automation Platform 2.7 and drives the
automation orchestrator: ten rulebook activations, eighteen job templates,
the controller-tier approval workflow and seven orchestrator workflows,
exercised against the two Purity//FA 6.10.6 arrays, a 2.7 installation and
a live orchestrator; the Prometheus path is exercised against the arrays'
native exporters. [docs/VERIFICATION.md](docs/VERIFICATION.md) has the
ledger.

### Collection and decision environment

- The repository is the collection `sdodsley.flasharray_eda` (`galaxy.yml`,
  `meta/runtime.yml`, `make collection`); the rulebooks name their sources
  by FQCN (`sdodsley.flasharray_eda.purefa_events`,
  `sdodsley.flasharray_eda.purefa_syslog`) and CI builds and installs the
  collection before the unit tests.
- The decision environment builds and installs the collection at image
  build time, so activations resolve the sources without `--source-dir`;
  `ANSIBLE_COLLECTIONS_PATH` in the image also names the base image's
  `ansible.eda` directory ([docs/PACKAGING.md](docs/PACKAGING.md)).

### Ansible Automation Platform

- `FlashArray fleet` credential type: one credential carries every array as
  a JSON list, injected as `purefa_fleet_json` (controller) and
  `purefa_arrays` (EDA). `playbooks/tasks/resolve_array.yml` picks the
  target array's URL and token in every array-facing playbook, and both
  plugins accept `arrays` as a JSON or YAML string.
- Platform rulebooks under `extensions/eda/rulebooks/platform/`, generated
  by `tools/platform_rulebooks.py` and checked by a unit test: every
  `run_playbook` becomes `run_job_template` in `aap_organization`; the
  webhook rulesets are left out. The config sources fall back to the fleet
  list when `config_arrays` is unset.
- `aap/seed.yml`: seven more job templates (eighteen, one per playbook;
  `FlashArray - post orchestrator event` is now `FlashArray - orchestrator
  post`), the `Platform user - flasharray-eda` EDA credential (the built-in
  Red Hat Ansible Automation Platform type) for the `run_job_template`
  actions, one activation per platform rulebook with the fleet credential
  and the site vars laid over the example file, enabled ten seconds apart
  when `aap_eda_activations_enabled` is true; `aap_eda_de_pull_policy`.
  [aap/README.md](aap/README.md) covers the image stores of a containerized
  platform.

### Orchestrator

- Four more definitions: `day2_qos_cap.json`, `day2_protect_now.json`,
  `day2_identify_led.json` and `dr_failover_test.json`, an ActiveDR
  failover test behind two approvals (`playbooks/dr_promote.yml`,
  `playbooks/dr_demote.yml`).
- All seven definitions follow the contract of the 2026.8 release:
  unwrapped trigger bodies (`${trigger.event...}`), `credential_names`,
  `approver_groups`, `decision_window`, `schedule_type` with `cron` and
  `missed_schedule_policy`. `tools/ao_lint.py` checks the parameter keys
  of every trigger and node type (`parameter_keys`).

### Monitoring

- `prometheus/flasharray.rules.yml` follows the names and labels of the
  array's native OpenMetrics exporter on Purity//FA 6.10.6
  (`namespace=purefa`, status as a label, `array_name` as a target label);
  `prometheus/prometheus.example.yml` and
  `prometheus/alertmanager.example.yml` are the scrape and routing
  configurations.

## 0.1.0 (2026-11-06)

First release of flasharray-eda: Event-Driven Ansible for standalone
FlashArrays, a prototype. Validated against two Purity//FA 6.10.6 arrays
(REST 2.46); the syslog listener and the Prometheus rules are tested with
replayed input. [docs/VERIFICATION.md](docs/VERIFICATION.md) lists what was
exercised on hardware, [docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md) the
array, SDK and collection facts, and [docs/DESIGN.md](docs/DESIGN.md) the
design.

### Event sources

- `purefa_events` polls the REST API for alerts, audits, health, telemetry,
  replication and config signals, reading each array's URL and token from
  the environment variables named by `fa_url_env` and `api_token_env`.
  Options include `request_timeout`, `growth_volumes`,
  `drr_drop_threshold_pct` and `snapshot_growth_threshold_pct`. It reads
  protection group transfers newest first, pages audits past `audit_batch`
  and reports an overflow as a `purefa_poll_error`, always reports volumes
  carrying the `eda/qos_cap` tag, honours the per-array lock for telemetry,
  and carries `DOCUMENTATION` and `EXAMPLES` blocks.
- `purefa_syslog` receives the arrays' syslog stream. It validates `arrays`
  as a mapping, filters senders with `allowed_sources`, takes `array` on
  audit lines from the message's own "Array name" field and emits one event
  shape for every line.
- Both plugins accept `automation_users` as a list or a comma-separated
  string.

### Rulebooks

- Ten rulebooks: alerts, change control, config drift, health, replication
  (with the DR drill), capacity (with the approval gate), performance (QoS
  cap with hysteresis), anomaly, syslog and alertmanager.
- `anomaly.yml` is one multi-event condition bounded by a 15 minute timeout
  and carries no throttle (ansible-rulebook rejects `once_within` on a
  multi-event condition).
- The replay rulebooks under `tests/` are rule-for-rule copies of the real
  ones, throttles included; a unit test enforces it.
- Listener hardening: the approvals webhook requires a bearer token
  (`APPROVAL_TOKEN`), the git-push webhook a GitHub-style
  `X-Hub-Signature-256` HMAC (`GIT_WEBHOOK_SECRET`), the syslog listener
  binds `syslog_host` and filters with `syslog_allowed_sources`, and the
  Alertmanager listener binds `alertmanager_host`.

### Playbooks

- Act on the arrays through `everpure.flasharray` 1.46.0 or later.
- `alert_runbook.yml` includes `runbooks/<code>.yml` relative to itself.
  Every alert-log record carries time, kind, severity and array;
  `identify_led.yml` accepts a `severity` extra var.
- `expand_volume.yml` grows a volume (day-2) behind size and name guards.
- Every playbook a workflow or orchestrator node may follow ends with an
  `ansible.builtin.set_stats` task (`remediate_ntp.yml`,
  `remediate_dns.yml` and `preferred_array.yml` record their work in the
  alert log only) that
  publishes job artifacts for workflow and orchestrator nodes (`request_id`
  / `candidate_count` / `candidates`, `failed_count` / `drills`, `old_size`
  / `new_size`, ...; the keys are listed in
  [orchestrator/README.md](orchestrator/README.md)).
- `capacity_request.yml` publishes `candidates` as the request file's
  `{volumes, snapshots}` dict in full. `capacity_reclaim.yml` rebuilds the
  request from the `array` (or `at_risk_array`) and `candidates` extra vars
  when the request file is absent and the launch sets
  `rebuild_from_artifacts: true`, so an approved request completes as a
  later job in a fresh execution environment.

### Orchestrator handoff

- A per-site switch `orchestrator_enabled` (default false) routes the
  capacity-at-risk rule to `playbooks/orchestrator_post.yml`, which obtains
  a client-credentials token from Red Hat's automation orchestrator and
  posts the event to the workflow's EDA trigger as a `flasharray-eda/1`
  envelope ([docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md)). A 202 is
  recorded with its execution id; 401, 403, 413, a connection failure or a
  body over 1000000 bytes is recorded as a warning and never fails the
  rule. With the switch off, the request file and the approvals webhook
  handle the approval locally.
- Credentials are the `AO_CLIENT_ID` / `AO_CLIENT_SECRET` environment
  variables (`env.example`; `run.sh` forwards them with `--env-vars`).
  Vars: `orchestrator_url`, `orchestrator_paths`,
  `orchestrator_validate_certs`.
- `orchestrator/` holds three workflow definitions (capacity reclaim with
  approval, day-2 expand volume, nightly DR drill) with `REPLACE_AT_SYNC`
  placeholders for per-environment ids, and `job_templates.txt`
  ([orchestrator/README.md](orchestrator/README.md)). `tools/ao_lint.py`
  lints them offline; `make lint-ao` runs it in `make check` and CI.

### Ansible Automation Platform

- `aap/seed.yml` seeds AAP 2.7: organization, credential types,
  credentials from the environment, inventory, project, execution
  environment, eleven job templates, the controller-tier capacity workflow
  and the EDA objects with activations disabled
  ([aap/README.md](aap/README.md)).
- The `FlashArray API` credential type carries the array's inventory name
  (`array_name`) and injects it as `credential_array`; every array-facing
  playbook refuses to run when that is not the array it was asked to act
  on. The orchestrator's capacity nodes pick the event's array's
  credential, and the DR drill template takes the credential of its first
  drill's target array.

### Launcher and configuration

- `run.sh` launches one rulebook. `env.sh` (sourced by `run.sh` and the
  end-to-end scripts) loads `$HOME/.flasharray-eda.env` (template
  `env.example`), activates `./venv` or uses `ansible-rulebook` from the
  PATH, and passes `APPROVAL_TOKEN`, `GIT_WEBHOOK_SECRET`, `AO_CLIENT_ID`
  and `AO_CLIENT_SECRET` to ansible-rulebook with `--env-vars` when set.
- `vars/example.yml` documents every variable the rulebooks reference; the
  example admin-authentication baseline uses hardened values.
- `.gitignore` covers env files, local vars and inventory files, real
  baselines and the ansible-builder context.

### Monitoring, packaging and decision environment

- Prometheus rules for the array's native OpenMetrics exporter
  (`prometheus/flasharray.rules.yml`), consumed by the alertmanager rulebook
  through `ansible.eda.alertmanager`.
- A decision environment on `quay.io/ansible/ansible-rulebook:v1.3.2`: the
  Containerfile and the ansible-builder definition produce the same tree
  and both build the collection from the checkout. CI does not build the image.
- [docs/PACKAGING.md](docs/PACKAGING.md) lists the steps to move the
  content into the `everpure.flasharray` collection.

### Tests

- Unit tests for the plugins (against a fake SDK client) and static checks
  over the rulebooks (schema, variables, playbooks, replay drift), run by
  `make check` and GitHub Actions.
- `tests/unit/test_ao_lint.py`: every linter rule has a fixture, and the
  expand-volume form and playbook agree on sizes.
  `tests/unit/test_playbook_artifacts.py`: artifacts, credential guard,
  seed.
- `tests/mock_orchestrator.py` (standard library) mocks the token and EDA
  webhook endpoints; `tests/unit/test_orchestrator_bridge.py` runs the
  handoff playbook against it (skipped without `ansible-playbook`);
  `tests/replay_capacity.yml` replays one capacity event through the real
  rules for an end-to-end run with ansible-rulebook.
- The end-to-end scripts (`tests/e2e_sources.sh`,
  `tests/e2e_change_control.sh`, `tests/e2e_replication.sh`,
  `tests/e2e_approvals.sh`) run against real arrays, write only under
  `tests/out/`, take array and object names from the environment, and
  refuse the eradicate step on an array with pending-eradication objects
  unless `E2E_ERADICATE_ALL=1`.

### Repository

- Apache-2.0; SPDX headers on every Python and shell file.
