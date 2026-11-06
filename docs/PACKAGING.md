# Packaging into everpure.flasharray

A checklist for moving this prototype's Event-Driven Ansible content into
the `everpure.flasharray` collection. The repository runs standalone with
`ansible-rulebook --source-dir`; the steps below are what changes when the
content ships in the collection.

## Why it matters

Every rulebook names its sources bare (`- purefa_events:`,
`- purefa_syslog:`). A bare name resolves only through `--source-dir`,
which `run.sh` and the decision environment's `podman run` line pass. EDA
Controller activations pass no `--source-dir` and resolve custom event
sources only from collections installed in the decision environment, so
running these rulebooks as activations needs the plugins in a collection.

## Checklist

1. **Layout.** Copy `extensions/eda/plugins/event_source/purefa_events.py`
   and `purefa_syslog.py` to the same path in the collection, the rulebooks
   to `extensions/eda/rulebooks/`, and `playbooks/` (with its `runbooks/`
   subdirectory) to the collection's top-level `playbooks/`. The layout
   follows the collection convention, so this step is a copy.
2. **Source names.** Change the nine source declarations to FQCNs:
   `everpure.flasharray.purefa_events` in alerts, anomaly, capacity,
   change_control, config_drift, health, performance and replication, and
   `everpure.flasharray.purefa_syslog` in syslog. Do it in the collection
   copy only. ansible-rulebook looks in `--source-dir` first and then in
   collections, so bare names keep `./run.sh` working here without the
   collection installed; FQCNs here would break it for anyone who has not
   installed the collection.
3. **Playbook references.** `run_playbook: name: playbooks/notify.yml`
   becomes the collection playbook FQCN (`everpure.flasharray.notify`), one
   per playbook. Re-check `alert_runbook.yml`'s relative include of
   `runbooks/<code>.yml` from a collection playbook, where `playbook_dir`
   points into the installed collection.
4. **Requirements.** `py-pure-client` is a collection requirement. The
   rulebooks also use the stock `ansible.eda` sources (webhook, tick,
   generic, alertmanager): declare `ansible.eda` under `dependencies` in
   `galaxy.yml` or document it; `aiohttp` arrives with ansible-rulebook.
   `meta/runtime.yml` `requires_ansible` must cover the ansible-core the
   decision environment ships.
5. **Variables and secrets.** The `--vars` file (`vars/example.yml`) becomes
   the activation's extra vars. `APPROVAL_TOKEN` and `GIT_WEBHOOK_SECRET`
   become credentials injected as environment variables; the rulebooks
   read them as `{{ APPROVAL_TOKEN }}`-style names, which `--env-vars`
   provides in a standalone run. In the controller the two webhook rulesets
   can be replaced by event streams, which authenticate at the platform.
6. **Documentation.** Both event sources carry `DOCUMENTATION` and
   `EXAMPLES` blocks (a unit test asserts every argument the code reads is
   an option). On the move, re-check them with the collection's doc
   tooling, add a README section for the EDA content, and a changelog
   fragment (quote entries that contain `key: value` text or the changelog
   sanity test fails).
7. **Tests.** Move `tests/unit/test_events_plugin.py` and
   `tests/unit/test_syslog_parse.py` with the `conftest.py` SDK stub to
   `tests/unit/plugins/event_source/`; add an asyncio test of each plugin's
   `main()` with a fake queue; run `ansible-test sanity` and `ansible-lint`
   as the collection's CI does. The replay rulebooks and
   `tests/unit/test_rulebooks.py` stay in this repository.
8. **Decision environment.** When the collection carries the plugins, the
   Containerfile and the builder definition install the released collection
   from `requirements.yml` instead of copying `extensions/` and
   `playbooks/`, and the `podman run` line drops `--source-dir`.
9. **Certification.** Red Hat's review of certified EDA content looks for
   asyncio sources with `main(queue, args)` that never block the event loop
   (the pollers run SDK calls in a thread pool), clean shutdown on
   cancellation, documented arguments, rulebook examples under
   `extensions/eda/rulebooks`, unit tests, and a published decision
   environment definition. Confirm the current list against the partner
   guide before submitting. Automation Hub judges the built artifact, so
   check that `build_ignore` does not drop `extensions/`.
