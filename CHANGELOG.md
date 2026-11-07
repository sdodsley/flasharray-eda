# Changelog

## Unreleased

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
  nothing. Standard library only; `tests/unit/test_ao_sync.py` drives it
  against an in-process mock of the API.
- `tools/ao_lint.py` knows loop nodes: `iterate` and `complete` ports, the
  body's return edge (`to_port: iterate`) as the one allowed back edge
  (`loop_ports`), and the loop parameter keys.
- `purefa_syslog` counts the array's own service accounts as automation
  (`system_users`, default `pure_datainsights`; `purity` writes with auth
  source `system`), so the human-action rule stays quiet for the array's
  scheduled snapshots and tag housekeeping.

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
- Every action playbook ends with an `ansible.builtin.set_stats` task that
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
  and share the root `requirements.yml`. CI does not build the image.
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
