# Orchestrator workflow definitions

Workflow definitions for Red Hat's automation orchestrator (the Ansible
Automation Platform 2.7 add-on; upstream project Syntara), in the JSON the
orchestrator imports and exports (`schema_version` 2.0.0: `triggers`,
`nodes`, `edges`). They do not replace the rulebooks: `ansible-rulebook`
still decides what an array event means. What the orchestrator adds is the
part a rulebook cannot do well, a human approval that pauses the run, a
schedule with an overlap policy, and an audit record of who decided what.
Every node that touches an array is still one of this project's playbooks,
launched as an AAP job template.

Nothing in this directory has been imported into a live orchestrator yet;
the definitions follow the published model (see the assumptions at the end)
and `tools/ao_lint.py` checks them offline. Availability: Red Hat announced the automation orchestrator as a
technology preview in May 2026 and made it generally available as an
add-on to Ansible Automation Platform 2.7 on 2026-08-21, for
OpenShift-based deployments; as of 2026-10-15 Red Hat had published no
timeline for the containerized (non-OpenShift) AAP topology, and the one
configuration-as-code collection that targets its API (1.3.1) describes
that API as a technology preview that may change. Treat every endpoint
and parameter in this document as unverified until it has been exercised
against a live instance.

## The definitions

| File | Starts from | What it does |
| --- | --- | --- |
| `capacity_reclaim_approval.json` | `eda_trigger` on webhook path `fa_capacity` | A `purefa_capacity` event forwarded by the capacity rulebook runs **FlashArray - capacity request** (the request file and alert-log record of `playbooks/capacity_request.yml`). If the request lists reclaim candidates, **FlashArray - notify** announces it, an approval node waits up to 24 hours, and an approval runs **FlashArray - capacity reclaim** on exactly the candidates the request listed; a rejection or timeout runs a separate notify node. No candidates: a notify node says so. Nothing is freed without the approval. |
| `day2_expand_volume.json` | `manual_trigger` with an input form (array, volume, new size) | Notify, approval (24 hours), then **FlashArray - expand volume** with the three inputs and the requested array's credential, then notify with the AAP job URL. A rejection notifies and changes nothing. |
| `dr_drill_schedule.json` | `scheduled_trigger`, cron `0 2 * * *`, overlap policy `skip` | Runs **FlashArray - DR drill** (`playbooks/dr_drill.yml`); when its `failed_count` artifact is above zero a critical notify fires, otherwise an info summary. |

Approval nodes always have two distinct successors: `approved` and
`rejected` never share a node (the linter enforces it), because a shared
successor would run on either decision.

## The event envelope

The capacity definition expects what the EDA bridge
(`playbooks/orchestrator_post.yml`, run by the capacity rulebook when
`orchestrator_enabled` is true; the path is `orchestrator_paths.capacity`
in the vars file and the contract is in `docs/ORCHESTRATOR.md`) sends to
`POST {orchestrator_url}/api/v1/webhooks/eda/fa_capacity` with a service
account bearer token:

```json
{"event": {"schema": "flasharray-eda/1", "event_type": "purefa_capacity",
           "array": "array-a", "maintenance_active": false,
           "observed_at": "2026-11-05T02:00:00Z",
           "data": {"used_pct": 91.2, "used_bytes": 1, "capacity_bytes": 1,
                    "days_to_full": 12, "top_growers": [], "others": [],
                    "pending_eradication_volumes": 3,
                    "pending_eradication_snapshots": 40}}}
```

The orchestrator wraps the body as `trigger.payload`, so the nodes read
`${trigger.payload.event.array}` and `${trigger.payload.event.data.<field>}`;
`data` carries the `purefa_capacity` event fields unchanged. The trigger's
`input_schema` (JSON Schema draft-07) requires the envelope keys and the
array-name pattern the playbooks assert on.

## Job templates and what they must carry

`job_templates.txt` lists every template name the definitions use; `make
lint-ao` refuses a definition that names anything else. The nodes pass
only what comes from the event, the form or an earlier node; everything
site-specific is set on the job template itself (extra vars or a survey),
not in these files:

| Template | Playbook | Passed by the node | Set on the template |
| --- | --- | --- | --- |
| FlashArray - capacity request | `playbooks/capacity_request.yml` | `at_risk_array`, `used_pct`, `used_bytes`, `capacity_bytes`, `days_to_full`, `top_growers`, `pending_eradication_volumes`, `pending_eradication_snapshots`, `candidate_targets` | `request_dir`, `alert_log` |
| FlashArray - capacity reclaim | `playbooks/capacity_reclaim.yml` | `request_id`, `array`, `candidates`, `rebuild_from_artifacts` (`true`), `reclaim_action` (`eradicate_pending`), `approver` | `request_dir` (persistent, same path as the request template), `alert_log` |
| FlashArray - notify | `playbooks/notify.yml` | `severity`, `subject`, `detail`, `target_array` | `alert_log` |
| FlashArray - expand volume | `playbooks/expand_volume.yml` | `target_array`, `volume`, `new_size` | `alert_log` (optionally `reason`, `requested_by`) |
| FlashArray - DR drill | `playbooks/dr_drill.yml` | nothing | `dr_drills`, `alert_log` |

`request_dir` should be a path that outlives a job and is shared by the
request and reclaim templates (a host directory exposed to isolated jobs
and the same instance group, see aap/README.md, "Paths inside the
execution environment"); the execution environment's /tmp that
`aap/vars/example.yml` defaults to is discarded when the request job ends,
up to 24 hours before the reclaim job starts. The reclaim node therefore
also passes `array` and `candidates` (the request job's artifacts), and
`capacity_reclaim.yml` rebuilds the request from them when the file is not
there, so the approval still eradicates exactly what the request listed; a
shared `request_dir` keeps the request and its outcome in one file. The
rebuild happens only when the launch also carries `rebuild_from_artifacts:
true`, as the node does; a launch without it is refused when the request
file is absent. Because the rebuild lets a launch name its own candidates,
execute on **FlashArray - capacity reclaim** must be held only by the
controller-tier workflow and the orchestrator's AAP credential, never by
people: a direct launch of that template with execute rights is the right
to eradicate any object already pending eradication.

The templates need the inventory of this project (one host per array) and
the array credentials `aap/seed.yml` creates (`FlashArray - <array>`, one
per array, each injecting `fa_url`, `api_token` and `credential_array`). A
job holds one of them, so the two capacity nodes set `credentials` to
`FlashArray - ${trigger.payload.event.array}`, the event's own array, and
the expand-volume node to `FlashArray - ${trigger.array}`, the array the
form asked for; the playbooks refuse to run when `credential_array` is not
the array they were asked to act on. The DR-drill node uses the credential
its template carries (`aap/seed.yml` attaches the first drill's target
array's); pick another array's credential on the node the same way.

The condition nodes route on job artifacts, which the playbooks publish
with `set_stats` so the orchestrator sees them as
`${<node_id>.artifacts.*}`:

- **FlashArray - capacity request** publishes `request_id`,
  `at_risk_array`, `candidate_count` (volumes plus snapshots pending
  eradication) and `candidates`, the request file's own `{volumes,
  snapshots}` dict in full. The reclaim playbook reads the request file by
  `request_id` when it is there and rebuilds it from `array` and
  `candidates` when it is not (see above).
- **FlashArray - DR drill** publishes `failed_count` and `drills` (one
  record per drill).
- **FlashArray - expand volume** publishes `array`, `volume`, `old_size`
  and `new_size` (bytes) and `changed`.

`${<node_id>.job_url}` is the orchestrator's own output for an AAP node
(the job's URL in automation controller) and is what the notify nodes
link to.

## Placeholders

Per-environment identifiers never appear in this directory. The string
`REPLACE_AT_SYNC` marks every one of them and is substituted when the
definition is imported:

| Placeholder | Where | Replaced by |
| --- | --- | --- |
| `credential_id` | every `aap_job_template` (and `aap_workflow_job_template`) node | the orchestrator's AAP credential (the integration's token or user) |
| `approver_group_ids` | every `approval` node | the group ids allowed to decide; `approver_user_ids` works the same way |
| `authorized_service_account_ids` | the `eda_trigger` | the service account the EDA bridge authenticates as |

`organization_name` is `FlashArray`, the organization `aap/seed.yml`
creates (`aap_organization`); change both together if the templates live
elsewhere. Approval timeouts are 86400 seconds (24 hours), after which a
run takes the `rejected` port.

## Importing

The orchestrator stores a workflow as draft and published versions; webhook,
EDA and scheduled runs use the published version, so an import is two
calls: create or update the workflow from the JSON (`POST /api/v1/workflows`,
which the `kenmoini.automation_orchestrator_config` collection's `workflow`
module wraps), then publish the version
(`POST /api/v1/workflows/{id}/versions/{version}/publish`). Publishing a
scheduled trigger is what creates its schedule; unpublishing deletes it.
`GET /api/v1/workflows/{id}/versions/{version}/export` and the UI's export
button return the same JSON this directory holds, so a change made on the
canvas can be exported and committed here. Authenticate with a service
account token (`POST /api/v1/auth/token`, `grant_type=client_credentials`,
15 minute lifetime, not refreshable; never written to disk).

A sync from this directory **never deletes**: it creates the workflows
that are missing, updates by name the ones that exist, substitutes the
placeholders from per-environment variables that are not committed, and
leaves anything else in the orchestrator alone (reporting it, not
removing it). Removing a workflow is a deliberate action in the
orchestrator by a human, not a side effect of a file disappearing.

Before every import run the linter; it is what `make check` runs:

```
python tools/ao_lint.py orchestrator/*.json --allowed-templates orchestrator/job_templates.txt
```

It needs nothing but the standard library and prints one line per problem
as `file:node:rule:message`. The rules: `schema_version` is `2.0.0`; the
top-level keys are present; ids match `^[a-zA-Z_][a-zA-Z0-9_]*$` and are
unique across triggers and nodes; only the known trigger and node types;
every edge has `from`/`to` (plus `from_port`) naming existing ids and
never ending at a trigger; an approval node
lists approvers and has `approved` and `rejected` edges to different nodes;
a condition node has an expression and `true` and `false` edges; a switch
has a `default_port` and complete cases; a converge node has at least two
incoming edges and any other node at most one; no cycle; at most 200
nodes, 500 edges and 1.5 MB; every
`aap_job_template` or `aap_workflow_job_template` node has
`organization_name`, a `credential_id` and its template
(`job_template_name` or `job_template_id`; `workflow_job_template_name` or
`workflow_job_template_id`), a name being one from the allowed list when
that is given; every agentic node has `credential_id` and `prompt`; no
`http_request` to a bare IP address; every `${name...}` expression names a
node or one of the `trigger`, `workflow_context` and `loop` namespaces (a
trigger's id is not a namespace: its payload is `${trigger...}`), and a
node it names runs before the referencing node (not on a parallel branch
or later); every `${trigger...}` reads what the trigger exposes
(`trigger_ref`: `${trigger.payload...}` for a webhook or EDA trigger,
`scheduled_at` or `triggered_at` for a scheduled trigger, a property of the
`input_schema` for a manual trigger, anything when it declares none);
every node has an incoming edge (`unreachable`); `from_port` appears only
on edges leaving an approval, condition or switch node; and every
condition and switch-case expression, with each `${...}` replaced by a
name, parses into the safe evaluator's operators only (`==`, `!=`, `>`,
`<`, `>=`, `<=`, `and`, `or`, `not`, `in`, names and literals; no `&&`,
arithmetic or function calls).

## Assumptions to confirm on a live instance

These follow the published model and the Red Hat demo definitions, not a
run against the product:

- Manual-trigger inputs are read as `${trigger.<field>}` (the demos do
  that); webhook and EDA bodies as `${trigger.payload...}`.
- The approval node's second port is named `rejected` and is also taken
  on timeout.
- The scheduled trigger's overlap policy is the `overlap_policy` parameter
  and `0 2 * * *` is evaluated in the orchestrator's schedule time zone.
- AAP job artifacts (`set_stats`) surface as `${<node>.artifacts.<name>}`
  and the job URL as `${<node>.job_url}`.
- A full-span expression such as `"${trigger.payload.event.data.top_growers}"`
  keeps the value's type, so lists reach the playbooks as lists.
- Expressions are interpolated inside an `aap_job_template` node's
  `credentials` list, so `FlashArray - ${trigger.payload.event.array}`
  names the event's array's credential at run time (and `FlashArray -
  ${trigger.array}` the form's array's in the day-2 definition).
- How a loop node's body is wired: the published edge shape is `{from, to,
  from_port?}` with no back edge, so the linter rejects every cycle and
  treats `from_port` on an edge leaving a loop node (or any node other than
  approval, condition and switch) as a mistake; confirm the loop node's
  wiring and ports before using one.
