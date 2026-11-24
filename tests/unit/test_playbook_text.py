# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The records the playbooks build keep the text a rule hands them as text.

Statically: every field of a record that is not the playbook's own is one
expression ending in `| string` (a count ends in `| int`, a list is taken
only when it is a list), none of them reads a play or task var, and the
local fallback of orchestrator_post.yml hands the event's fields on the same
way. By execution (skipped when ansible-playbook is not on the PATH): text
shaped like a list or a dict is recorded as sent, a null field as empty
text, and an array name or a request id with a trailing line break is
refused."""

import json
import os
import pathlib
import re
import shutil
import subprocess

import jinja2
import pytest
import yaml
from jinja2 import meta, nodes

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAYBOOKS = ROOT / "playbooks"
INVENTORY = ROOT / "inventory" / "example.yml"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
JINJA = jinja2.Environment()
MARKERS = ("{{", "{%", "{#", "#jinja2")

needs_ansible = pytest.mark.skipif(
    ANSIBLE_PLAYBOOK is None, reason="ansible-playbook is not on PATH"
)

# playbook, task, fact, the fields the playbook sets from its own values
RECORDS = [
    ("notify.yml", "Build the alert record", "alert_record", ("time", "kind")),
    (
        "alert_runbook.yml",
        "Build the record",
        "alert_record",
        ("time", "kind", "runbook_note", "route"),
    ),
    (
        "capacity_request.yml",
        "Build the request",
        "capacity_request",
        ("time", "kind", "severity", "status", "candidates", "proposed_actions"),
    ),
]
COUNTS = ("pending_eradication_volumes", "pending_eradication_snapshots")
LISTS = ("top_growers", "candidate_targets")
# left out of the notify record when empty
OMITTED = ("alert_id", "alert_code")


def _tasks(tasks):
    for task in tasks or []:
        yield task
        for part in ("block", "rescue", "always"):
            yield from _tasks(task.get(part))


def _plays(playbook):
    return yaml.safe_load((PLAYBOOKS / playbook).read_text())


def _task(playbook, name):
    """The named task and its play."""
    for play in _plays(playbook):
        for task in _tasks(play.get("tasks")):
            if task.get("name") == name:
                return task, play
    raise AssertionError(f"{playbook}: no task named {name}")


def _root(text):
    """The expression of a value that is one `{{ ... }}` and nothing else."""
    tree = JINJA.parse(text.strip())
    if len(tree.body) == 1 and isinstance(tree.body[0], nodes.Output):
        parts = tree.body[0].nodes
        if len(parts) == 1 and not isinstance(parts[0], nodes.TemplateData):
            return parts[0]
    return None


def _is_filter(node, name):
    return isinstance(node, nodes.Filter) and node.name == name


def _is_a_list_or_nothing(text):
    """`x if (x is ... not string ... not mapping ... iterable) else []`."""
    root = _root(text)
    tests = {t.name for t in root.find_all(nodes.Test)} if root else set()
    return (
        isinstance(root, nodes.CondExpr)
        and isinstance(root.expr2, nodes.List)
        and not root.expr2.items
        and {"defined", "string", "mapping", "iterable"} <= tests
        and all(t.node == root.expr1 for t in root.test.find_all(nodes.Test))
        and " is not string" in text
        and " is not mapping" in text
    )


@pytest.mark.parametrize(
    "playbook, task, fact, own", RECORDS, ids=lambda v: str(v)[:24]
)
def test_every_event_field_of_a_record_is_kept_as_text(playbook, task, fact, own):
    """Deny by default: a field added to a record later is held to the rule
    unless it is listed as the playbook's own."""
    found, play = _task(playbook, task)
    record = found["ansible.builtin.set_fact"][fact]
    assert set(own) <= set(record), f"{playbook}: {set(own) - set(record)}"
    lazy = set(play.get("vars") or {}) | set(found.get("vars") or {})
    for field, text in record.items():
        if field in own:
            continue
        where = f"{playbook}: {field}"
        root = _root(text)
        if field in COUNTS:
            assert _is_filter(root, "int"), where
        elif field in LISTS:
            assert _is_a_list_or_nothing(text), where
        elif playbook == "notify.yml" and field in OMITTED:
            assert isinstance(root, nodes.CondExpr), where
            assert isinstance(root.expr1, nodes.Name), where
            assert root.expr1.name == "omit" and _is_filter(root.expr2, "string"), where
        else:
            assert _is_filter(root, "string"), where
        read = meta.find_undeclared_variables(JINJA.parse(text))
        assert not read & lazy, f"{where} reads a play or task var: {read & lazy}"


def test_the_fallback_hands_each_event_field_on_as_text_a_count_or_a_list():
    imported = next(
        p
        for p in _plays("orchestrator_post.yml")
        if p.get("import_playbook") == "capacity_request.yml"
    )
    assert imported["vars"], "the fallback passes no vars"
    for name, text in imported["vars"].items():
        if name in COUNTS:
            assert _is_filter(_root(text), "int"), name
        elif name in LISTS:
            assert _is_a_list_or_nothing(text), name
        else:
            assert _is_filter(_root(text), "string"), name


@pytest.mark.parametrize(
    "task, fact",
    [
        ("Keep the token and the token outcome", "token_reason"),
        ("Keep the post outcome (status and execution id only)", "post_reason"),
    ],
)
# the pattern of the playbook's own regex_replace is an escape Jinja warns about when it parses the text
@pytest.mark.filterwarnings("ignore:invalid escape sequence")
def test_the_reason_an_orchestrator_gives_is_kept_as_text(task, fact):
    found, _ = _task("orchestrator_post.yml", task)
    assert _is_filter(_root(found["ansible.builtin.set_fact"][fact]), "string")


def test_the_ticket_key_join_completes_no_marker(plain_text_filter):
    plain = plain_text_filter.plain_text
    found, _ = _task("alert_runbook.yml", "Build the record")
    template = JINJA.from_string(
        found["ansible.builtin.set_fact"]["alert_record"]["ticket_key"]
    )
    for array in ("#jinja2", "#JINJA2", "#", "#jin", "{", "x{", "array-a"):
        for alert_id in ("{ x }}", "% x %}", "# x #}", "jinja2:x", "2:x", "", 42):
            key = template.render(
                target_array=plain(array), alert_id=plain(alert_id), transition="opened"
            )
            for marker in MARKERS:
                assert marker not in key.lower(), (array, alert_id)


def test_the_slack_message_keeps_a_space_for_a_carriage_return():
    found, _ = _task("notify.yml", "Format the Slack message")
    env = jinja2.Environment(undefined=jinja2.Undefined)
    env.filters["regex_replace"] = lambda value, pattern, repl: re.sub(
        pattern, repl, value
    )
    out = env.from_string(found["ansible.builtin.set_fact"]["slack_text"]).render(
        slack_icons={},
        alert_record={
            "severity": "warning",
            "subject": "a subject",
            "array": "array-a",
            "detail": "line one\rline two",
        },
    )
    assert out.endswith("line one line two")


def _run(tmp_path, playbook, extra_vars):
    """Run a playbook once with a temporary alert log and request directory;
    return (rc, alert-log records, output)."""
    alert_log = tmp_path / "alerts.log"
    extra = tmp_path / "extra_vars.json"
    extra.write_text(
        json.dumps(
            {
                "alert_log": str(alert_log),
                "request_dir": str(tmp_path / "requests"),
                **extra_vars,
            }
        )
    )
    env = dict(os.environ)
    for name in (
        "ANSIBLE_CONFIG",
        "SLACK_WEBHOOK_URL",
        "SLACK_MIN_SEVERITY",
        "AAP_TOKEN",
    ):
        env.pop(name, None)
    env.update(
        {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "PYTHONWARNINGS": "ignore",
        }
    )
    proc = subprocess.run(
        [
            ANSIBLE_PLAYBOOK,
            "-i",
            str(INVENTORY),
            "-e",
            f"@{extra}",
            str(PLAYBOOKS / playbook),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    records = []
    if alert_log.exists():
        records = [json.loads(line) for line in alert_log.read_text().splitlines()]
    return proc.returncode, records, proc.stdout + proc.stderr


@needs_ansible
def test_notify_records_text_shaped_like_a_list_as_sent(tmp_path):
    rc, records, output = _run(
        tmp_path,
        "notify.yml",
        {
            "severity": "warning",
            "target_array": "array-a",
            "subject": "['a' 'b']",
            "detail": " {'a': 1} ",
            "alert_id": 42,
            "alert_code": "007",
        },
    )
    assert rc == 0, output
    assert len(records) == 1, records
    record = records[0]
    assert record["subject"] == "['a' 'b']"
    assert record["detail"] == "{'a': 1}"
    assert record["alert_id"] == "42" and record["alert_code"] == "007"
    assert record["severity"] == "warning" and record["array"] == "array-a"


@needs_ansible
def test_notify_records_a_null_field_as_empty_text(tmp_path):
    rc, records, output = _run(
        tmp_path,
        "notify.yml",
        {
            "severity": None,
            "target_array": None,
            "subject": None,
            "detail": None,
            "alert_id": None,
            "alert_code": None,
        },
    )
    assert rc == 0, output
    assert len(records) == 1, records
    record = records[0]
    assert record["severity"] == "warning"
    assert record["array"] == "" and record["subject"] == "" and record["detail"] == ""
    assert "alert_id" not in record and "alert_code" not in record


@needs_ansible
def test_the_alert_runbook_records_its_fields_as_text(tmp_path):
    rc, records, output = _run(
        tmp_path,
        "alert_runbook.yml",
        {
            "alert_runbooks": {"default": {"route": "ticket", "note": "a note"}},
            "severity": "warning",
            "target_array": "array-a",
            "alert_id": 42,
            "alert_code": 7,
            "subject": "['a' 'b']",
            "component": "{'a': 1}",
            "array_action": None,
            "knowledge_base_url": None,
        },
    )
    assert rc == 0, output
    assert len(records) == 1, records
    record = records[0]
    assert record["kind"] == "ticket" and record["route"] == "ticket"
    assert record["ticket_key"] == "array-a:42:opened"
    assert record["alert_id"] == "42" and record["alert_code"] == "7"
    assert record["subject"] == "['a' 'b']" and record["component"] == "{'a': 1}"
    assert record["array_action"] == "" and record["knowledge_base_url"] == ""


@needs_ansible
def test_the_alert_runbook_records_a_null_field_as_empty_text(tmp_path):
    rc, records, output = _run(
        tmp_path,
        "alert_runbook.yml",
        {
            "alert_runbooks": {"default": {"route": "ticket"}},
            "severity": None,
            "transition": None,
            "target_array": None,
            "alert_id": None,
            "alert_code": None,
            "subject": None,
            "component": None,
            "array_action": None,
            "knowledge_base_url": None,
        },
    )
    assert rc == 0, output
    assert len(records) == 1, records
    record = records[0]
    assert record["ticket_key"] == "::opened" and record["transition"] == "opened"
    for field in (
        "severity",
        "array",
        "alert_id",
        "alert_code",
        "subject",
        "component",
        "array_action",
        "knowledge_base_url",
    ):
        assert record[field] == "", field


RUNBOOK_VARS = {
    "alert_runbooks": {"default": {"route": "ticket"}},
    "severity": "warning",
    "target_array": "array-z",
    "alert_id": "10042",
    "alert_code": "25",
    "subject": "a subject",
}


@needs_ansible
def test_the_alert_runbook_keys_a_ticket_on_array_alert_and_transition(tmp_path):
    rc, records, output = _run(tmp_path, "alert_runbook.yml", RUNBOOK_VARS)
    assert rc == 0, output
    assert len(records) == 1, records
    assert records[0]["ticket_key"] == "array-z:10042:opened"
    assert records[0]["severity"] == "warning"


@needs_ansible
@pytest.mark.parametrize(
    "missing", ["target_array", "alert_id", "alert_code", "severity", "subject"]
)
def test_the_alert_runbook_fails_without_a_field_it_records(tmp_path, missing):
    extra_vars = {k: v for k, v in RUNBOOK_VARS.items() if k != missing}
    rc, records, output = _run(tmp_path, "alert_runbook.yml", extra_vars)
    assert rc != 0, output
    assert "undefined" in output
    assert records == []


@needs_ansible
def test_the_capacity_request_records_text_counts_and_lists(tmp_path):
    """An array that is not in the inventory: nothing is read from an array.
    A list sent as text is no list, and a count sent as text is a number."""
    rc, records, output = _run(
        tmp_path,
        "capacity_request.yml",
        {
            "at_risk_array": "array-z",
            "request_key": "2026-11-05T10:00:00Z",
            "used_pct": 91.2,
            "used_bytes": "45600000000000",
            "capacity_bytes": 50000000000000,
            "days_to_full": None,
            "top_growers": "['a' 'b']",
            "candidate_targets": "{'a': 1}",
            "pending_eradication_volumes": "3",
            "pending_eradication_snapshots": None,
        },
    )
    assert rc == 0, output
    assert len(records) == 1, records
    record = records[0]
    assert record["status"] == "awaiting_approval"
    assert record["request_id"] == "array-z-20261105T100000Z"
    assert record["array"] == "array-z"
    assert record["used_pct"] == "91.2"
    assert record["used_bytes"] == "45600000000000"
    assert record["capacity_bytes"] == "50000000000000"
    assert record["days_to_full"] == ""
    assert record["top_growers"] == [] and record["candidate_targets"] == []
    assert int(record["pending_eradication_volumes"]) == 3
    assert int(record["pending_eradication_snapshots"]) == 0
    assert record["candidates"] == {"volumes": [], "snapshots": []}
    assert record["candidates_error"] == "array-z is not in the inventory"
    written = tmp_path / "requests" / "array-z-20261105T100000Z.json"
    assert json.loads(written.read_text()) == record


@needs_ansible
def test_the_capacity_request_keeps_a_list_that_is_a_list(tmp_path):
    growers = [{"volume": "vol-db-01", "growth_bytes": 120000000000}]
    targets = [{"array": "array-b", "used_pct": 40.5}]
    rc, records, output = _run(
        tmp_path,
        "capacity_request.yml",
        {
            "at_risk_array": "array-z",
            "used_pct": "91.2",
            "days_to_full": 12,
            "top_growers": growers,
            "candidate_targets": targets,
            "pending_eradication_volumes": 1,
            "pending_eradication_snapshots": 3,
        },
    )
    assert rc == 0, output
    assert len(records) == 1, records
    record = records[0]
    assert record["used_pct"] == "91.2" and record["days_to_full"] == "12"
    assert record["used_bytes"] == "" and record["capacity_bytes"] == ""
    assert record["top_growers"] == growers
    assert record["candidate_targets"] == targets
    assert int(record["pending_eradication_volumes"]) == 1
    assert int(record["pending_eradication_snapshots"]) == 3
    assert "['array-b']" in output


@needs_ansible
def test_the_capacity_request_refuses_a_name_with_a_trailing_line_break(tmp_path):
    rc, records, output = _run(
        tmp_path, "capacity_request.yml", {"at_risk_array": "array-z\n"}
    )
    assert rc != 0, output
    assert "is not a valid array name" in output
    assert records == []
    assert not (tmp_path / "requests").exists()


# event text a playbook stores outside a record: playbook, task, path to the value
PINNED = [
    ("capacity_request.yml", "Pin the request id and time", ("request_id",)),
    (
        "capacity_request.yml",
        "Reuse the open request when there is one",
        ("request_id",),
    ),
    ("orchestrator_post.yml", "Build the envelope", ("envelope", "event", "array")),
    (
        "replicate_now.yml",
        "Pin the snapshot suffix and decide whether a snapshot is still wanted",
        ("snap_suffix",),
    ),
]


@pytest.mark.parametrize("playbook, task, path", PINNED, ids=lambda v: str(v)[:24])
def test_event_text_stored_outside_a_record_is_kept_as_text(playbook, task, path):
    found, _ = _task(playbook, task)
    value = found["ansible.builtin.set_fact"]
    for key in path:
        value = value[key]
    assert _is_filter(_root(value), "string"), f"{playbook}: {'.'.join(path)}"


FULL_NAME = "is regex('[A-Za-z0-9][A-Za-z0-9._-]*', match_type='fullmatch')"


@pytest.mark.parametrize(
    "playbook, guards", [("capacity_request.yml", 1), ("capacity_reclaim.yml", 2)]
)
def test_the_capacity_name_guards_match_the_whole_name(playbook, guards):
    text = (PLAYBOOKS / playbook).read_text()
    assert text.count(FULL_NAME) == guards
    assert "is match(" not in text


REBUILD = {
    "reclaim_action": "eradicate_pending",
    "rebuild_from_artifacts": True,
    "candidates": {"volumes": [], "snapshots": []},
}


@needs_ansible
@pytest.mark.parametrize(
    "extra_vars, refusal",
    [
        (
            {"request_id": "array-z-1\n", "array": "array-z", **REBUILD},
            "is not a valid request id",
        ),
        (
            {"request_id": "array-z-1", "array": "array-z\n", **REBUILD},
            "are not both set",
        ),
        (
            {"request_id": "array-z-1", "at_risk_array": "array-z\n", **REBUILD},
            "are not both set",
        ),
    ],
    ids=["request_id", "array", "at_risk_array"],
)
def test_the_capacity_reclaim_refuses_a_name_with_a_trailing_line_break(
    tmp_path, extra_vars, refusal
):
    """The rebuild path, so a name that passed would be written to a file."""
    rc, records, output = _run(tmp_path, "capacity_reclaim.yml", extra_vars)
    assert rc != 0, output
    assert refusal in output
    assert records == []
    assert not (tmp_path / "requests").exists()
