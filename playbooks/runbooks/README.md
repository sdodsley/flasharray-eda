# Per-code runbook tasks

`playbooks/alert_runbook.yml` includes `<runbook_dir>/<alert code>.yml` when
the code's entry in `alert_runbooks` has `route: tasks`. Each file is a task
list that runs on localhost with the alert fields in scope: `target_array`,
`alert_id`, `alert_code`, `component`, `array_action`, `knowledge_base_url`,
`subject`, `severity`. Delegate to `{{ target_array }}` for collection
modules. `9999.yml` is the fixture used by the tests.
