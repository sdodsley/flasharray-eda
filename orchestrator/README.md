# Orchestrator workflow definitions

Workflow definitions for Red Hat's automation orchestrator (the Ansible
Automation Platform 2.7 add-on; upstream project Syntara), in the JSON the
orchestrator imports and exports (`schema_version` 2.0.0: `triggers`,
`nodes`, `edges`). They do not replace the rulebooks: `ansible-rulebook`
still decides what an array event means. What the orchestrator adds is the
part a rulebook cannot do well, a human approval that pauses the run, a
schedule with a missed-run policy, and an audit record of who decided what.
Every node that touches an array is still one of this project's playbooks,
launched as an AAP job template.

The capacity definition is verified on the upstream orchestrator code at
the tag Red Hat ships as the 2026.8 release (API 1.3.1): it validates,
imports, publishes and runs from the EDA trigger through the approval to
the reclaim ("Checked against a live instance" below). Every definition
follows the node schemas of that release and `tools/ao_lint.py` checks them
offline. The product add-on is generally available for
OpenShift-based Ansible Automation Platform 2.7 only; no timeline is
published for the containerized topology, and its API (1.3.1) is described
as a technology preview that may change (docs/ORCHESTRATOR.md).

## The definitions

| File | Starts from | What it does |
| --- | --- | --- |
| `capacity_reclaim_approval.json` | `eda_trigger` on webhook path `fa_capacity` | A `purefa_capacity` event forwarded by the capacity rulebook runs **FlashArray - capacity request** (the request file and alert-log record of `playbooks/capacity_request.yml`). If the request lists reclaim candidates, **FlashArray - notify** announces it, an approval node waits up to 24 hours, and an approval runs **FlashArray - capacity reclaim** on exactly the candidates the request listed; a rejection runs a separate notify node. No candidates: a notify node says so. Nothing is freed without the approval. |
| `day2_expand_volume.json` | `manual_trigger` with an input form (array, volume, new size) | Notify, approval (24 hours), then **FlashArray - expand volume** with the three inputs and the requested array's credential, then notify with the AAP job URL. A rejection notifies and changes nothing. |
| `day2_qos_cap.json` | `manual_trigger` with an input form (array, volume, IOPS limit) | Notify, approval (24 hours), then **FlashArray - QoS cap** in `cap` mode with the requested array's credential, then a warning notify with the applied and previous limits and the AAP job URL. The cap carries the `eda/qos_cap` tag, so the performance rulebook lifts it once the volume's latency is normal again. A rejection notifies and changes nothing. |
| `day2_protect_now.json` | `manual_trigger` with an input form (array, seconds to add to the eradication delay) | Approval (24 hours), then **FlashArray - protect now** with the requested array's credential: an immediate snapshot of every local protection group and a longer enabled eradication delay (0 seconds snapshots only), then a warning notify with the snapshot suffix, the groups snapshotted or failed and the delay. A rejection notifies and changes nothing. |
| `day2_identify_led.json` | `manual_trigger` with an input form (array, component, LED on or off) | No approval: **FlashArray - identify LED** with the requested array's credential sets the component's identify LED, then an info notify reports the state, whether it changed and any error (a component without an identify LED), with the AAP job URL. |
| `dr_failover_test.json` | `manual_trigger` with an input form (target_array, pod) | A DR failover test on an ActiveDR replica link: notify, approval "promote" (24 hours), **FlashArray - DR promote** promotes the replica-link target pod on `target_array`, a notify with the promotion artifacts, then approval "revert" (its prompt says the writes made during the test are discarded), **FlashArray - DR demote** demotes the pod again and a notify reports the revert. Each rejection has its own notify; a rejected revert leaves the pod promoted and says so. |
| `dr_drill_schedule.json` | `scheduled_trigger`, cron `0 2 * * *` (UTC), missed runs skipped | Every night **FlashArray - DR drill** copies a volume out of the latest completed replicated snapshot on each drill target, verifies and destroys it; a condition on `failed_count` sends a critical **FlashArray - notify** when any drill failed and a one-line summary otherwise. Publishing the definition creates the schedule. |

Approval nodes always have two distinct successors: `approved` and
`rejected` never share a node (the linter enforces it), because a shared
successor would run on either decision.

The day-2 definitions have one rule in common, which
`tests/unit/test_ao_lint.py` checks: the form declares and requires exactly
the inputs the job node reads, every input is passed whole (`${trigger.x}`
on its own keeps a boolean or integer's type), the node carries
`credential_names` for the requested array, and nothing site-specific
(`alert_log`) is in the node.

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

The orchestrator passes the body through unwrapped (the trigger's output is
the request body itself, and the trigger's id is an alias of `trigger`), so
the nodes read `${trigger.event.array}` and `${trigger.event.data.<field>}`;
`data` carries the `purefa_capacity` event fields unchanged. The trigger's
`input_schema` (JSON Schema draft-07) requires the envelope keys and the
array-name pattern the playbooks assert on.

## Job templates and what they must carry

`job_templates.txt` lists every template name the definitions and the
platform rulebooks (`extensions/eda/rulebooks/platform/`, generated by
`tools/platform_rulebooks.py`) launch; `make lint-ao` refuses a definition
that names anything else, and the unit tests check that every listed name
is used by one or the other. The nodes pass only what comes from the event,
the form or an earlier node; everything site-specific is set on the job
template itself (extra vars or a survey), not in these files:

| Template | Playbook | Passed by the node | Set on the template |
| --- | --- | --- | --- |
| FlashArray - capacity request | `playbooks/capacity_request.yml` | `at_risk_array`, `used_pct`, `used_bytes`, `capacity_bytes`, `days_to_full`, `top_growers`, `pending_eradication_volumes`, `pending_eradication_snapshots`, `candidate_targets` | `request_dir`, `alert_log` |
| FlashArray - capacity reclaim | `playbooks/capacity_reclaim.yml` | `request_id`, `array`, `candidates`, `rebuild_from_artifacts` (`true`), `reclaim_action` (`eradicate_pending`), `approver` | `request_dir` (persistent, same path as the request template), `alert_log` |
| FlashArray - notify | `playbooks/notify.yml` | `severity`, `subject`, `detail`, `target_array` | `alert_log` |
| FlashArray - expand volume | `playbooks/expand_volume.yml` | `target_array`, `volume`, `new_size` | `alert_log` (optionally `reason`, `requested_by`) |
| FlashArray - DR drill | `playbooks/dr_drill.yml` | nothing | `dr_drills`, `alert_log` |
| FlashArray - QoS cap | `playbooks/qos_cap.yml` | `target_array`, `volume`, `mode` (`cap`), `cap_iops`, `reason` | `alert_log` |
| FlashArray - protect now | `playbooks/protect_now.yml` | `target_array`, `extend_eradication_by_s`, `audit_user` and `audit_command` (constants naming the workflow, so the record shows what asked) | `alert_log` |
| FlashArray - identify LED | `playbooks/identify_led.yml` | `target_array`, `component`, `led_state`, `severity` (`info`) | `alert_log` |
| FlashArray - DR promote | `playbooks/dr_promote.yml` | `target_array`, `pod` | `alert_log` |
| FlashArray - DR demote | `playbooks/dr_demote.yml` | `target_array`, `pod`, `quiesce` | `alert_log` |

The protect-now playbook acts on every local protection group of the
array, so its form has no protection-group input.

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
array credentials. `aap/seed.yml` attaches the **FlashArray fleet**
credential to every template (one JSON list of arrays, from which
`playbooks/tasks/resolve_array.yml` picks the target array's `fa_url` and
`api_token` at run time), and also creates one **FlashArray API**
credential per array (`FlashArray - <array>`, injecting `fa_url`,
`api_token` and `credential_array`). The two capacity nodes set
`credential_names` to `FlashArray - ${trigger.event.array}`, the event's
own array, and the day-2 job nodes to `FlashArray - ${trigger.array}`, the
array the form asked for (the node resolves the names in the organization
at launch): a per-array credential on the launch takes precedence over the
fleet, and the playbooks refuse to run when `credential_array` is not the
array they were asked to act on. The DR-drill and DR failover test nodes
use what their templates carry; pick a specific array's credential on a
node the same way.

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
- **FlashArray - QoS cap** publishes `array`, `volume`, `mode`,
  `applied_iops`, `previous_iops` and `changed`.
- **FlashArray - protect now** publishes `array`, `snapshot_suffix`,
  `groups_snapshotted`, `groups_failed` and `eradication_delay_days`.
- **FlashArray - identify LED** publishes `array`, `component`, `led_state`
  (`on` or `off`), `led_changed` and `led_error`.
- **FlashArray - DR promote** and **FlashArray - DR demote** publish
  `array`, `pod`, the pod's promotion status before and after, the replica
  link's status and `changed`; the failover test's notify nodes report
  them.

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
| `authorized_service_account_ids` | the `eda_trigger` | the service account the EDA bridge authenticates as |

`organization_name` is `FlashArray`, the organization `aap/seed.yml`
creates (`aap_organization`); change both together if the templates live
elsewhere. Approvers are named, not identified: `approver_groups` (and
`approver_users`) take group and user names, so they are not placeholders.
The shipped definitions name `admins`, the built-in group every instance
has; change it to the group that should decide. `decision_window` is 86400
seconds (24 hours); an approval nobody decides within it expires.
`fallback_decision` (`approve` or `reject`) names the port an expired
approval takes; the shipped definitions leave it unset, so an expired
approval never takes the `approved` port. How the execution ends in that
case (the `rejected` port or a failed node) is not verified.

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
every edge has `from`/`to` (plus `from_port`, `to_port`) naming existing
ids and never ending at a trigger; every trigger and node of a type with a
known parameter schema carries only the parameters that schema lists
(`parameter_keys`: the orchestrator refuses an unknown key), a scheduled
trigger has `schedule_type` and the matching `cron` or `interval`, a webhook
or EDA trigger its `webhook_path` and `authorized_service_account_ids`; an
approval node
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
node, a trigger id (an alias of `trigger`) or one of the `trigger`,
`workflow_context` and `loop` namespaces, and a
node it names runs before the referencing node (not on a parallel branch
or later); every `${trigger...}` reads what the trigger exposes
(`trigger_ref`: a top-level key of the body for a webhook or EDA trigger
and a property of the `input_schema` for a manual trigger, anything when the
trigger declares no `input_schema`; `scheduled_at` or `triggered_at` for a
scheduled trigger);
every node has an incoming edge (`unreachable`); `from_port` appears only
on edges leaving an approval, condition, switch or loop node; and every
condition and switch-case expression, with each `${...}` replaced by a
name, parses into the safe evaluator's operators only (`==`, `!=`, `>`,
`<`, `>=`, `<=`, `and`, `or`, `not`, `in`, names and literals; no `&&`,
arithmetic or function calls).

## Checked against a live instance

Contract facts read from the node schemas and the engine of the upstream
code at the tag Red Hat ships as the 2026.8 release (API 1.3.1); the
sections above follow them:

- A webhook or EDA body is passed through unwrapped: the trigger's output
  is the request body, read as `${trigger.<top-level key>}`, and the
  trigger's id is registered as a second name for the same output.
- Approval nodes take `approver_groups` and `approver_users` (names),
  `prompt`, `decision_window` (seconds) and `fallback_decision`; their
  ports are `approved` and `rejected`, and the decision recorded is one of
  approved, rejected, expired and cancelled.
- Condition ports are `true` and `false`; loop ports `iterate` and
  `complete`.
- An `aap_job_template` node names its job credentials with
  `credential_names` (or `job_credentials` ids); every node schema has
  `additionalProperties: false`, so a key outside the list is refused at
  import (the `parameter_keys` rule).
- A scheduled trigger takes `schedule_type` (`cron` or `interval`), the
  matching `cron` or `interval`, `timezone` (UTC when unset) and
  `missed_schedule_policy`; its output is `scheduled_at` and
  `triggered_at`.
- An AAP node's result carries `job_id`, `job_url`, `job_status`,
  `artifacts` (the job's `set_stats`), `created`, `started` and `finished`.

Verified with a run of the capacity workflow (docs/ORCHESTRATOR.md,
"Verified against a live orchestrator"): a full-span `${...}` expression
keeps its type (`candidate_targets` and `top_growers` reach the playbook
as lists, `used_pct` as a float); `credential_names` is interpolated
before the names are resolved, so `FlashArray - ${trigger.event.array}`
selects the event array's credential; artifacts and `job_url` surface as
described; the condition takes its `true` port; the approval node pauses
the execution for the `admins` group, and its `approved` port runs the
reclaim with the request rebuilt from artifacts.

Not verified: how a loop node's body is wired back to the node. The edge
model is `{from, to, from_port?, to_port?}`, and the linter rejects every
cycle, so a definition with a loop body does not pass `make lint-ao`.
