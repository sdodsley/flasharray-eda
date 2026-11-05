# Changelog

## 0.1.0 (2026-11-04)

First tagged state of the prototype.

- Two event sources: `purefa_events` (REST pull: alerts, audits, health,
  telemetry, replication, config) and `purefa_syslog` (push).
- Ten rulebooks: alerts, change control, config drift, health, replication
  (with the DR drill), capacity (with the approval gate), performance (QoS
  cap with hysteresis), anomaly, syslog, alertmanager.
- Playbooks act through `everpure.flasharray` 1.46.0 or later.
- Prometheus rules for the native OpenMetrics exporter, a decision
  environment definition, lab test scripts and replayable fixtures.
- Unit tests for the plugins and static checks over the rulebooks (schema,
  variables, playbooks, replay drift), run by `make check` and GitHub Actions.
- Lab-validated on two Purity//FA 6.10.6 arrays; records under `docs/`.

### Cleanup pass (2026-11-05)

- Launcher: `env.sh` (sourced by `run.sh` and the e2e scripts) loads
  `$HOME/.flasharray-eda.env` (template `env.example`), activates `./venv`
  or uses `ansible-rulebook` from the PATH, and passes `APPROVAL_TOKEN` and
  `GIT_WEBHOOK_SECRET` to ansible-rulebook with `--env-vars` when set. No
  lab paths remain in the tree.
- Listener hardening: the approvals webhook requires a bearer token, the
  git-push webhook a GitHub-style `X-Hub-Signature-256` HMAC; the syslog
  listener gains `syslog_host` and `syslog_allowed_sources`; the
  Alertmanager listener binds `alertmanager_host`.
- Plugins: `purefa_events` gains `request_timeout` and `growth_volumes`,
  renames the source arguments `drr_drop_pct` / `snapshot_growth_pct` to
  `drr_drop_threshold_pct` / `snapshot_growth_threshold_pct` (old names
  still read), reads protection group transfers newest first, pages audits
  past `audit_batch` and reports an overflow as a `purefa_poll_error`,
  always reports volumes carrying the `eda/qos_cap` tag, honours the
  per-array lock for telemetry and carries `DOCUMENTATION` and `EXAMPLES`
  blocks; `purefa_syslog` validates `arrays` as a mapping, gains
  `allowed_sources`, takes `array` on audit lines from the message's own
  "Array name" field, and emits one event shape for every line; both accept
  `automation_users` as a list or a comma-separated string.
- `runbook_dir` removed: `alert_runbook.yml` includes `runbooks/<code>.yml`
  relative to itself. Every alert-log record carries time, kind, severity
  and array; `identify_led.yml` accepts a `severity` extra var.
- `anomaly.yml` drops its throttle (ansible-rulebook rejects `once_within`
  on a multi-event condition; the 15 minute timeout bounds re-firing). Replay
  rulebooks are rule-for-rule copies of the real ones, throttles included,
  and the unit test enforces it.
- e2e scripts write only under `tests/out/`, take array and object names
  from the environment, and refuse the eradicate step on an array with
  pending-eradication objects unless `E2E_ERADICATE_ALL=1`.
- Decision environment pinned to `quay.io/ansible/ansible-rulebook:v1.3.2`;
  Containerfile and builder definition produce the same tree and share the
  root `requirements.yml`; `docs/PACKAGING.md` lists the steps to move the
  content into the collection.
- SPDX headers on every Python and shell file; `.gitignore` covers env
  files, real baselines and the ansible-builder context; the example
  admin-auth baseline uses hardened values.
