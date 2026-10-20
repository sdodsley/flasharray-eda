# Phase 1 record: change control, suppression, runbook map

Date: 2026-10-20. Same lab and tooling as [PHASE0.md](PHASE0.md).

## What was built

- **Change control from the audit log**
  (`extensions/eda/rulebooks/change_control.yml`). The audits signal with
  `automation_users`; governed commands by any other identity are reconciled
  from the git baseline (`playbooks/reconcile.yml`, `baseline/`), destructive
  commands by humans are reported, automation's own changes are logged. A
  second ruleset takes a git push webhook (`ansible.eda.webhook`) and applies
  the baseline forward to every array (`playbooks/apply_baseline.yml`).
- **Maintenance windows as the suppression primitive.** The plugin stamps
  `maintenance_active` on alert, audit, hardware and replication events from
  one cached `/maintenance-windows` read per array, so every rule can gate on
  it without cross-signal facts.
- **Alert-code runbook map** (`vars: alert_runbooks`,
  `playbooks/alert_runbook.yml`). Opened alerts route to notify, ticket, or
  ticket plus per-code tasks under `playbooks/runbooks/<code>.yml`, carrying
  the array's own action text and knowledge base link. Unknown codes take the
  default entry.

## Exit test results

| Step | What was done | Result |
| --- | --- | --- |
| human change | a second local user (created by the test) added an NTP server | audit arrived with that user's name, `reconcile.yml` reverted it to the baseline within one poll |
| human destroy | the same user created, destroyed and eradicated a scratch volume | each destructive command reported with user and interface |
| maintenance window | window opened, then the same drift again | audit arrived with `maintenance_active: true`, logged, not reconciled |
| git push | empty commit in the baseline repo, push payload POSTed to the webhook | `apply_baseline.yml` pulled and reapplied NTP and DNS to the array |
| runbook map | four replayed alerts: code 42, code 9999 (tasks), unknown 777, one inside a window | ticket, ticket plus per-code tasks, default notify, logged-not-routed |

## What the lab taught us

- Another identity was using the same account on the array (a QA harness
  logged `puredns setattr`, `purevol tag` and `puresnmp setattr` records
  during the run). With `automation_users` naming that account, change
  control logged them and acted only on the test user's changes. Running the
  automation as a dedicated service account is not optional.
- `ansible-rulebook --vars` values are not templated and `-e` means a file,
  so replay tests substitute fixture placeholders with `sed`.
- `ansible.eda` must be on `ANSIBLE_COLLECTIONS_PATH` for the webhook and
  generic sources; the launcher now adds a `collections/` directory beside
  the venv.
- The DR drill and the QoS state later in the plan both need array-side
  state; FlashArray tags (namespace `eda`) are used for that rather than
  rulebook facts, so a restart of the rulebook loses nothing.
