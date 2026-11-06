# Ansible Automation Platform seed

`seed.yml` builds, from an empty Ansible Automation Platform 2.7, the
objects this project needs to run its playbooks as job templates, hand the
capacity reclaim to a human through a workflow approval, and host the
rulebooks as Event-Driven Ansible activations. It is idempotent: every
object is addressed by name, so re-running it applies only what changed.

It creates nothing on an array and nothing in the automation orchestrator
(see "What it cannot seed").

## Prerequisites

- An Ansible Automation Platform 2.7 installation reachable at one gateway
  URL, with automation controller and the Event-Driven Ansible controller
  enabled.
- `AAP_HOST` (the gateway URL) and `AAP_TOKEN` in the environment. The
  first run creates the organization, which needs a platform
  administrator's token; after that an organization administrator's token
  is enough. The token is read from the environment only; it is never
  written to a file by the seed.
- The `ARRAY_*_URL` / `ARRAY_*_TOKEN` pairs for every array in `aap_arrays`
  (the same names `env.example` uses), and, if the orchestrator credential
  is wanted, `AO_URL`, `AO_CLIENT_ID` and `AO_CLIENT_SECRET`. The seed
  refuses to run with an array's pair empty and skips the orchestrator
  credential when `AO_CLIENT_ID` is empty.
- The certified collections `ansible.platform`, `ansible.controller` and
  `ansible.eda` on the machine that runs the seed. The first two are
  published on Red Hat Automation Hub only, so they live in
  `aap/requirements.yml` rather than the top-level file that CI installs
  from Galaxy:

      ansible-galaxy collection install -r aap/requirements.yml   # Hub configured in ansible.cfg

- An execution environment image the platform can pull that contains
  py-pure-client and the `everpure.flasharray` collection. The default is
  the supported `ee-supported-rhel9` name with no registry; it lacks both.
  `aap/execution-environment.yml` is an ansible-builder definition that
  adds them on top of the supported base (`aap/decision-environment.yml`
  does the same for the Event-Driven Ansible side); build it, then set
  `aap_ee_image` (and `aap_ee_registry` if it is pushed somewhere) to the
  result, or expect the job templates to fail on the first SDK call. The
  controller does not install Python packages for a project.

## Run

    cp aap/vars/example.yml vars/local-aap.yml      # gitignored with the other local files; edit
    export AAP_HOST=https://<gateway> AAP_TOKEN=<token>
    export ARRAY_A_URL=... ARRAY_A_TOKEN=... ARRAY_B_URL=... ARRAY_B_TOKEN=...
    ansible-playbook -i aap/inventory/example.yml aap/seed.yml -e @vars/local-aap.yml

`aap/vars/example.yml` is loaded as the defaults; the `-e @file` overrides
them. `aap/inventory/example.yml` is just localhost: the seed talks to the
platform API from where it runs.

## What it creates

| Object | Name | Notes |
| --- | --- | --- |
| Organization | `FlashArray` | gateway object (`ansible.platform.organization`) |
| Credential type | `FlashArray API` | inputs `fa_url`, `api_token` (secret), `array_name`; injected as the extra_vars `fa_url`, `api_token` and `credential_array`; the playbooks refuse to act on an array other than `credential_array` |
| Credential type | `Orchestrator service account` | inputs `orchestrator_url`, `client_id`, `client_secret` (secret); injected as the environment variables `AO_CLIENT_ID`, `AO_CLIENT_SECRET` and the extra_var `orchestrator_url` |
| Credentials | `FlashArray - <array>` | one per entry in `aap_arrays`, values from the named environment variables |
| Credential | `Orchestrator service account - flasharray-eda` | only when `AO_CLIENT_ID` is set |
| Inventory | `FlashArrays` | `localhost` plus one host per array, named as in `purefa_arrays`; `ansible_connection: local` |
| Project | `FlashArray EDA` | git, `aap_project_url` on `aap_project_branch`; synced once by the seed |
| Execution environment | `FlashArray EDA EE` | image from `aap_ee_image` |
| Job templates | `FlashArray - capacity request`, `FlashArray - capacity reclaim`, `FlashArray - notify`, `FlashArray - expand volume`, `FlashArray - DR drill`, `FlashArray - identify LED`, `FlashArray - reconcile`, `FlashArray - apply baseline`, `FlashArray - protect now`, `FlashArray - QoS cap`, `FlashArray - post orchestrator event` | one per playbook; prompt for extra vars, credentials and a limit; `alert_log`, `request_dir`, `baseline_dir` from `aap_job_extra_vars`; the DR drill carries `aap_dr_drills` and the credential of its first drill's target array; apply baseline is limited to `localhost` plus the default array (one launch applies one array's baseline) |
| Workflow job template | `FlashArray - capacity reclaim (controller tier)` | request -> approval -> reclaim; notify on failure or denial |
| EDA decision environment | `FlashArray EDA DE` | image from `aap_eda_de_image` (the `make de` image, pushed to a registry) |
| EDA project | `FlashArray EDA` | same repository and branch |
| EDA credential type and credentials | `FlashArray API`, `FlashArray - <array>` | same inputs; injected as variables |
| EDA rulebook activations | `FlashArray rulebook - <name>` | one per file in `extensions/eda/rulebooks/`, created disabled (see below) |

The names in the job template column are fixed: the orchestrator workflow
definitions and the rulebook actions refer to them, and
`tests/unit/test_playbook_artifacts.py` checks that `seed.yml` still
carries them. `FlashArray - post orchestrator event` runs
`playbooks/orchestrator_post.yml`; it is created only when the orchestrator
credential exists, and the project sync must already see that playbook.

## How the array credentials reach the playbooks

The playbooks address an array as `fa_url` and `api_token` (host variables
in the standalone inventory, which reads them from the environment). On the
platform there are two ways to provide those; the seed uses the first.

**Chosen: one credential per array, attached to the job template.** The
`FlashArray API` type injects the pair as extra_vars, which override host
variables, so a job runs against the array whose credential is attached.
Every template is seeded with the default array's credential
(`aap_default_array`) and prompts for credentials on launch, so a launch
picks another array by naming its credential: the orchestrator's
`aap_job_template` node has a `credentials` field, a workflow node has a
credential list, a person picks it in the prompt. The token is stored
encrypted in the platform's credential store, is visible to nobody, and
can be rotated by re-running the seed with `-e aap_update_secrets=true`.
The cost: one launch is one array. A controller job holds one credential
of a given type, so the fleet-wide playbooks (the DR drill across several
target arrays, `apply_baseline` across every array) are launched once per
array with that array's credential; a drill or baseline over a fleet is a
loop in the orchestrator or several workflow nodes. With a single
credential attached, a playbook that reads `hostvars[<array>].fa_url` sees
the attached array's values whatever host it names, so `target_array` or
`at_risk_array` must name the array whose credential is attached. The
playbooks enforce that: the credential type carries the array's inventory
name (`array_name`, filled from `aap_arrays[].name`) and injects it as
`credential_array`, and every array-facing playbook refuses to run when
`credential_array` is not the array it was asked to act on
(`at_risk_array`, the request's array, the drill's `target_array`, or the
play's host). A mismatch fails the job before any array call; outside the
platform `credential_array` is undefined and the check is skipped.
`apply_baseline`, whose play spans every array, applies only the attached
credential's array per launch and ends the other hosts instead of failing;
its template is seeded with a limit of `localhost` (the play that refreshes
the checkout) plus the default array, and every template prompts for the
limit, so launch it once per array with that array's credential and limit.
The orchestrator's capacity nodes pick `FlashArray - <event array>` per
run and the expand-volume node `FlashArray - <form array>`; the DR drill
template is seeded with the credential of `aap_dr_drills[0].target_array`,
the array the drill copies on.

**Not chosen: host variables.** Putting `fa_url` and `api_token` on each
host of the `FlashArrays` inventory would let one job span every array,
but inventory variables are stored and displayed in clear to anyone with
read access to the inventory, and a token in an inventory is outside the
credential store's rotation and audit. A custom credential type has fixed
fields, so one credential cannot carry a token per array either. The
inventory therefore holds names only.

## Paths inside the execution environment

A job runs in a fresh container. `aap_job_extra_vars` points `alert_log`,
`request_dir` and `baseline_dir` at `/tmp`, which exists, is writable and
vanishes with the job. The durable record of a platform job is its output
and its artifacts (the `set_stats` at the end of every playbook, shown on
the job and passed to the next workflow node). Two things need a path that
outlives a job:

- the controller-tier workflow and the orchestrator's capacity workflow:
  `capacity_reclaim.yml` reads the request file `capacity_request.yml`
  wrote, in a later job. Without a shared path it rebuilds the request from
  the artifacts the launching node passes (`array` or `at_risk_array`, and
  `candidates`), so the reclaim still works, but the request and its
  outcome then sit in two jobs' outputs rather than in one file;
- the baseline playbooks: `baseline_dir` is a git checkout.

For those, expose a host directory to the jobs (Settings > Jobs > Paths to
expose to isolated jobs), pin the templates to the instance group whose
nodes have it, and point the three variables there.

## The controller-tier workflow

`FlashArray - capacity reclaim (controller tier)` is the approval gate the
standalone webhook ruleset stands in for. Launch it with the variables the
capacity rule passes to `capacity_request.yml` (`at_risk_array`,
`used_pct`, `days_to_full`, ... and `target_array` set to the same array
so the notify records carry it), from the capacity rulebook with
`run_workflow_template` or by hand. The request node's artifacts
(`request_id`, `at_risk_array`, `candidate_count`, `candidates`) are passed
to the following nodes as extra vars, which is how the reclaim node learns
`request_id` and, when `request_dir` is not a path shared by both jobs,
rebuilds the request from `at_risk_array` and `candidates` (see "Paths
inside the execution environment"; the reclaim node passes
`rebuild_from_artifacts: true`, without which the playbook refuses a
missing request file). Launch permission on `FlashArray - capacity
reclaim` is permission to eradicate already-destroyed objects (the reclaim
rebuilds its request from the `candidates` it is given when no request file
exists); grant it to the workflow's and the orchestrator's identity only,
and give people the workflow, not the template. The approval node waits
`aap_approval_timeout_s` seconds for an approver; its denial or timeout
takes the failure edge to a warning notify, and a failed job takes it to a
critical one. The reclaim node runs with `reclaim_action:
eradicate_pending` and records the approver as `workflow-approval`; the
platform's activity stream holds who approved. The request and reclaim
nodes run their templates with the credential the template carries (the
default array's), so one copy of the workflow serves one array; a launch
for another array fails at the `credential_array` check instead of acting
on the wrong array. For a second array, seed the workflow again under
another name with that array's credential on both nodes.

## The Event-Driven Ansible controller side

The decision environment, the project, a credential type, one credential
per array and one activation per rulebook are created, each activation
with the rulebook vars file (`FAEDA_VARS`, else `vars/example.yml`) as its
extra vars and the array credentials attached. The activations are created
**disabled** (`aap_eda_activations_enabled: false`) because two things in
this tree are not yet done:

- activations resolve custom event sources only from collections in the
  decision environment, so `purefa_events` and `purefa_syslog` must be
  packaged and the rulebooks switched to FQCNs first
  ([docs/PACKAGING.md](../docs/PACKAGING.md));
- the plugins read each array's token from the environment variable named
  in `purefa_arrays[].api_token_env`, and an EDA credential injects
  variables, not environment. Until the plugin accepts the token as a
  variable, an enabled activation would refuse to start with the token
  "unset".

The array credentials are still seeded as EDA credentials (type
`FlashArray API`, injected as `fa_url` / `api_token`) so nothing secret has
to be typed in later. `APPROVAL_TOKEN` and `GIT_WEBHOOK_SECRET` are not
seeded; on the platform the webhook rulesets become event streams with
their own credentials.

## Re-running safely

Run the same command again. Objects are matched by name and only
differences are applied; the two exceptions are `destroy_current_nodes`
on the workflow (off by default; `-e aap_rebuild_workflow_nodes=true`
recreates the nodes, which always reports changed) and secrets, which the
platform cannot read back: credentials are updated only with
`-e aap_update_secrets=true` (controller) and EDA credentials report
changed on every run. `ansible.eda.rulebook_activation` cannot modify an
existing activation; to change one, delete it in the UI and re-run. The
seed never deletes anything.

## What it cannot seed

- **The automation orchestrator.** It has no configuration-as-code API and
  no certified collection: the AAP integration, the service account and
  its client credentials, the EDA trigger with the service account bound to
  it, and the workflow definitions are set up as
  [orchestrator/README.md](../orchestrator/README.md) describes. The seed
  only stores the service account's credentials, once they exist, so that
  `FlashArray - post orchestrator event` can use them; and the add-on is
  documented for OpenShift-based AAP only (generally available 2026-08-21,
  no published timeline for the containerized topology as of 2026-10-15),
  so a containerized AAP 2.7 may not be able to host it.
- **The images.** The execution environment and the decision environment
  are references by name and image; building and pushing them is outside
  the platform.
- **The Automation Hub token** the collection install needs, and the
  platform's own subscription.
- **Approvers.** The workflow approval accepts any user with the approve
  permission on the workflow; granting that role is a platform RBAC step.
