# Orchestrator handoff

How a rulebook hands an event to Red Hat's automation orchestrator (the
AAP 2.7 add-on; upstream code: Syntara) instead of acting on it locally.
This is Phase 1a of the orchestrator extension: the bridge, its mock and
its tests. The workflow definition that consumes the event is
orchestrator/capacity_reclaim_approval.json (orchestrator/README.md).

Status (2026-11-05): tested against `tests/mock_orchestrator.py` from the
unit test and from ansible-rulebook 1.3.2 replaying one event through the
real capacity rules (`tests/replay_capacity.yml`). No automation
orchestrator instance has been reached from this project. The contract
below comes from the upstream Syntara documentation and from the 1.3.1 API
as third parties observed it; confirm it against a live instance before
relying on it, in particular the authentication of the EDA webhook endpoint
(one upstream page calls it public, another requires a service account
token; this bridge always sends one). Availability: Red Hat announced the automation orchestrator as a
technology preview in May 2026 and made it generally available as an
add-on to Ansible Automation Platform 2.7 on 2026-08-21, for
OpenShift-based deployments; as of 2026-10-15 Red Hat had published no
timeline for the containerized (non-OpenShift) AAP topology, and the one
configuration-as-code collection that targets its API (1.3.1) describes
that API as a technology preview that may change. Treat every endpoint
and parameter in this document as unverified until it has been exercised
against a live instance.

## What moves, what stays

Only the capacity-at-risk rule moves. Everything else stays in EDA:
polling, the gates (`maintenance_active`, `automation_users`, the per-site
switches), the throttles, notifications, the QoS cap and the replication
actions.

`orchestrator_enabled` (vars) is the switch:

- `false` (default): unchanged. The rule writes a request file and the
  approvals webhook ruleset answers it (`capacity_request.yml`,
  `capacity_reclaim.yml`).
- `true`: the rule runs `playbooks/orchestrator_post.yml`, which posts the
  whole event to the EDA trigger named by `orchestrator_paths.capacity`.
  No request file is written and the approvals webhook is idle for that
  rule. Approve or reject, and the reclaim that follows, happen in the
  orchestrator workflow (an `approval` node, then an `aap_job_template`
  node that runs the reclaim with the event's fields as `extra_vars`).

The switch is a literal in the rule's condition (`vars.orchestrator_enabled
== true` / `== false`), because rulebook conditions take no filters or
defaults; an existing `vars/local-<site>.yml` needs the key added.

The rule keeps its 24 hour per-array throttle. A handoff that fails (the
orchestrator is down, the token is refused, the service account is not
bound) is recorded as a warning and is not retried until the throttle
window has passed; nothing is written locally in that case. Watch the alert
log for `orchestrator_post` records with `severity: warning`.

## The contract

### 1. Token

```
POST {orchestrator_url}/api/v1/auth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<AO_CLIENT_ID>&client_secret=<AO_CLIENT_SECRET>
```

Answer `200 {"access_token": "...", "token_type": "bearer", "expires_in":
900}`; `401` for bad credentials. Tokens live 15 minutes by default, cannot
be refreshed and are never cached to disk: the playbook requests one per
event and holds it in a `no_log` fact.

### 2. Event

```
POST {orchestrator_url}/api/v1/webhooks/eda/{webhook_path}
Authorization: Bearer <access_token>
Content-Type: application/json

{"event": {...}}
```

| Status | Meaning | Record |
| --- | --- | --- |
| 202 `{"execution_id": "..."}` | a workflow execution started | `severity: info`, `execution_id` |
| 401 | missing, expired or invalid token | `warning`, `status: 401` |
| 403 | the service account is not bound to this trigger (`authorized_service_account_ids`) | `warning`, `status: 403` |
| 413 | body over the orchestrator's 1 MB payload limit | `warning`, `status: 413` (the playbook refuses bodies over 1000000 bytes before sending, see below) |
| -1 | no HTTP answer (connection refused, timeout, TLS failure) | `warning`, `status: -1` |

The orchestrator passes the body through unwrapped, so a workflow node
reads `${trigger.event.array}`, `${trigger.event.data.used_pct}` and so on
(the trigger's id is an alias of `trigger`).

### 3. Envelope

```json
{
  "event": {
    "schema": "flasharray-eda/1",
    "event_type": "purefa_capacity",
    "array": "array-a",
    "maintenance_active": false,
    "observed_at": "2026-11-05T21:40:49Z",
    "data": {
      "array": "array-a",
      "used_pct": 97.3,
      "used_bytes": 48650000000000,
      "capacity_bytes": 50000000000000,
      "days_to_full": 9,
      "days_to_full_known": true,
      "growth_bytes_per_day": 150000000000,
      "top_growers": [{"volume": "vol-db-01", "growth_bytes": 90000000000}],
      "pending_eradication_volumes": 2,
      "pending_eradication_snapshots": 5,
      "others": [{"array": "array-b", "used_pct": 41.0}]
    }
  }
}
```

| Field | Content |
| --- | --- |
| `schema` | `flasharray-eda/1`; bump when a field changes meaning |
| `event_type` | the event's `purefa_*` key as the plugin emits it |
| `array` | inventory name of the array, as every event carries it |
| `maintenance_active` | the event's own flag; `false` when the event type does not carry one (the capacity event does not) |
| `observed_at` | UTC, `%Y-%m-%dT%H:%M:%SZ`, when the playbook built the envelope |
| `data` | the event's fields, unchanged, without ansible-rulebook's `meta` |

The body must stay under the orchestrator's 1 MB limit. The playbook
measures `to_json` output and refuses anything over 1000000 bytes without
a request (`stage: size_check`, `status: null`). A capacity event is a few
hundred bytes; the limit matters only if a future route forwards large
lists.

### 4. The trigger on the workflow side

The published workflow carries an `eda_trigger` whose `webhook_path` is
the value in `orchestrator_paths` and whose `authorized_service_account_ids`
lists the service account behind `AO_CLIENT_ID`. Ids differ per
environment; the definition files use the placeholder `REPLACE_AT_SYNC`:

```json
{
  "id": "fa_capacity_event",
  "type": "eda_trigger",
  "parameters": {
    "webhook_path": "fa_capacity",
    "authorized_service_account_ids": ["REPLACE_AT_SYNC"]
  }
}
```

The shipped trigger also carries an `input_schema` that requires the
envelope keys (`schema`, `event_type`, `array`, `maintenance_active`,
`observed_at`, `data`) and the array-name pattern the playbooks assert on.

A webhook trigger and an EDA trigger may share a path; the EDA endpoint is
`/api/v1/webhooks/eda/<path>`, the plain webhook `/api/v1/webhooks/<path>`.

## Configuration

Vars (`vars/example.yml`, copied to `vars/local-<site>.yml`):

| Key | Default | Meaning |
| --- | --- | --- |
| `orchestrator_enabled` | `false` | the switch described above |
| `orchestrator_url` | `https://orchestrator.example.com` | base URL; `/api/v1/...` is appended |
| `orchestrator_paths` | `{capacity: fa_capacity}` | `webhook_path` per route; one key per rule that hands over |
| `orchestrator_validate_certs` | `true` | TLS verification for both calls (the orchestrator normally has a real certificate, unlike lab arrays) |

Playbook-only knobs, overridable as extra_vars: `orchestrator_timeout`
(seconds per call, 30) and `orchestrator_max_body_bytes` (1000000).

Environment (`env.example`): `AO_CLIENT_ID` and `AO_CLIENT_SECRET`, the
client credentials of an orchestrator service account. The playbook reads
them with `lookup('env')`; `run.sh` also forwards them to ansible-rulebook
with `--env-vars` when they are set, like `APPROVAL_TOKEN`. They never sit
in a vars file. Without them the playbook fails with a message naming
both variables and sends nothing.

On the orchestrator: create the service account in the workflow's project,
give it a client-credentials credential (the secret is shown once; client
ids start with `nx_sa_`; credentials expire after 180 days by default, with
a rotation grace period), and list the account in each EDA trigger's
`authorized_service_account_ids`. A service account bound to no trigger
gets a token and then a 403.

## The alert-log record

One line per handoff, whatever happened, kind `orchestrator_post`:

```json
{"time": "2026-11-05T21:40:50Z", "kind": "orchestrator_post", "severity": "info", "array": "array-a", "event_type": "purefa_capacity", "webhook_path": "fa_capacity", "stage": "post", "status": 202, "execution_id": "e639e52f-5e55-4564-a639-00d729606dd6", "reason": "", "body_bytes": 608}
{"time": "...", "kind": "orchestrator_post", "severity": "warning", "array": "array-a", "event_type": "purefa_capacity", "webhook_path": "fa_capacity", "stage": "token", "status": 401, "execution_id": "", "reason": "token request failed: invalid client credentials", "body_bytes": 608}
{"time": "...", "kind": "orchestrator_post", "severity": "warning", "array": "array-a", "event_type": "purefa_capacity", "webhook_path": "fa_other", "stage": "post", "status": 403, "execution_id": "", "reason": "service account is not authorized for this trigger", "body_bytes": 608}
{"time": "...", "kind": "orchestrator_post", "severity": "warning", "array": "array-a", "event_type": "purefa_capacity", "webhook_path": "fa_capacity", "stage": "token", "status": -1, "execution_id": "", "reason": "token request failed: Status code was -1 and not [200]: Request failed: <urlopen error [Errno 111] Connection refused>", "body_bytes": 608}
{"time": "...", "kind": "orchestrator_post", "severity": "warning", "array": "array-a", "event_type": "purefa_capacity", "webhook_path": "fa_capacity", "stage": "size_check", "status": null, "execution_id": "", "reason": "body of 1100401 bytes exceeds the 1000000 byte limit; not sent", "body_bytes": 1100401}
```

`stage` is where it stopped: `size_check`, `token` or `post`. `reason` is
the orchestrator's problem-details `detail` when there is one, else the
uri module's one-line message. The record never holds the token, the
client secret or the body.

## Running the mock

`tests/mock_orchestrator.py` is a standard-library HTTP server with the
two endpoints and `GET /healthz`. From the repository root:

```
python tests/mock_orchestrator.py --port 8800 --client-id nx_sa_demo \
    --client-secret demo --paths fa_capacity --record tests/out/orchestrator.jsonl
```

`--paths` is the comma-separated list of webhook paths the service account
is "bound" to (anything else answers 403); `--record` receives one JSON
line per accepted event; `--token-ttl` (default 900) ages tokens. By hand:

```
TOKEN=$(curl -s -X POST http://127.0.0.1:8800/api/v1/auth/token \
  -d grant_type=client_credentials -d client_id=nx_sa_demo -d client_secret=demo | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -s -X POST http://127.0.0.1:8800/api/v1/webhooks/eda/fa_capacity \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"event": {"schema": "flasharray-eda/1", "event_type": "purefa_capacity", "array": "array-a", "data": {}}}'
```

The playbook alone, against the mock:

```
AO_CLIENT_ID=nx_sa_demo AO_CLIENT_SECRET=demo ansible-playbook -i inventory/example.yml \
  -e orchestrator_url=http://127.0.0.1:8800 -e orchestrator_webhook_path=fa_capacity \
  -e '{"event": {"purefa_capacity": {"array": "array-a", "used_pct": 97.3}}}' \
  -e array=array-a -e alert_log=$PWD/tests/out/alerts.log playbooks/orchestrator_post.yml
```

`alert_log` must be absolute; a relative path is resolved against
`playbooks/`, not the directory the command runs from.

The whole chain, with ansible-rulebook: the header of
`tests/replay_capacity.yml` has the three commands (start the mock, derive
a vars file with `orchestrator_enabled: true` and the mock's URL, run the
replay). Expected: one `orchestrator_post` record with status 202 and the
envelope as one line in the record file.

`tests/unit/test_orchestrator_bridge.py` does the same without a shell:
it serves the mock in a thread on a free port, checks the mock's own
answers (200, 401, 403, 413, 202), then runs the playbook with
`ansible-playbook` for the accepted case, a wrong secret, an unbound path,
an unreachable orchestrator (a closed port; recorded as `status: -1`), an
oversize event and missing credentials, and checks that neither the
secret nor any issued token appears in the playbook output. The playbook
cases are skipped when `ansible-playbook` is not on the PATH. It runs in
about ten seconds.

## Security notes

- Both HTTP tasks run with `no_log`; the playbook logs only the status and
  the execution id. The credentials come from the environment and the
  token from memory; nothing is cached.
- TLS verification defaults to on for the orchestrator calls, unlike the
  array calls (`SECURITY.md`); set `orchestrator_validate_certs: false`
  only for a lab instance with a self-signed certificate.
- The envelope carries array, volume and protection group names and
  numbers, never credentials. The orchestrator keeps the payload in its
  audit log and Temporal history, so treat those stores as holding your
  array names.
- A 403 means the service account exists but is not bound to the trigger;
  a 401 on the token call means the credential is wrong, disabled or
  expired. Both are recorded, never retried in a loop.

## Open items

- Verify against a live automation orchestrator: the token endpoint's
  form fields, whether the EDA webhook requires the bearer token, the
  exact 403 semantics and the problem-details shape.
- A failed handoff leaves no local request. If a site wants a fallback
  (write the request file and notify when the post fails), that is a
  small addition to the rule or the playbook, not done here.
- As an EDA Controller activation `run_playbook` becomes
  `run_job_template` against `FlashArray - post orchestrator event`;
  `aap/seed.yml` creates that template and the `Orchestrator service
  account` credential type that injects `AO_CLIENT_ID` / `AO_CLIENT_SECRET`
  as environment variables (aap/README.md); neither has run against a
  platform.
- Other routes (replication, anomaly) follow the same pattern: add a key
  under `orchestrator_paths`, a rule guarded by `vars.orchestrator_enabled
  == true` that runs `orchestrator_post.yml`, and `== false` on the local
  rule. Only capacity is wired in this phase.

## Verified against a live orchestrator (2026-11-06)

The upstream orchestrator code at the tag Red Hat ships as the 2026.8
release (API 1.3.1) was run from its own compose file beside the
containerized platform of aap/README.md, on the same host, with its host
ports moved out of the platform's way. What ran:

1. Setup through `/api/v1`: a service account `flasharray-eda` in the
   default project with one client credential (`AO_CLIENT_ID` is its
   identifier, the secret shown once); an `Ansible Automation Platform`
   credential (platform admin, basic auth) and an
   `ansible_automation_platform` integration, validated `success`, through
   which `GET /api/v1/proxies/aap/job_templates` listed the ten FlashArray
   templates and the array credentials of aap/seed.yml;
   `orchestrator/capacity_reclaim_approval.json` with its two placeholders
   substituted (`POST /api/v1/workflows/validate`: valid, 0 findings),
   created as version 1 and published.
2. `playbooks/orchestrator_post.yml`, run by hand with `AO_CLIENT_ID` and
   `AO_CLIENT_SECRET` in the environment and a `purefa_capacity` event for a
   lab array (91.2 % used, 12 days to full, another array in `others`):
   token obtained, event posted, **202** with an execution id, one
   `orchestrator_post` record (`stage: post`, `status: 202`, 582 body
   bytes, `severity: info`).
3. The execution: the trigger completed with the body unwrapped, so every
   `${trigger.event...}` resolved; **FlashArray - capacity request**
   launched with the credential `credential_names` named
   (`FlashArray - <event array>`, interpolated, then resolved by name in
   the organization) and its extra_vars typed as sent (`candidate_targets`
   and `top_growers` lists, `used_pct` a float, the byte counts integers);
   its `set_stats` surfaced as `${capacity_request.artifacts.*}`
   (`request_id`, `at_risk_array`, `candidate_count` 4, `candidates`) and
   its job URL as `job_url`; the condition evaluated `true`; **FlashArray -
   notify** ran; the approval node created an approval for the `admins`
   group carrying the prompt from the definition and paused the execution
   (`approval_pending: true`, `status: paused`). The decision belongs to a
   human and is not part of this run.

Three facts about running that stack beside a containerized platform on
one host (its development compose, not the product):

- A rootless container cannot reach the host's own address (the user-mode
  network gives the container that address itself), and the orchestrator's
  URL validator refuses the link-local alias the container runtime offers
  for the host. The AAP integration therefore points at a small TCP relay
  container on the stack's network that forwards to that alias; the relay's
  name goes into `APP_INTEGRATION_URL_ALLOWED_HOSTS`, and the API and worker
  containers are recreated for the variable to take.
- The compose gives the worker services an `extra_hosts` entry that points
  the name `syntara` at the host (development mode, with the API outside
  containers). With the API's host port moved, the approval node's calls to
  the Approvals API fail ("Failed to connect to Approvals API after 3
  attempts") and the execution fails at the approval. Removing the entry
  lets the name resolve to the API container; `POST
  /api/v1/executions/{id}/retry` then re-runs the workflow from its start
  (a new execution, a new capacity request).
- The secrets generator can fail to produce a bootstrap admin password; a
  password written by hand must stay readable by the container user (the
  generator's own last step is `chmod -R +r`).
