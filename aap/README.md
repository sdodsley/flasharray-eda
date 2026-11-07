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
  written to a file by the seed. `ansible.controller` 4.8.0, the build
  shipped with the 2.7 installer, declares `aap_token` but authenticates
  with HTTP basic auth only, so set `AAP_USERNAME` and `AAP_PASSWORD` for
  the same account as well; the gateway and Event-Driven Ansible modules
  use the token.
- The `ARRAY_*_URL` / `ARRAY_*_TOKEN` pairs for every array in `aap_arrays`
  (the same names `env.example` uses), and, if the orchestrator credential
  is wanted, `AO_URL`, `AO_CLIENT_ID` and `AO_CLIENT_SECRET`. The seed
  refuses to run with an array's pair empty and skips the orchestrator
  credentials when `AO_CLIENT_ID` is empty.
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
  does the same for the Event-Driven Ansible side, where the
  `sdodsley.flasharray_eda` collection that carries the event sources is
  needed as well), and `aap/Containerfile.ee` builds the same content with
  `podman build` when ansible-builder is not available; build one of them,
  then set `aap_ee_image` (and `aap_ee_registry` if it is pushed
  somewhere) to the result, or expect the job templates to fail on the
  first SDK call. The controller does not install Python packages for a
  project.

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
| Credential type | `FlashArray fleet` | one input, `arrays`: a JSON list of `{name, fa_url, api_token}`, one entry per array, name as in the inventory; injected as the extra_var `purefa_fleet_json`, from which `playbooks/tasks/resolve_array.yml` picks the array a launch names |
| Credential type | `FlashArray API` | inputs `fa_url`, `api_token` (secret), `array_name`; injected as the extra_vars `fa_url`, `api_token` and `credential_array`; the playbooks refuse to act on an array other than `credential_array` |
| Credential type | `Orchestrator service account` | inputs `orchestrator_url`, `client_id`, `client_secret` (secret); injected as the environment variables `AO_CLIENT_ID`, `AO_CLIENT_SECRET` and the extra_var `orchestrator_url` |
| Credential | `FlashArray fleet - <organization>` | every entry of `aap_arrays`, values from the named environment variables; attached to every job template |
| Credentials | `FlashArray - <array>` | one per entry in `aap_arrays`; for launches that pin one array (`credential_names` on an orchestrator node, or the credential prompt) |
| Credential | `Orchestrator service account - flasharray-eda` | only when `AO_CLIENT_ID` is set |
| Inventory | `FlashArrays` | `localhost` plus one host per array, named as in `purefa_arrays`; `ansible_connection: local` |
| Project | `FlashArray EDA` | git, `aap_project_url` on `aap_project_branch`; synced once by the seed |
| Execution environment | `FlashArray EDA EE` | image from `aap_ee_image` |
| Job templates | `FlashArray - capacity request`, `FlashArray - capacity reclaim`, `FlashArray - notify`, `FlashArray - expand volume`, `FlashArray - DR drill`, `FlashArray - identify LED`, `FlashArray - reconcile`, `FlashArray - apply baseline`, `FlashArray - protect now`, `FlashArray - QoS cap`, `FlashArray - remediate DNS`, `FlashArray - remediate NTP`, `FlashArray - replicate now`, `FlashArray - preferred array`, `FlashArray - alert runbook`, `FlashArray - orchestrator post`, `FlashArray - DR promote`, `FlashArray - DR demote` | one per playbook; the fleet credential attached; prompt for extra vars, credentials and a limit; `alert_log`, `request_dir`, `baseline_dir` from `aap_job_extra_vars`; the DR drill carries `aap_dr_drills`; apply baseline is limited to `localhost` plus `aap_default_array` (one launch applies one array's baseline); the orchestrator post also carries the service account credential |
| Workflow job template | `FlashArray - capacity reclaim (controller tier)` | request -> approval -> reclaim; notify on failure or denial |
| EDA decision environment | `FlashArray EDA DE` | image from `aap_eda_de_image`, pulled with `aap_eda_de_pull_policy`: the image `aap/decision-environment.yml` or `make de` builds, pushed to a registry or loaded into the store the activation workers use ("Images on a containerized platform") |
| EDA project | `FlashArray EDA` | same repository and branch |
| EDA credential types and credentials | `FlashArray fleet`, `FlashArray API`, `Orchestrator service account`; `FlashArray fleet - <organization>`, `FlashArray - <array>`, `Orchestrator service account - flasharray-eda` | same inputs as the controller types; injected as variables (the fleet list as `purefa_arrays`); `Platform user - flasharray-eda` (the built-in `Red Hat Ansible Automation Platform` type, the user the activations launch job templates as, host `<gateway>/api/controller/`; `aap_eda_platform_*`) |
| EDA rulebook activations | `FlashArray rulebook - <name>` | one per file in `extensions/eda/rulebooks/platform/`, with the fleet credential and, when it exists, the orchestrator credential attached; created disabled (see below) |

The names in the job template column are fixed: the orchestrator workflow
definitions and the platform rulebooks' `run_job_template` actions refer
to them, `orchestrator/job_templates.txt` lists them, and
`tests/unit/test_playbook_artifacts.py` checks that `seed.yml` carries
them. `FlashArray - orchestrator post` runs
`playbooks/orchestrator_post.yml`; it is created only when the orchestrator
credential exists and the synced project contains that playbook.

## How the array credentials reach the playbooks

The playbooks address an array as `fa_url` and `api_token` (host variables
in the standalone inventory, which reads them from the environment). On the
platform the seed provides those through credentials; the inventory holds
names only.

**The fleet credential.** The `FlashArray fleet` type carries every array
in one secret input (a JSON list of `{name, fa_url, api_token}`, built by
the seed from the environment variables `aap_arrays` names) and injects it
as the extra_var `purefa_fleet_json`. Every array-facing playbook starts
with `playbooks/tasks/resolve_array.yml`, which leaves `fa_url` and
`api_token` alone when they are already set, and otherwise picks the entry
whose `name` is the array the launch names (`target_array`,
`at_risk_array`, the request's array, the drill's `target_array`) and sets
`fa_url`, `api_token` and `credential_array` from it. The job template is
seeded with this credential, so one template, one workflow copy and one
orchestrator definition serve every array: the launch variables say which
array, the credential resolves it. The token list is stored encrypted in
the platform's credential store, is visible to nobody, and is rotated by
re-running the seed with `-e aap_update_secrets=true`. `apply_baseline`,
whose play spans every array, resolves each host's own entry and so
applies every host in the limit; its template is seeded with a limit of
`localhost` (the play that refreshes the checkout) plus `aap_default_array`,
and every template prompts for the limit.

**One array per launch.** The `FlashArray API` type injects one array's
pair as the extra_vars `fa_url`, `api_token` and `credential_array`, which
override host variables, so a job given that credential runs against that
array whatever host it names. The seed keeps one such credential per array
for launches that pin an array: an orchestrator node's `credential_names`,
a workflow node's credential list, a person in the credential prompt. Every
array-facing playbook refuses to run when `credential_array` is not the
array it was asked to act on, so a mismatch fails the job before any array
call; outside the platform `credential_array` is undefined and the check is
skipped. With a single-array credential attached, `apply_baseline` applies
only that array and ends the other hosts instead of failing.

**Not chosen: host variables.** Putting `fa_url` and `api_token` on each
host of the `FlashArrays` inventory would also let one job span every
array, but inventory variables are stored and displayed in clear to anyone
with read access to the inventory, and a token in an inventory is outside
the credential store's rotation and audit. The fleet credential keeps the
one-job-many-arrays property inside the credential store.

## Images on a containerized platform

On a containerized Ansible Automation Platform the controller's task
container keeps its own container image store, separate from the host
user's, and the Event-Driven Ansible activation workers start activations
through the host user's podman service (the workers' `CONTAINER_HOST` is
that user's podman socket; the worker image carries no podman of its own),
which the installer runs with a storage configuration of its own. An image
that exists only in the host user's command-line store (for example
`localhost/flasharray-eda-ee` built on the same machine) fails at job or
activation start with "pinging container registry localhost", because a
`localhost/` name that the store lacks is pulled from a registry called
localhost. Either push the image to a registry the platform can pull from
(the private automation hub counts) and reference it by that name, or load
it into the stores that need it:

    podman save localhost/flasharray-eda-ee:latest \
      | podman exec -i automation-controller-task podman load
    podman save localhost/flasharray-eda-de:latest | podman --remote load

The second command loads through the socket the activation workers use.
Set `aap_eda_de_pull_policy: never` for an image loaded that way, so that
the worker never tries to pull the `localhost/` name.

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
  `candidates`), so the reclaim works without one, but the request and its
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
`run_workflow_template` or by hand. The variables must have the event's
shape: `candidate_targets` is a list of `{array, used_pct}` records, not a
list of names. The request node's artifacts (`request_id`, `at_risk_array`,
`candidate_count`, `candidates`) are passed to the following nodes as extra
vars, which is how the reclaim node learns `request_id` and, when
`request_dir` is not a path shared by both jobs, rebuilds the request from
`at_risk_array` and `candidates` (see "Paths inside the execution
environment"; the reclaim node passes `rebuild_from_artifacts: true`,
without which the playbook refuses a missing request file). Launch
permission on `FlashArray - capacity reclaim` is permission to eradicate
already-destroyed objects (the reclaim rebuilds its request from the
`candidates` it is given when no request file exists); grant it to the
workflow's and the orchestrator's identity only, and give people the
workflow, not the template. The approval node waits `aap_approval_timeout_s`
seconds for an approver; its denial or timeout takes the failure edge to
a warning notify, and a failed job takes it to a critical one. The
reclaim node runs with `reclaim_action: eradicate_pending` and records
the approver as `workflow-approval`; the platform's activity stream
holds who approved. The request and reclaim nodes run their templates
with the fleet credential the templates carry, and `at_risk_array` picks
the array, so one copy of the workflow serves every array.

## The Event-Driven Ansible controller side

The rulebooks an activation runs are the platform rulebooks under
`extensions/eda/rulebooks/platform/`, generated from the standalone ones
by `tools/platform_rulebooks.py` (`python tools/platform_rulebooks.py`;
`tests/unit/test_platform_rulebooks.py` fails when the committed files
drift). A platform rulebook has the standalone rulebook's rulesets,
sources and rules, with three differences: every `run_playbook` is a
`run_job_template` naming the job template above for that playbook, in
the organization `aap_organization`, passing the same extra vars minus
`alert_log`, `request_dir` and `baseline_dir` (the templates carry those);
the project's event sources are named by their collection FQCN
(`sdodsley.flasharray_eda.purefa_events`,
`sdodsley.flasharray_eda.purefa_syslog`), which is how an activation
resolves them from the decision environment; and the rulesets whose only
source is `ansible.eda.webhook` (the capacity approvals and the git push)
are left out, because on the platform those are event streams with their
own credentials or the controller-tier workflow. `APPROVAL_TOKEN` and
`GIT_WEBHOOK_SECRET` are therefore not seeded.

The seed creates the decision environment, the project, the three
credential types with their credentials, and one activation per platform
rulebook (`rulebook_name` is the file's path relative to
`extensions/eda/rulebooks`, `platform/<file>`, which is how the controller
lists a rulebook in a subdirectory). Each activation gets:

- as extra vars, the rulebook vars file (`FAEDA_VARS`, else
  `vars/example.yml`) minus `purefa_arrays`, plus `aap_organization`;
  minus `orchestrator_url` as well when the orchestrator credential is
  attached. The controller refuses an activation whose extra vars and
  credentials define the same key, so what a credential injects is removed
  from the file's content;
- the `FlashArray fleet - <organization>` EDA credential, whose type
  injects the arrays list as the variable `purefa_arrays`, the list the
  `purefa_events` source polls, so the plugin needs no environment
  variables and one credential covers the fleet;
- the `Orchestrator service account - flasharray-eda` EDA credential when
  `AO_CLIENT_ID` is set, which injects `orchestrator_url`.

The per-array EDA credentials (`FlashArray - <array>`) are seeded but not
attached: the fleet credential covers every array, and the controller
refuses two credentials of one injecting type on an activation ("Key:
fa_url already exists in extra_vars"); they serve a rulebook copy that
polls one array only.

The activations are created **disabled** (`aap_eda_activations_enabled:
false`) and the flag is enforced on every activation on every run, because
`ansible.eda.rulebook_activation` cannot modify an existing activation and
the enable and disable endpoints are the one thing the seed can still set.
Set it true in the site vars file once the decision environment image,
with the `sdodsley.flasharray_eda` collection in it, is in the activation
workers' store, and keep it there: a later run with the example default
disables every running activation.

## Re-running safely

Run the same command again. Objects are matched by name and only
differences are applied; the two exceptions are `destroy_current_nodes`
on the workflow (off by default; `-e aap_rebuild_workflow_nodes=true`
recreates the nodes, which always reports changed) and secrets, which the
platform cannot read back: credentials are updated only with
`-e aap_update_secrets=true` (controller) and EDA credentials report
changed on every run. `ansible.eda.rulebook_activation` cannot modify an
existing activation; to change one (a new rulebook, a new variable in its
extra vars), delete it in the UI or through the API and re-run, which
recreates only that one. The run also sets `aap_eda_activations_enabled`
on every FlashArray activation, so a site whose activations are running
keeps the key true in its vars file. The seed never deletes anything.

## Verified on a platform

The seed and the controller-tier workflow are exercised on Ansible
Automation Platform 2.7, containerized growth topology on one RHEL 9.8
host, seeded from empty by this playbook (a repeat run reports a change
only for the EDA credentials, see "Re-running safely"), with the
execution environment built from `aap/Containerfile.ee` and loaded into
the controller's task container, against one FlashArray:

- `FlashArray - notify`, launched with `subject`, `severity`,
  `target_array` and `detail`, succeeds and publishes the artifacts
  `record_kind`, `severity` and `array`.
- `FlashArray - capacity reclaim (controller tier)`, launched with the
  capacity rule's variables: the request job reads the array's
  pending-eradication objects, writes the request and the alert record,
  and publishes `request_id`, `candidate_count` and `candidates`; the
  workflow pauses at the approval node; the approval records who decided
  (`approved_or_denied_by`); the reclaim node rebuilds the request from
  the artifacts (`rebuilt_from: job artifacts`) and asks the array to
  eradicate the candidates. A snapshot that belongs to a protection group
  snapshot is refused by the array ("Cannot eradicate volume snapshot
  because it belongs to a protection group snapshot"); the reclaim records
  each refusal, eradicates nothing, ends with `result: refused`, and the
  workflow completes successfully.

The fleet credential is exercised by the activations: the change-control,
config-drift and replication activations launch the remediate NTP,
remediate DNS, notify and DR drill templates, whose playbooks resolve the
array from `purefa_fleet_json` and run against it. The ten activations run
from the decision-environment image (`localhost/flasharray-eda-de`, loaded
through the host user's podman socket with `aap_eda_de_pull_policy: never`,
see "Images on a containerized platform"). The seed enables the activations
ten seconds apart: started in the same second they contend for the EDA
websocket and some fail their first start.

## What it cannot seed

- **The automation orchestrator.** It has no certified collection, and the
  seed does not drive its API: the AAP integration, the service account
  and its client credentials, the EDA trigger with the service account
  bound to it, and the workflow definitions are set up as
  [orchestrator/README.md](../orchestrator/README.md) describes. The seed
  only stores the service account's credentials, once they exist, so that
  `FlashArray - orchestrator post` and the activations can use them;
  `AO_URL` is the orchestrator as the jobs reach it, which for an
  orchestrator on the platform host of a containerized platform is
  `https://host.containers.internal:<port>` (a job's execution environment
  cannot reach the host's own address). Red
  Hat documents the add-on for OpenShift-based deployments only, so a
  containerized AAP 2.7 may not be able to host it.
- **The images.** The execution environment and the decision environment
  are references by name and image; building and pushing them is outside
  the platform.
- **The Automation Hub token** the collection install needs, and the
  platform's own subscription.
- **Approvers.** The workflow approval accepts any user with the approve
  permission on the workflow; granting that role is a platform RBAC step.
