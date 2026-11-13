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
  credentials when `AO_CLIENT_ID` is empty. For Slack, optionally
  `SLACK_WEBHOOK_URL` (an incoming webhook, stored as a credential on the
  notify template) and `SLACK_BOT_TOKEN` with `SLACK_CHANNEL` (a bot token
  with `chat:write`, invited to the channel, for the platform's own Slack
  notification template).
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
    export AO_URL=... AO_CLIENT_ID=... AO_CLIENT_SECRET=...              # optional: the orchestrator credential
    export SLACK_WEBHOOK_URL=... SLACK_BOT_TOKEN=... SLACK_CHANNEL=...   # optional: Slack
    ansible-playbook -i aap/inventory/example.yml aap/seed.yml -e @vars/local-aap.yml

`aap/vars/example.yml` is loaded as the defaults; the `-e @file` overrides
them. `aap/inventory/example.yml` is just localhost: the seed talks to the
platform API from where it runs.

## What it creates

| Object | Name | Notes |
| --- | --- | --- |
| Organization | `FlashArray` | gateway object (`ansible.platform.organization`) |
| Setting | `TOWER_URL_BASE` | the base URL the platform writes into the links of its notifications; `aap_url_base`, by default the gateway URL (a lab reachable by address only puts the address there) |
| Credential type | `FlashArray fleet` | one input, `arrays`: a JSON list of `{name, fa_url, api_token}`, one entry per array, name as in the inventory; injected as the extra_var `purefa_fleet_json`, from which `playbooks/tasks/resolve_array.yml` picks the array a launch names |
| Credential type | `FlashArray API` | inputs `fa_url`, `api_token` (secret), `array_name`; injected as the extra_vars `fa_url`, `api_token` and `credential_array`; the playbooks refuse to act on an array other than `credential_array` |
| Credential type | `Orchestrator service account` | inputs `orchestrator_url`, `client_id`, `client_secret` (secret); injected as the environment variables `AO_CLIENT_ID`, `AO_CLIENT_SECRET` and the extra_var `orchestrator_url` |
| Credential | `FlashArray fleet - <organization>` | every entry of `aap_arrays`, values from the named environment variables; attached to every job template |
| Credentials | `FlashArray - <array>` | one per entry in `aap_arrays`; for launches that pin one array (`credential_names` on an orchestrator node, or the credential prompt) |
| Credential | `Orchestrator service account - flasharray-eda` | only when `AO_CLIENT_ID` is set |
| Credential type | `Slack webhook` | one secret input, `webhook_url`; injected as the environment variable `SLACK_WEBHOOK_URL`, which `playbooks/notify.yml` posts to |
| Credential | `Slack webhook - flasharray-eda` | only when `SLACK_WEBHOOK_URL` is set; attached to `FlashArray - notify` |
| Credential | `Platform token - flasharray-eda` | the built-in `Red Hat Ansible Automation Platform` type, holding the gateway and the token the variable `aap_platform_token_env` names (the seeding token by default; a user with read on the Event-Driven Ansible controller is the right owner); injected as `AAP_HOSTNAME` and `AAP_TOKEN` into `FlashArray - definitions check` |
| Inventory | `FlashArrays` | `localhost` plus one host per array, named as in `purefa_arrays`; `ansible_connection: local` |
| Project | `FlashArray EDA` | git, `aap_project_url` on `aap_project_branch`; synced once by the seed |
| Execution environment | `FlashArray EDA EE` | image from `aap_ee_image` |
| Job templates | `FlashArray - capacity request`, `FlashArray - capacity reclaim`, `FlashArray - notify`, `FlashArray - expand volume`, `FlashArray - DR drill`, `FlashArray - identify LED`, `FlashArray - reconcile`, `FlashArray - apply baseline`, `FlashArray - protect now`, `FlashArray - QoS cap`, `FlashArray - remediate DNS`, `FlashArray - remediate NTP`, `FlashArray - replicate now`, `FlashArray - preferred array`, `FlashArray - alert runbook`, `FlashArray - orchestrator post`, `FlashArray - DR promote`, `FlashArray - DR demote`, `FlashArray - DR switchover`, `FlashArray - Alertmanager silence`, `FlashArray - self check`, `FlashArray - definitions check` | one per playbook; the fleet credential attached (except the definitions check, which reaches no array); prompt for extra vars, credentials and a limit; `alert_log`, `request_dir`, `baseline_dir` from `aap_job_extra_vars`; the DR drill carries `aap_dr_drills`; apply baseline is limited to `localhost` plus `aap_default_array` (one launch applies one array's baseline); the orchestrator post also carries the service account credential; the Alertmanager silence carries `aap_alertmanager_url` as `alertmanager_url`; the self check carries `aap_self_check_tests` and `aap_self_check_alert_watchers`; the definitions check carries the platform token credential, the orchestrator credential when it exists, the site vars the seed read (`expected_activation_vars`) and the `aap_orchestrator_*` names ("Schedules") |
| Workflow job template | `FlashArray - capacity reclaim (controller tier)` | request -> approval -> reclaim; notify on failure or denial |
| Notification template | `Slack - flasharray-eda` | only when `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` are set: type Slack, posts every job template's failures and the workflow's success, failure and approval events (pending, approved, denied, timed out) to the channel, each with its link into the platform |
| Schedules | `FlashArray - DR drill (nightly)`, `FlashArray - self check (weekly)`, `FlashArray - definitions check (nightly)` | iCal rules from `aap_dr_drill_schedule`, `aap_self_check_schedule` and `aap_definitions_check_schedule` in `aap_schedule_timezone`; the drill schedule passes `dr_drill_fail_job: true` so a failed drill fails the job ("Schedules") |
| EDA decision environment | `FlashArray EDA DE` | image from `aap_eda_de_image`, pulled with `aap_eda_de_pull_policy`: the image `aap/decision-environment.yml` or `make de` builds, pushed to a registry or loaded into the store the activation workers use ("Images on a containerized platform") |
| EDA project | `FlashArray EDA` | same repository and branch |
| EDA credential types and credentials | `FlashArray fleet`, `FlashArray API`, `Orchestrator service account`; `FlashArray fleet - <organization>`, `FlashArray - <array>`, `Orchestrator service account - flasharray-eda` | same inputs as the controller types; injected as variables (the fleet list as `purefa_arrays`); `Platform user - flasharray-eda` (the built-in `Red Hat Ansible Automation Platform` type, the user the activations launch job templates as, host `<gateway>/api/controller/`; `aap_eda_platform_*`) |
| EDA rulebook activations | `FlashArray rulebook - <name>` | one per file in `extensions/eda/rulebooks/platform/`, with the fleet credential, the `Platform user - flasharray-eda` credential (the one the `run_job_template` actions launch as) and, when it exists, the orchestrator credential attached; disabled unless the site vars say otherwise (see below) |

The names in the job template column are fixed: the orchestrator workflow
definitions and the platform rulebooks' `run_job_template` actions refer
to them, `orchestrator/job_templates.txt` lists the nineteen a definition
or a platform rulebook launches (`FlashArray - apply baseline` is launched
by people and the git-push ruleset only, `FlashArray - self check` and
`FlashArray - definitions check` by their schedules and people), and
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
standalone webhook ruleset stands in for. Launch it by hand with the
variables the capacity rule passes to `capacity_request.yml`
(`at_risk_array`, `used_pct`, `days_to_full`, ... and `target_array` set to
the same array so the notify records carry it); the platform capacity
rulebook does not launch it (it runs `FlashArray - capacity request` on
its own, or hands the event to the orchestrator). The variables must have the event's
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

## Schedules

Three things run at a fixed hour, as controller schedules on the templates
above (`ansible.controller.schedule`; iCal rules built from
`aap_dr_drill_schedule`, `aap_self_check_schedule` and
`aap_definitions_check_schedule`, times a quoted `HH:MM` in
`aap_schedule_timezone`, the weekday an iCal code; the seed refuses an
unquoted time, which YAML reads as a number; a schedule with
`enabled: false` is created and never fires). A schedule launches its template with the template's extra
vars plus its own extra data, and a failed job is reported by the
template's failure notification (`Slack - flasharray-eda` when the bot
token is seeded), so a failed drill, a broken pager path or a drifted
definition reaches the channel with its link into the platform.

- **`FlashArray - DR drill (nightly)`** runs `FlashArray - DR drill` on
  `aap_dr_drills` with `dr_drill_fail_job: true`: a drill that found no
  completed snapshot to copy from, or whose copy failed, fails the job
  (the artifacts are published first). The standalone replication rulebook
  drills off a tick that restarts with the rulebook; the platform rulebook
  leaves that ruleset out (`tools/platform_rulebooks.py` drops tick-only
  rulesets as it drops webhook-only ones), so on the platform this
  schedule is the drill. The orchestrator's `dr_drill_schedule.json` runs
  the same drill from the orchestrator with a notify on failure; a site
  with both keeps one and disables the other (two drills at the same hour
  are harmless, the second finds the first's copy in flight and skips, but
  pointless).
- **`FlashArray - self check (weekly)`** runs `playbooks/self_check.yml`
  on every array of the inventory. Each array tests its own notification
  paths: a test e-mail to its alert watchers (`/alert-watchers/test`;
  `aap_self_check_alert_watchers` names them, empty means every enabled
  one), a `purity.test` line to each syslog server (`/syslog-servers/test`;
  the syslog rulebook records the line when it arrives, so the alert log
  holds both ends of that round trip) and its phone-home test
  (`/support/test`, which answers for phone home and remote assist).
  `aap_self_check_tests` picks the tests; `ntp` (`/arrays/ntp/test`) and
  `snmp_managers` (`/snmp-managers/test`) may be added. One `self_check`
  record per destination: a failure is critical and, after one retry, fails
  the job; an array with nothing to test (no enabled watcher, no syslog
  server) is a warning. A broken pager path is found on Monday morning
  instead of during an incident.
- **`FlashArray - definitions check (nightly)`** runs
  `playbooks/definitions_check.yml`, two comparisons that write nothing:
  `tools/ao_sync.py --check` of the orchestrator's published workflow
  definitions against `orchestrator/*.json`, as the service account the
  orchestrator credential injects (`aap_orchestrator_project` and the
  `aap_orchestrator_*` names fill the definitions' placeholders; skipped
  when that credential is not seeded), and `tools/activation_audit.py` of
  every `FlashArray rulebook - *` activation's extra vars against the site
  vars the seed read (the site file laid over the example file, put on the
  template as `expected_activation_vars`; the audit renders what each
  activation should carry from them exactly as the seed does), through the
  `Platform token - flasharray-eda` credential (the tool takes the gateway
  and the TLS setting from that credential's environment). The job fails on
  drift, on a comparison that could not be made, and with a warning record
  when neither could be made. The template's copy of the site vars is
  refreshed by every seed run, so the check says whether the activations
  match the last seed; "Checking the activations" below is the same
  comparison by hand, against the site file itself.

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
`GIT_WEBHOOK_SECRET` are therefore not seeded. The ruleset whose only
source is `ansible.eda.tick` (the DR drill) is left out as well: on the
platform the drill is the schedule above.

The seed creates the decision environment, the project, the three
credential types with their credentials, and one activation per platform
rulebook (`rulebook_name` is the file's path relative to
`extensions/eda/rulebooks`, `platform/<file>`, which is how the controller
lists a rulebook in a subdirectory). Each activation gets:

- as extra vars, the rulebook vars file (`FAEDA_VARS`, else
  `vars/example.yml`) laid over `vars/example.yml`, so a key the site file
  lacks keeps its default, minus `purefa_arrays` and `config_arrays` (the
  config and change-control sources fall back to the fleet list), plus
  `aap_organization`; minus `orchestrator_url` as well when the
  orchestrator credential is attached. The controller refuses an activation whose extra vars and
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

The activations end up **disabled** by default (`aap_eda_activations_enabled:
false`): `ansible.eda.rulebook_activation` creates an activation enabled
whatever it is told and cannot modify one afterwards, so the seed sets the
flag through the enable and disable endpoints on every activation on every
run, the one thing it can still change.
Set it true in the site vars file once the decision environment image,
with the `sdodsley.flasharray_eda` collection in it, is in the activation
workers' store, and keep it there: a later run with the example default
disables every running activation.

`aap_alertmanager_url` is the Alertmanager the "FlashArray - Alertmanager
silence" template talks to, as the execution environment reaches it (on a
containerized platform a loopback-only Alertmanager is out of reach). The
playbook sends no credentials, so reach the API over a network only the
execution environment can, or through a proxy that authenticates on the
job's behalf. Empty,
the template is a recorded no-op and the DR workflows run the same; one
that does not answer is recorded in the job and the switchover goes on.

## Checking the activations

An activation keeps the extra vars it was created with, and the seed
renders them from the site file laid over `vars/example.yml`, so a key the
site file has lost quietly takes the example default: the capacity
activation carried `orchestrator.example.com` for two days that way, and
the first handoff after the route was switched on posted to it, with
nothing but a warning record in a job to show for it. Two things now stop
the posting. The seed refuses to render extra vars whose
`orchestrator_enabled` is true while `orchestrator_url` is still the
example placeholder (unless the orchestrator credential injects it), and
`tools/activation_audit.py` compares every `FlashArray rulebook - *`
activation with what the seed would render today:

```
export AAP_TOKEN=...            # a platform token with EDA read (and write for --recreate)
python tools/activation_audit.py --url https://<gateway> --vars vars/<site>.yml \
    [--organization FlashArray] [--allow KEY]... [--recreate] [--insecure] [--json]
```

It prints one line per activation: `ok`, or the keys whose values differ,
are missing or are extra, plus any value that is an example placeholder
(`example.com`, documentation IP ranges, `REPLACE_AT_`) and any activation
that is disabled or not running. `--allow KEY` accepts a placeholder a site
means (a baseline that really is `example.com`). The exit status is 1 when
anything is wrong, so a schedule can run it; on a platform seeded with the
default `aap_eda_activations_enabled: false` every activation is reported
disabled and the audit exits 1 until they are enabled. `--recreate` deletes
each activation whose vars differ from the expected ones and creates it
again with those vars and the same project, rulebook, decision environment,
credentials, policies and enabled flag, waiting for it to run (a create
the gateway answers with an error but the controller carried out is
found by name and kept); a
placeholder-only, disabled or stopped activation is reported, not
recreated, and nothing is recreated while the expected vars themselves
still carry a placeholder (fix the site file first). Each delete and create
is printed as it happens, old id and new id, because the Rule Audit rows
stay under the old id. Run it after the seed and after any change to the
site file; the nightly `FlashArray - definitions check` runs the same
comparison from the platform against the site vars the last seed run left
on its template ("Schedules").

The plugin counts consecutive failed polls per array and signal and sets
`escalate: true` on `purefa_poll_error` once they reach its
`poll_error_escalate` source argument (default 10). The health rulebook
pages at warning on the first failure of a streak (once an hour per array);
the other polling rulebooks record theirs at info, which the default Slack
floor keeps in the alert log, so one unreachable array is one page rather
than one per rulebook; every rulebook pages at critical once its own streak
escalates (once every six hours), since a signal that fails on its own is
worth a page. An unreachable array, a revoked token or a wrong URL reaches
a person instead of a debug line.

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
recreates only that one; an activation keeps the rulebook and the extra
vars it was created with, so a changed rule or a new vars key reaches it
the same way (a rule that reads a key the activation's copy of the vars
file lacks fails at source start with "is undefined"). A change to an
event source plugin travels further still: the collection is built into
the decision environment, so the image is rebuilt, loaded into the store
the activation workers use, and the activation recreated. The run also sets
`aap_eda_activations_enabled` on every FlashArray activation, so a site
whose activations are running keeps the key true in its vars file. The
two projects are synced when the seed creates them and not on later runs,
and the job templates do not update the project on launch, so after a
push sync the controller project (and the EDA project, before recreating
an activation) or a job runs the revision the project last fetched. A
gateway answering 503 or 504 while the platform is busy fails the one
task that hit it; the seed is idempotent, so the next run finishes the
rest. The seed never deletes anything.

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

Slack: `FlashArray - notify` with the `Slack webhook` credential attached
posts the record to the channel (the post task returns 200, the failure
report is skipped). The `Slack - flasharray-eda` notification template
posts through the platform itself: the controller-tier workflow's approval
node, launched by hand, produced a successful "approval node needs review"
notification in the channel, and the approval bot posted its card beside
it. Its job-failure posts wait for a failed job.

The fleet credential is exercised by the activations: the change-control,
config-drift and replication activations launch the remediate NTP,
remediate DNS and notify templates, whose playbooks resolve the array from
`purefa_fleet_json` and run against it; the DR drill runs from its
schedule the same way. The three schedules were fired once each on the
platform (a one-run rule two minutes ahead, then the real rule put back):
the self check tested both arrays and failed the job on an array whose
watchers have no SMTP relay, with the syslog activation recording the test
lines it received; the drill copied, verified and destroyed a volume from
the latest replicated snapshot; the definitions check, launched by hand,
found every definition and activation in place
([docs/VERIFICATION.md](../docs/VERIFICATION.md)). The ten activations run
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
- **The Slack app.** The incoming webhook, the bot token and the Socket
  Mode token are created at api.slack.com for the channel
  ([slack/README.md](../slack/README.md)); the seed stores the webhook and
  the bot token once they exist, and the approval bot runs beside the
  platform, not on it.
