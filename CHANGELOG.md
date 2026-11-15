# Changelog

## Unreleased

- `tools/activation_audit.py` rides out a busy gateway: transient answers
  (5xx, 403, a dropped connection on a read) are retried for two minutes
  instead of seconds, the activation list is read in pages of twenty, the
  wait for the list after a delete lasts the name-release window, and
  `--recreate --enable` creates the new activation enabled whatever flag
  the old one carried. The nightly definitions check no longer fails on a
  single 500 from the gateway.
- The seed refuses to go on when the activation list it read has more
  pages than it fetched, instead of missing activations and creating
  duplicates on a platform with more than one page of them.

## 0.4.1 (2026-11-14)

A maintenance release. The seed's enabled-flag enforcement acts on
FlashArray activations only, which matters on a platform shared with
another project, and the documentation describes the tree as it stands
instead of the order it was built in, with the changelog contracted to
match.

Upgrading from 0.4.0: sync the controller project and run `aap/seed.yml`
again; nothing else changes.

- The seed enforces the enabled flag on FlashArray activations only. The
  EDA API ignores the name filter on its activation list, so the seed picks
  ours out by name and leaves another project's activations alone.
- The documentation describes the tree as it stands rather than the order
  it was built in; CONTRIBUTING.md carries the writing rules, and the
  changelog is contracted to match.

## 0.4.0 (2026-11-14)

Housekeeping runs on controller schedules, the Slack approval bot survives
restarts and reports outcomes, planned work does not page, the loop watches
its own polls and activations, the alert lifecycle is modelled, every
template playbook can be run twice, and the QoS cap understands volume
groups. Verified on Purity//FA 6.10.6 and Ansible Automation Platform 2.7
with the automation orchestrator; docs/VERIFICATION.md has the ledger.

Upgrading from 0.3.1, in this order: sync both platform projects, the
controller one before the seed and the EDA one before any activation is
recreated; rebuild the decision environment and load it into the store the
activation workers use (aap/README.md), as the platform rulebooks need
fields only the rebuilt image emits; run the seed again, with the three
orchestrator names in the vars file or the definitions-check schedule
disabled on a site that seeds the orchestrator credential; publish the
changed orchestrator definitions with `tools/ao_sync.py`; recreate every
activation with `tools/activation_audit.py --recreate`; recreate the bot
container with a writable `STATE_DIR` volume (slack/README.md) and a
platform token whose user may execute the notify template.

- The QoS cap understands volume groups. The array refuses a volume limit
  on a member of a group with its own, so the telemetry signal reports the
  group and `qos_cap_scope` chooses a warning (the default), a cap on the
  whole group or an attempt on the volume with the refusal recorded.
- The Slack approval bot keeps its cards on disk, so a restart edits them
  instead of posting again, and rewrites each card with its outcome and who
  decided. It reminds in the thread at half and ninety percent of the
  decision window, posts a critical message for an undecided approval, and
  can route a second project's cards to its own channel.
- The seed creates three controller schedules: the nightly DR drill, the
  weekly self check, in which every array tests its alert e-mail, syslog
  and phone-home paths, and the nightly definitions check, which runs the
  orchestrator sync in check mode and the activation audit. A site that
  seeds the orchestrator credential names the three orchestrator
  identities in its vars file or disables the definitions-check schedule,
  or the seed refuses to run.
- The playbooks that span every array write the shared alert log one array
  at a time, so parallel writers cannot lose a record.
- Every template playbook honours the run-twice contract: a second launch
  with the same extra vars leaves the array as it is and says so. Each
  playbook recognises work already done or in flight, and the orchestrator
  handoff records pending when an execution is handling the same event
  (`orchestrator_dedupe: false` turns that off).
- The alert lifecycle is modelled. A resolved alert is notified once, one
  severity below its own; an alert whose
  severity rises or which the array reopens is routed again; the health
  rulebook clears the identify LED it lit when the component recovers; and
  the runbook map falls through from alert code to component type
  (`alert_runbooks_by_type`) to default.
- Planned work does not page: the switchover tags the source pod before
  demoting, the plugin marks both sides' replica-link events as planned
  while the tag is fresh, and the replication rulebook logs them instead
  of notifying. The failover-and-failback
  workflow also silences the replica-link rules in Alertmanager around
  each switchover, a recorded no-op without `aap_alertmanager_url`.
- The loop watches itself: the plugin counts consecutive failed polls per
  array and signal, the health rulebook pages at warning on the first
  failure of a streak and every rulebook at critical once the streak
  reaches the threshold (10 by default). `tools/activation_audit.py`
  compares every FlashArray activation's extra vars with what the seed
  would render and recreates the drifted ones on request.
- The orchestrator sync tool ignores the canvas position the UI saves on a
  node, so a dragged box does not publish a new version, and its check mode
  exits 1 when any definition would change.
- Notifications reach Slack only at or above `slack_min_severity` (warning
  by default); every record still reaches the alert log, and the job says
  when a record stayed out of the channel.
- The demote playbook eradicates the undo-demote pod an earlier demotion
  left when `eradicate_undo_demote` is true, as the switchover does;
  without the flag the play refuses and names the pods.

## 0.3.1 (2026-11-09)

The QoS cap holds up under a real load: it caps only when it tightens,
changes nothing when run twice, and a lift leaves the volume as the cap
found it; the pod guards report the array's message for a missing pod.
Verified on Purity//FA 6.10.6 and Ansible Automation Platform 2.7;
docs/VERIFICATION.md has the ledger.

- The pod guards of the DR playbooks report the array's message for a
  missing pod instead of an undefined-variable error.
- A lifted cap leaves the volume without a limit: the lift clears it
  through the API, because the collection module writes the array's
  maximum for a zero limit; a real previous limit is restored through the
  module.
- The lift's normal-poll count starts from the cap, so a day-2 cap on a
  volume whose latency was normal lasts the configured polls.
- The cap is idempotent and never loosens a limit: a repeated cap or lift
  changes nothing, and a volume whose own limit is at or below the
  requested cap keeps it, so a lift restores the same value.
- The undo-demote refusal is the collection module's, not the array's; the
  switchover's own eradicate step stays until a collection release carries
  the module's option.

## 0.3.0 (2026-11-09)

The syslog path runs on the platform from both arrays, with the audit and
alert layouts parsed into fields. Notifications reach Slack and approvals
come back through a bot. A planned ActiveDR failover and failback runbook
joins the orchestrator workflows, and a sync tool imports the definitions
into a live orchestrator. Verified on Purity//FA 6.10.6, Ansible Automation
Platform 2.7 and a live orchestrator; docs/VERIFICATION.md has the ledger.

- Every approval node names the group `approvers` beside the admins, so the
  people who decide and the bot's orchestrator user can sit in a group of
  their own; an undecided approval expires and fails its execution whatever
  the fallback decision says.
- A planned ActiveDR failover and failback runbook: the switchover playbook
  demotes the source pod, waits for the target's copy and promotes the
  target pod; with the sides swapped it fails back.
  With `eradicate_undo_demote` true it first eradicates the undo-demote
  pods of earlier demotions, otherwise it refuses and names them; an
  orchestrator definition runs it behind two approvals.
- `tools/ao_sync.py` imports the orchestrator definitions: it creates the
  workflows the project lacks, updates the existing ones by name,
  validates, publishes a changed definition as the next version and never
  deletes; a dry run writes nothing.
- The orchestrator linter knows loop nodes, their ports and the one allowed
  back edge.
- A second orchestrator route hands over a protection group whose
  replication is overdue or stuck and gates the immediate replicated
  snapshot behind an approval; `orchestrator_fallback_local` raises the
  local capacity request when a handoff produced no execution.
- The syslog source parses what Purity//FA 6.10.6 sends, the audit and
  alert layouts into fields graded by the severity word. By default only
  the alert, audit and test programs are emitted (`syslog_programs`) and
  the rest of the controllers' system log is dropped and counted; the
  array's own service accounts count as automation, so the human-action
  rule stays quiet for the array's scheduled work.
- The notify playbook posts every notification to an incoming webhook when
  `SLACK_WEBHOOK_URL` is set, and the seed creates a Slack notification
  template for job failures and controller-tier approval events when a bot
  token and channel are set.
- A Slack Socket Mode bot posts pending orchestrator and controller
  approvals as cards with Approve and Reject buttons and carries the
  decision back with the Slack user's name; `SLACK_APPROVERS` limits who
  may click.
- The immediate replication playbook publishes job artifacts, and a unit
  test checks that every artifact a definition reads is published.
- The ansible-builder definitions build on the supported base, and the
  decision environment builds on a vfs image store (docs/PACKAGING.md has
  the rootless-overlay caveat).
- The seed sets the platform's base URL for notification links
  (`aap_url_base`, the gateway URL by default).

## 0.2.0 (2026-11-07)

The project runs on Ansible Automation Platform 2.7 and drives the
automation orchestrator: ten rulebook activations, eighteen job templates,
the controller-tier approval workflow and seven orchestrator workflows.
Verified on Purity//FA 6.10.6, a 2.7 installation and a live orchestrator;
docs/VERIFICATION.md has the ledger.

- The repository is the collection `sdodsley.flasharray_eda`; the rulebooks
  name their sources by FQCN, and CI builds and installs the collection
  before the unit tests.
- The decision environment installs the collection at image build time, so
  activations resolve the sources without a source directory
  (docs/PACKAGING.md).
- A fleet credential type carries every array as a JSON list for controller
  jobs and EDA activations; every array-facing playbook picks the target
  array's URL and token from it.
- Platform rulebooks, generated from the local ones and checked by a unit
  test: every run-playbook action becomes a job template launch and the
  webhook rulesets are left out.
- The seed creates one job template per playbook (eighteen), a platform
  user credential for the launches, and one activation per platform
  rulebook with the fleet credential and the site vars, enabled when
  `aap_eda_activations_enabled` is true.
- Four more orchestrator definitions: the day-2 QoS cap, immediate
  protection and identify LED, and an ActiveDR failover test behind two
  approvals. All seven follow the contract of the 2026.8 orchestrator
  release, and the linter checks the parameter keys of every trigger and
  node type.
- The Prometheus rules follow the names and labels of the array's native
  OpenMetrics exporter; example scrape and Alertmanager routing
  configurations sit beside them.

## 0.1.0 (2026-11-06)

First release of flasharray-eda: Event-Driven Ansible for standalone
FlashArrays, a prototype. Validated on Purity//FA 6.10.6 (REST 2.46); the
syslog listener and the Prometheus rules are tested with replayed input.
docs/VERIFICATION.md lists what was exercised on hardware,
docs/HARDWARE_NOTES.md the array, SDK and collection facts, and
docs/DESIGN.md the design.

- The events source polls the REST API for alerts, audits, health,
  telemetry, replication and config signals, reading each array's URL and
  token from named environment variables.
- The syslog source receives the arrays' syslog stream, filters senders by
  an allow list, takes the array name from the message and emits one event
  shape for every line.
- Ten rulebooks: alerts, change control, config drift, health, replication
  (with the DR drill), capacity (with the approval gate), performance (QoS
  cap with hysteresis), anomaly, syslog and alertmanager.
- Listener hardening: the approvals webhook requires a bearer token, the
  git-push webhook an HMAC signature, and the syslog and Alertmanager
  listeners bind a configured host.
- The playbooks act on the arrays through everpure.flasharray 1.46.0 or
  later: per-code alert runbooks, an identify LED and a day-2 expand
  volume behind size and name guards.
- Every playbook a workflow or orchestrator node may follow publishes job
  artifacts, and the capacity reclaim rebuilds its request from them when
  `rebuild_from_artifacts` is set, so an approved request completes as a
  later job.
- `orchestrator_enabled` (default false) routes the capacity-at-risk rule
  to a handoff playbook that posts the event to the orchestrator workflow's
  EDA trigger; a failed post is recorded as a warning and never fails the
  rule, and with the switch off the approvals webhook handles the approval
  locally.
- The orchestrator directory holds three workflow definitions (capacity
  reclaim with approval, day-2 expand volume, nightly DR drill) with
  placeholders for per-environment ids; a linter checks them offline in CI
  (orchestrator/README.md).
- The seed playbook seeds AAP 2.7 with the organization, credentials,
  inventory, project, execution environment, eleven job templates, the
  controller-tier capacity workflow and the EDA objects, activations
  disabled (aap/README.md).
- The array API credential type carries the array's inventory name, and
  every array-facing playbook refuses to run against another array.
- The launcher runs one rulebook, loads the environment file
  (`env.example` is the template) and forwards the secrets to
  ansible-rulebook; the example vars file documents every variable the
  rulebooks reference.
- Prometheus rules for the array's native OpenMetrics exporter, consumed by
  the alertmanager rulebook.
- A decision environment on the ansible-rulebook 1.3.2 image;
  docs/PACKAGING.md lists the steps to move the content into the
  everpure.flasharray collection.
- Unit tests for the plugins and static checks over the rulebooks run under
  make check and GitHub Actions.
- The end-to-end scripts run against real arrays, write only under the
  tests output directory and refuse the eradicate step on an array with
  pending-eradication objects unless `E2E_ERADICATE_ALL=1`.
- Apache-2.0; SPDX headers on every Python and shell file.
