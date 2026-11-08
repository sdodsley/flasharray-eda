# Security

This project talks to storage arrays with administrative credentials and
can change their configuration. Read this before running it anywhere that
matters.

- **Two credentials, two roles.** The source plugins only read; the
  remediation playbooks write. The shipped examples hand one token per
  array to both (`ARRAY_A_TOKEN` in `vars/example.yml`,
  `inventory/example.yml` and `aap/vars/example.yml`), so that token needs
  a role that can write. To split them, put a read-only token in its own
  environment variable and name that variable in `api_token_env`; give the
  playbooks a dedicated service account, name it in `automation_users`, and
  name its variable in the inventory's `lookup('env', ...)` (on the
  platform, in the fleet credential). Credentials reach the plugins through
  environment variables (`api_token_env`, `fa_url_env`) and the playbooks
  through the inventory, never through a vars file. `run.sh` loads them
  from `~/.flasharray-eda.env` (template: `env.example`); keep that file
  mode 600. `*.env` is gitignored.
- **Not every write is gated the same way.** Maintenance windows suppress
  the actions whose events carry `maintenance_active` (alerts, audits,
  hardware and replication): the alert runbooks, the reconcile, the
  identify LED, the preferred-array move and replicate-now; the config
  drift reapply, the QoS cap, the anomaly response, the baseline apply and
  the DR drill run inside a window too. Three switches turn actions into
  notifications: `qos_auto_cap`, `activecluster_auto_prefer` and
  `replication_auto_replicate_now`; the NTP and DNS reapply, the reconcile,
  the identify LED, protect-now and the DR drill have none. The reclaim of
  pending-eradication objects sits behind an approval on every path. Start
  with the three switches off and `orchestrator_enabled` off, and read the
  rules you enable.
- **The approvals webhook needs a bearer token.** The capacity approval
  ruleset accepts a POST only with `Authorization: Bearer <APPROVAL_TOKEN>`.
  Generate a long random value and put it in the env file; `run.sh` passes
  it to ansible-rulebook with `--env-vars`, and the rulebook reads
  `{{ APPROVAL_TOKEN }}` from the environment, never from a vars file.
- **The git-push webhook needs an HMAC signature.** The change control
  ruleset verifies GitHub's `X-Hub-Signature-256: sha256=<hex>` over the
  exact request body with `GIT_WEBHOOK_SECRET` (same env file, same
  `--env-vars` route). Set the same secret on the GitHub webhook; an
  unsigned or wrongly signed push is dropped.
- **The orchestrator handoff uses a service-account client secret.** With
  `orchestrator_enabled: true`, `playbooks/orchestrator_post.yml` reads
  `AO_CLIENT_ID` and `AO_CLIENT_SECRET` from the environment (same env
  file), exchanges them for a 15 minute bearer token held in a `no_log`
  fact for one playbook run, and never writes the token or the secret
  anywhere; the alert-log record carries status and execution id only. TLS
  verification for the orchestrator calls is on by default
  (`orchestrator_validate_certs`), unlike the array calls below. Give the
  service account access to the EDA triggers it needs and nothing else
  ([docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md)). On the platform, launch
  permission on the `FlashArray - capacity reclaim` job template is
  permission to eradicate already-destroyed objects (the reclaim rebuilds
  its request from the `candidates` it is given when no request file
  exists, provided the launch also sets `rebuild_from_artifacts: true`);
  grant it to the workflow's and the orchestrator's identity only.
- **The Alertmanager listener cannot authenticate.** `ansible.eda.alertmanager`
  has no token option. Bind it with `alertmanager_host` to an interface only
  Alertmanager reaches, and put it behind the Event-Driven Ansible
  controller's event streams or a reverse proxy that authenticates.
- **The syslog listener filters, it does not authenticate.** Bind it with
  `syslog_host` and list the arrays' addresses or prefixes in
  `syslog_allowed_sources` (empty accepts any sender). UDP source addresses
  can be forged, so syslog events are a fast hint: `syslog.yml` only
  notifies, and the REST plugin owns every action that changes an array.
- **The Slack webhook URL is a secret.** Anyone holding it can post to the
  channel. It lives in the env file or the `Slack webhook` credential,
  never in a vars file or a definition, and the notify playbook posts
  with `no_log`. The bot token behind the platform's notification template
  needs `chat:write` only.
- **The approval bot decides on behalf of its identity.** `slack/approval_bot.py`
  approves or rejects with the orchestrator and controller identities it
  holds; restrict who may click with `SLACK_APPROVERS` (Slack user ids), or
  anyone in the channel can decide. Its tokens reach it through its
  environment or, preferably, through files and podman secrets (the
  `*_FILE` variables), never on the command line, and it logs no secret.
- **TLS verification is off by default** in the plugins and the playbooks
  (`verify_ssl: false`, `disable_warnings: true`) because arrays in a lab
  usually carry self-signed certificates. Turn it on where the arrays have
  real ones.
- **Reporting.** Please report a vulnerability privately to the maintainer
  through GitHub's security advisory form for this repository rather than in
  a public issue.
