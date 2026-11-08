# Contributing

This is a prototype maintained in the open. Issues and pull requests are
welcome, especially rules you wish existed and reports of what a real array
did that the code did not expect. Bugs in the `everpure.flasharray` modules
themselves go to https://github.com/Everpure-Ansible/FlashArray-Collection,
not here.

## Before you push

```
python3 -m venv venv && . venv/bin/activate
make deps      # Python requirements and the two collections
make check     # black, pyflakes, yamllint, ao_lint, unit tests, playbook syntax
```

`make check` is what CI runs; black and pyflakes cover the plugins,
`tools/`, `slack/` and the tests. The unit tests need no array: the
plugins are exercised with a fake SDK client, the orchestrator sync tool
and the Slack bot run against in-process fakes, and the rulebook tests
validate every
rulebook against ansible-rulebook's own schema, check that every lowercase
variable a rulebook references is documented in `vars/example.yml`
(uppercase names such as `APPROVAL_TOKEN` are environment variables passed
with `--env-vars`), that every playbook a rule runs exists, and that the
replay copies under `tests/` are rule-for-rule identical to the real
rulebooks, throttles included.

## Conventions

- Rulebooks decide, plugins observe. A source plugin emits facts (including
  the booleans and counters rules need, because rulebook conditions cannot
  do arithmetic or use filters); policy lives in the rulebook.
- ansible-rulebook facts to respect, not to fix (the full list is in
  [docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md)): conditions allow no
  Jinja filters or arithmetic (actions do); `throttle.once_within` must be a
  literal and cannot sit on a multi-event condition; `--vars` is one
  untemplated file; `localhost` must be in the inventory; `run_playbook`
  copies the playbook's whole directory, so relative includes within
  `playbooks/` work and files outside it do not.
- An action that changes an array is gated by what its event carries:
  `maintenance_active` where the event has it (alert, audit, hardware and
  replication events; config and telemetry events do not), `automation_users`
  on audit events so the automation never reacts to itself, and a per-site
  switch that falls back to notify on the QoS cap, the preferred-array move
  and replicate-now. A new action states which of the three it has, and
  README.md's gating paragraph says so.
- Every record a playbook appends to the alert log carries `time`, `kind`,
  `severity` and `array`.
- State that must survive a rulebook restart lives on the array (tags in the
  `eda` namespace) or in a file, never in a rulebook fact.
- Keep lab specifics out of committed files. `vars/local*.yml`,
  `inventory/local*.yml`, `baseline/*.yml` (except `example.yml`) and
  `*.env` are gitignored for that; no host names, addresses or
  home-directory paths from a lab belong anywhere in the tree.
- Changes that touch an array should be validated against one; say in the
  pull request what ran and what the array did.
- Every `.py` and `.sh` file starts (after the shebang, if any) with
  `# Copyright 2026 Everpure, Inc.` and
  `# SPDX-License-Identifier: Apache-2.0`.
