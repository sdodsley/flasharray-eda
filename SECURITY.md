# Security

This project talks to storage arrays with administrative credentials and
can change their configuration. Read this before running it anywhere that
matters.

- **Two credentials, two roles.** The source plugins only read; give them a
  token with a read-only array role. The remediation playbooks write; give
  them a dedicated service account, name it in `automation_users`, and keep
  its token in your automation platform's credential store. Credentials
  reach the plugins through environment variables (`api_token_env`,
  `fa_url_env`) and the playbooks through the inventory's
  `lookup('env', ...)`, never through a vars file. `run.sh` loads them from
  `~/.flasharray-eda.env` (template: `env.example`); keep that file mode
  600. `*.env` is gitignored.
- **Every write is gated.** Maintenance windows suppress remediation,
  destructive actions sit behind an approval, and per-site switches turn
  each automated action back into a notification. Start with the switches
  off.
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
- **The Alertmanager listener cannot authenticate.** `ansible.eda.alertmanager`
  has no token option. Bind it with `alertmanager_host` to an interface only
  Alertmanager reaches, and put it behind the Event-Driven Ansible
  controller's event streams or a reverse proxy that authenticates.
- **The syslog listener filters, it does not authenticate.** Bind it with
  `syslog_host` and list the arrays' addresses or prefixes in
  `syslog_allowed_sources` (empty accepts any sender). UDP source addresses
  can be forged, so syslog events are a fast hint: `syslog.yml` only
  notifies, and the REST plugin owns every action that changes an array.
- **TLS verification is off by default** in the plugins and the playbooks
  (`verify_ssl: false`, `disable_warnings: true`) because lab arrays carry
  self-signed certificates. Turn it on where the arrays have real ones.
- **Reporting.** Please report a vulnerability privately to the maintainer
  through GitHub's security advisory form for this repository rather than in
  a public issue.
