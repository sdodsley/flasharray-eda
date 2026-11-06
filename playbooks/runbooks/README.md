# Per-code runbook tasks

`playbooks/alert_runbook.yml` includes `runbooks/<alert code>.yml` relative
to itself (`{{ playbook_dir }}/runbooks/`; ansible-rulebook copies the whole
`playbooks/` directory into its temporary project, so the relative path
resolves) when the code's entry in `alert_runbooks` has `route: tasks`. Each
file is a task list that runs on localhost with the alert fields in scope:
`target_array`, `alert_id`, `alert_code`, `component`, `array_action`,
`knowledge_base_url`, `subject`, `severity`. Delegate to
`{{ target_array }}` for collection modules. `9999.yml` is the fixture used
by the tests.
