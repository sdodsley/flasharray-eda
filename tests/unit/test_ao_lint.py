# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for tools/ao_lint.py and the orchestrator definitions: the shipped
definitions pass with the allowed template list, the template list matches
the definitions and the platform rulebooks, no per-environment id has been
committed, every rule has a fixture under tests/fixtures/orchestrator/ that
trips it, and the CLI exits and prints as documented."""

import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
LINTER = ROOT / "tools" / "ao_lint.py"
DEFINITIONS = sorted((ROOT / "orchestrator").glob("*.json"))
DAY2_DEFINITIONS = [p for p in DEFINITIONS if p.name.startswith("day2_")]
TEMPLATES = ROOT / "orchestrator" / "job_templates.txt"
PLATFORM_RULEBOOKS = ROOT / "extensions" / "eda" / "rulebooks" / "platform"
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "orchestrator").glob("bad_*.json"))
PLACEHOLDER = "REPLACE_AT_SYNC"
TRIGGER_FIELD = re.compile(r"\$\{trigger\.([A-Za-z_][A-Za-z0-9_]*)")
# a problem line is file:node:rule:message; the file part may hold no colon
# because the CLI is given relative POSIX paths
PROBLEM_LINE = re.compile(
    r"^(?P<file>[^:]+):(?P<node>[^:]*):(?P<rule>[a-z_]+):(?P<message>.+)$"
)


def _load_linter():
    spec = importlib.util.spec_from_file_location("ao_lint", LINTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ao_lint():
    return _load_linter()


@pytest.fixture(scope="module")
def allowed(ao_lint):
    return ao_lint.load_allowed_templates(TEMPLATES)


def _rule_of(fixture):
    """bad_<rule>.json or bad_<rule>__<variant>.json -> rule."""
    return fixture.stem[len("bad_") :].split("__")[0]


def _job_template_names(doc):
    return {
        node["parameters"]["job_template_name"]
        for node in doc["nodes"]
        if node["type"] == "aap_job_template"
    }


def _platform_job_template_names():
    """Every run_job_template name in the generated platform rulebooks."""
    names = set()
    for path in sorted(PLATFORM_RULEBOOKS.glob("*.yml")):
        for ruleset in yaml.safe_load(path.read_text()) or []:
            for rule in ruleset.get("rules", []):
                actions = rule.get("actions") or [rule.get("action", {})]
                for action in actions:
                    job = action.get("run_job_template")
                    if job:
                        names.add(job["name"])
    return names


def test_there_are_eight_shipped_definitions():
    assert [p.name for p in DEFINITIONS] == [
        "capacity_reclaim_approval.json",
        "day2_expand_volume.json",
        "day2_identify_led.json",
        "day2_protect_now.json",
        "day2_qos_cap.json",
        "dr_drill_schedule.json",
        "dr_failover_test.json",
        "replication_overdue_approval.json",
    ]


@pytest.mark.parametrize("path", DEFINITIONS, ids=lambda p: p.name)
def test_shipped_definition_passes(path, ao_lint, allowed):
    problems = ao_lint.lint_file(path, allowed)
    assert problems == [], [ao_lint.format_problem(p) for p in problems]


@pytest.mark.parametrize("path", DEFINITIONS, ids=lambda p: p.name)
def test_shipped_definition_is_ascii_and_pretty(path):
    raw = path.read_bytes()
    raw.decode("ascii")
    assert raw.endswith(b"}\n")
    doc = json.loads(raw)
    assert doc["schema_version"] == "2.0.0"


def test_job_templates_txt_matches_the_definitions(allowed):
    """The definitions and the platform rulebooks name only listed templates,
    and every listed template is named by one of them. The reverse check
    needs the generated platform rulebooks; without that directory only the
    definitions' side is checked."""
    used = set()
    for path in DEFINITIONS:
        used |= _job_template_names(json.loads(path.read_text()))
    assert used <= allowed, f"used but not listed: {used - allowed}"
    if PLATFORM_RULEBOOKS.is_dir():
        used |= _platform_job_template_names()
        assert used == allowed, (
            f"used but not listed: {used - allowed}; "
            f"listed but unused: {allowed - used}"
        )


def _first_job_node(doc):
    """The first node on the path from the trigger that runs a template
    other than notify: the node that does the work the form asked for."""
    nodes = {node["id"]: node for node in doc["nodes"]}
    (trigger_id,) = [t["id"] for t in doc["triggers"]]
    queue = [trigger_id]
    seen = set()
    while queue:
        current = queue.pop(0)
        node = nodes.get(current)
        if (
            node is not None
            and node["type"] == "aap_job_template"
            and node["parameters"]["job_template_name"] != "FlashArray - notify"
        ):
            return node
        for edge in doc["edges"]:
            if edge["from"] == current and edge["to"] not in seen:
                seen.add(edge["to"])
                queue.append(edge["to"])
    raise AssertionError("no job node other than notify")


@pytest.mark.parametrize("path", DAY2_DEFINITIONS, ids=lambda p: p.name)
def test_day2_form_declares_exactly_what_its_job_node_reads(path):
    """Each day-2 form (manual trigger) declares, and requires, exactly the
    inputs its job node passes to the playbook or uses to pick the array's
    credential: an input nobody reads is noise on the form, and a reference
    the form does not declare is a run-time failure the linter also flags."""
    doc = json.loads(path.read_text())
    schema = doc["triggers"][0]["parameters"]["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    node = _first_job_node(doc)
    params = node["parameters"]
    read = set(TRIGGER_FIELD.findall(json.dumps(params["extra_vars"])))
    read |= set(TRIGGER_FIELD.findall(json.dumps(params.get("credential_names", []))))
    assert read == set(schema["properties"]), node["id"]
    assert params["credential_names"] == ["FlashArray - ${trigger.array}"]
    assert params["extra_vars"]["target_array"] == "${trigger.array}"
    assert "alert_log" not in params["extra_vars"]
    # a boolean or integer input is passed whole, so it keeps its type
    for name, prop in schema["properties"].items():
        if prop["type"] in ("boolean", "integer"):
            assert "${trigger." + name + "}" in params["extra_vars"].values(), name


ARTIFACT_REF = re.compile(
    r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\.artifacts\.([A-Za-z_][A-Za-z0-9_]*)"
)
DATA_KEY = re.compile(r"'([a-z][a-z0-9_]*)':")


class _AnyTagLoader(yaml.SafeLoader):
    """safe_load that keeps the text of Ansible-only tags such as !unsafe."""


def _any_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_AnyTagLoader.add_multi_constructor("!", _any_tag)


def _flatten(obj):
    if isinstance(obj, dict):
        yield obj
        for key in ("tasks", "pre_tasks", "post_tasks", "block", "always", "rescue"):
            for item in obj.get(key) or []:
                yield from _flatten(item)
    elif isinstance(obj, list):
        for item in obj:
            yield from _flatten(item)


def _published_keys(playbook):
    """Keys of every set_stats data dict in the playbook and in the task files
    under playbooks/ it names (dr_drill.yml publishes from dr_drill_one.yml)."""
    text = (ROOT / playbook).read_text()
    files = [ROOT / playbook] + [
        ROOT / "playbooks" / name
        for name in sorted(set(re.findall(r"([a-z_]+\.yml)", text)))
        if (ROOT / "playbooks" / name).exists() and name != pathlib.Path(playbook).name
    ]
    keys = set()
    for path in files:
        for item in _flatten(yaml.load(path.read_text(), Loader=_AnyTagLoader)):
            if "ansible.builtin.set_stats" in item:
                keys |= set(
                    DATA_KEY.findall(
                        str(item["ansible.builtin.set_stats"].get("data", ""))
                    )
                )
    return keys


def test_artifacts_a_definition_reads_are_published_by_the_playbook():
    """`${<node>.artifacts.<key>}` works only when the node's job template's
    playbook publishes <key> with set_stats; a missing key fails the node at
    run time, which the orchestrator's validator cannot know."""
    templates = {}

    def walk(obj):
        if isinstance(obj, dict):
            if (
                str(obj.get("name", "")).startswith("FlashArray - ")
                and "playbook" in obj
            ):
                templates[obj["name"]] = obj["playbook"]
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(yaml.load((ROOT / "aap" / "seed.yml").read_text(), Loader=_AnyTagLoader))
    assert templates, "no job templates found in aap/seed.yml"
    checked = 0
    for path in DEFINITIONS:
        text = path.read_text()
        doc = json.loads(text)
        node_template = {
            n["id"]: n["parameters"].get("job_template_name")
            for n in doc["nodes"]
            if n["type"] == "aap_job_template"
        }
        for node_id, key in sorted(set(ARTIFACT_REF.findall(text))):
            assert (
                node_id in node_template
            ), f"{path.name}: {node_id} is not a job template node"
            playbook = templates[node_template[node_id]]
            assert key in _published_keys(
                playbook
            ), f"{path.name}: {node_id}.artifacts.{key} is not published by {playbook}"
            checked += 1
    assert checked >= 10


@pytest.mark.parametrize("path", DEFINITIONS, ids=lambda p: p.name)
def test_only_placeholders_for_per_environment_ids(path):
    doc = json.loads(path.read_text())
    for node in doc["nodes"]:
        params = node["parameters"]
        if node["type"] == "aap_job_template":
            assert params["credential_id"] == PLACEHOLDER, node["id"]
            assert params["organization_name"] == "FlashArray", node["id"]
        if node["type"] == "approval":
            # approvers are named by group name, not id: the built-in group
            assert params["approver_groups"] == ["admins"], node["id"]
            # 24 hours, or one hour for the time-sensitive replication route
            assert params["decision_window"] in (86400, 3600), node["id"]
    for trigger in doc["triggers"]:
        accounts = trigger["parameters"].get("authorized_service_account_ids")
        if accounts is not None:
            assert accounts == [PLACEHOLDER], trigger["id"]


def test_capacity_definition_feeds_the_request_playbook():
    """The request node passes exactly the event-derived variables
    playbooks/capacity_request.yml reads; paths come from the template."""
    doc = json.loads(
        (ROOT / "orchestrator" / "capacity_reclaim_approval.json").read_text()
    )
    nodes = {node["id"]: node for node in doc["nodes"]}
    extra = nodes["capacity_request"]["parameters"]["extra_vars"]
    assert extra["at_risk_array"] == "${trigger.event.array}"
    assert set(extra) == {
        "at_risk_array",
        "used_pct",
        "used_bytes",
        "capacity_bytes",
        "days_to_full",
        "top_growers",
        "pending_eradication_volumes",
        "pending_eradication_snapshots",
        "candidate_targets",
    }
    assert "request_dir" not in extra and "alert_log" not in extra
    reclaim = nodes["capacity_reclaim"]["parameters"]["extra_vars"]
    assert reclaim["request_id"] == "${capacity_request.artifacts.request_id}"
    assert reclaim["candidates"] == "${capacity_request.artifacts.candidates}"
    assert reclaim["array"] == "${trigger.event.array}"
    assert reclaim["reclaim_action"] == "eradicate_pending"
    assert doc["triggers"][0]["parameters"]["webhook_path"] == "fa_capacity"
    # both array-touching nodes run with the event's array's own credential
    for node_id in ("capacity_request", "capacity_reclaim"):
        assert nodes[node_id]["parameters"]["credential_names"] == [
            "FlashArray - ${trigger.event.array}"
        ], node_id


SIZE_SAMPLES = {
    "2T": True,
    "500G": True,
    "1024": True,
    "1.5T": False,
    "2TB": False,
    "0x10": False,
}


def _playbook_size_regex():
    text = (ROOT / "playbooks" / "expand_volume.yml").read_text()
    match = re.search(r"new_size \| string is match\('([^']+)'\)", text)
    assert match, "playbooks/expand_volume.yml does not assert on new_size"
    return match.group(1)


def test_expand_form_and_playbook_agree_on_sizes():
    """The manual trigger's form must not accept a size the playbook refuses
    (purefa_volume parses 1.5T and 2TB as zero) nor refuse one it takes."""
    doc = json.loads((ROOT / "orchestrator" / "day2_expand_volume.json").read_text())
    schema = doc["triggers"][0]["parameters"]["input_schema"]["properties"]["new_size"]
    assert schema["maxLength"] == 16
    form = re.compile(schema["pattern"])
    playbook = re.compile(_playbook_size_regex())
    for sample, accepted in SIZE_SAMPLES.items():
        assert bool(form.fullmatch(sample)) is accepted, f"form: {sample}"
        assert bool(playbook.fullmatch(sample)) is accepted, f"playbook: {sample}"


def test_expand_node_uses_the_requested_arrays_credential():
    """The form accepts any array name, so the expand node must run with
    that array's credential (as the capacity nodes do with the event's);
    with the template's default credential a request for another array
    would be approved and then fail expand_volume.yml's credential_array
    guard, with no notify on the approved branch."""
    doc = json.loads((ROOT / "orchestrator" / "day2_expand_volume.json").read_text())
    nodes = {node["id"]: node for node in doc["nodes"]}
    params = nodes["expand_volume"]["parameters"]
    assert params["credential_names"] == ["FlashArray - ${trigger.array}"]
    assert params["extra_vars"]["target_array"] == "${trigger.array}"
    # the notify nodes need no array credential
    for node_id, node in nodes.items():
        if node_id != "expand_volume" and node["type"] == "aap_job_template":
            assert "credential_names" not in node["parameters"], node_id


def test_trigger_id_is_an_alias_of_trigger(ao_lint):
    """The engine registers the trigger output under trigger and under the
    trigger's id, so ${<trigger id>.array} resolves like ${trigger.array}
    and is checked the same way."""
    doc = json.loads((ROOT / "orchestrator" / "day2_expand_volume.json").read_text())
    assert ao_lint.lint_definition(doc) == []
    trigger_id = doc["triggers"][0]["id"]
    node = next(n for n in doc["nodes"] if n["id"] == "notify_requested")
    node["parameters"]["extra_vars"]["target_array"] = "${" + trigger_id + ".array}"
    assert ao_lint.lint_definition(doc) == []
    node["parameters"]["extra_vars"]["target_array"] = (
        "${" + trigger_id + ".payload.array}"
    )
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [("notify_requested", "trigger_ref")]
    assert "array, new_size, volume" in problems[0].message
    node["parameters"]["extra_vars"]["target_array"] = "${no_such_trigger.array}"
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [
        ("notify_requested", "expression_ref")
    ]
    assert "node id, trigger id or namespace" in problems[0].message


def _definition(name):
    return json.loads((ROOT / "orchestrator" / name).read_text())


def _node(doc, node_id):
    return next(n for n in doc["nodes"] if n["id"] == node_id)


def test_trigger_refs_follow_the_trigger_type(ao_lint):
    """Manual inputs and webhook or EDA bodies are both read as
    ${trigger.<top-level key>} (the body is not wrapped), a schedule has
    scheduled_at and triggered_at; the wrapped form, a key the input_schema
    does not have and a payload on a schedule all resolve to nothing at run
    time and lint as trigger_ref."""
    doc = _definition("day2_expand_volume.json")
    extra = _node(doc, "notify_requested")["parameters"]["extra_vars"]
    extra["target_array"] = "${trigger.payload.event.array}"
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [("notify_requested", "trigger_ref")]
    assert "${trigger.payload.event.array}" in problems[0].message
    assert "array, new_size, volume" in problems[0].message

    doc = _definition("capacity_reclaim_approval.json")
    _node(doc, "capacity_reclaim")["parameters"]["extra_vars"][
        "array"
    ] = "${trigger.payload.event.array}"
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [("capacity_reclaim", "trigger_ref")]
    assert "input_schema has event (the body is not wrapped)" in problems[0].message
    # an EDA trigger without an input_schema takes any top-level key
    del doc["triggers"][0]["parameters"]["input_schema"]
    assert ao_lint.lint_definition(doc) == []

    doc = _definition("dr_drill_schedule.json")
    extra = _node(doc, "notify_summary")["parameters"]["extra_vars"]
    extra["detail"] = "ran at ${trigger.scheduled_at}; ${trigger.payload.x}"
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [("notify_summary", "trigger_ref")]
    assert "${trigger.payload.x}" in problems[0].message
    # a manual trigger without an input_schema takes any name
    doc = _definition("day2_expand_volume.json")
    doc["triggers"][0]["parameters"] = {}
    assert ao_lint.lint_definition(doc) == []


def test_parameter_keys_follow_the_node_schemas(ao_lint):
    """Every node schema has additionalProperties false, so a key it does
    not list (an old parameter name, the controller's names) is refused at
    import; the linter holds the types whose schemas it has read to their
    key sets, and a scheduled or webhook trigger to its required fields."""
    doc = _definition("capacity_reclaim_approval.json")
    _node(doc, "capacity_request")["parameters"]["credentials"] = ["x"]
    _node(doc, "approve_reclaim")["parameters"]["timeout"] = 60
    doc["triggers"][0]["parameters"]["secret"] = "x"
    problems = ao_lint.lint_definition(doc)
    assert sorted((p.node, p.rule) for p in problems) == [
        ("approve_reclaim", "parameter_keys"),
        ("capacity_request", "parameter_keys"),
        ("fa_capacity_event", "parameter_keys"),
    ]
    messages = {p.node: p.message for p in problems}
    assert "has no parameter credentials; it takes" in messages["capacity_request"]
    assert "credential_names" in messages["capacity_request"]
    assert "has no parameter timeout" in messages["approve_reclaim"]
    assert "decision_window" in messages["approve_reclaim"]
    # a type whose schema has not been read is not held to a key set
    doc = _definition("dr_drill_schedule.json")
    doc["nodes"].append(
        {"id": "pause", "type": "wait", "name": "Pause", "parameters": {"anything": 1}}
    )
    doc["edges"].append({"from": "notify_summary", "to": "pause"})
    assert ao_lint.lint_definition(doc) == []

    doc = _definition("dr_drill_schedule.json")
    params = doc["triggers"][0]["parameters"]
    del params["schedule_type"]
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [("nightly", "trigger_parameters")]
    assert "schedule_type" in problems[0].message
    params["schedule_type"] = "interval"
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [("nightly", "trigger_parameters")]
    assert "has no interval" in problems[0].message

    doc = _definition("capacity_reclaim_approval.json")
    doc["triggers"][0]["parameters"]["authorized_service_account_ids"] = []
    problems = ao_lint.lint_definition(doc)
    assert [(p.node, p.rule) for p in problems] == [
        ("fa_capacity_event", "trigger_parameters")
    ]
    assert "authorized_service_account_ids" in problems[0].message


def test_node_references_must_name_an_ancestor(ao_lint):
    """${x...} resolves only when x ran before this node: a reference to a
    parallel branch or to a later node is expression_ref."""
    doc = _definition("capacity_reclaim_approval.json")
    _node(doc, "notify_no_candidates")["parameters"]["extra_vars"][
        "detail"
    ] = "${approve_reclaim.status}"
    _node(doc, "notify_pending")["parameters"]["extra_vars"][
        "detail"
    ] = "${capacity_reclaim.artifacts.result}"
    problems = ao_lint.lint_definition(doc)
    assert sorted((p.node, p.rule) for p in problems) == [
        ("notify_no_candidates", "expression_ref"),
        ("notify_pending", "expression_ref"),
    ]
    for problem in problems:
        assert "does not run before this one" in problem.message


def test_unreachable_nodes_portless_edges_and_unparsable_conditions(ao_lint):
    base = json.dumps(_definition("dr_drill_schedule.json"))

    doc = json.loads(base)
    doc["nodes"].append(
        {"id": "orphan", "type": "wait", "name": "Orphan", "parameters": {"seconds": 1}}
    )
    assert [(p.node, p.rule) for p in ao_lint.lint_definition(doc)] == [
        ("orphan", "unreachable")
    ]

    doc = json.loads(base)
    doc["edges"][0]["from_port"] = "fired"  # leaves the trigger
    doc["edges"][1]["from_port"] = "success"  # leaves the job template node
    assert sorted((p.node, p.rule) for p in ao_lint.lint_definition(doc)) == [
        ("dr_drill", "edge_keys"),
        ("nightly", "edge_keys"),
    ]

    rejected = (
        "${dr_drill.artifacts.failed_count} >",
        "${dr_drill.artifacts.failed_count} > 0 && true",
        "len(${dr_drill.artifacts.drills}) > 0",
        "${dr_drill.artifacts.failed_count} + 1 > 1",
        "${dr_drill.artifacts.failed_count} if True else 0",
    )
    for condition in rejected:
        doc = json.loads(base)
        _node(doc, "drills_failed")["parameters"]["condition"] = condition
        problems = ao_lint.lint_definition(doc)
        assert [(p.node, p.rule) for p in problems] == [
            ("drills_failed", "condition_expression")
        ], condition
        assert condition in problems[0].message
    accepted = (
        "${dr_drill.artifacts.failed_count} > 0",
        "${dr_drill.artifacts.status} == 'failed' or not ${dr_drill.artifacts.ok}",
        "${dr_drill.artifacts.status} in ['failed', 'error'] and true",
        "${dr_drill.artifacts.failed_count}",
    )
    for condition in accepted:
        doc = json.loads(base)
        _node(doc, "drills_failed")["parameters"]["condition"] = condition
        assert ao_lint.lint_definition(doc) == [], condition


def test_template_nodes_need_credential_organization_and_a_template(ao_lint):
    """Both AAP node types are checked the same way, by name or by id."""

    def lint(ntype, params):
        doc = {
            "schema_version": "2.0.0",
            "name": "t",
            "description": "t",
            "triggers": [{"id": "start", "type": "manual_trigger", "parameters": {}}],
            "nodes": [
                {"id": "run", "type": ntype, "name": "Run", "parameters": params}
            ],
            "edges": [{"from": "start", "to": "run"}],
        }
        return sorted({p.rule for p in ao_lint.lint_definition(doc, {"ok"})})

    base = {"organization_name": "FlashArray", "credential_id": "REPLACE_AT_SYNC"}
    assert lint("aap_job_template", dict(base, job_template_name="ok")) == []
    assert lint("aap_job_template", dict(base, job_template_id=12)) == []
    assert lint("aap_job_template", dict(base, job_template_id="12")) == []
    assert (
        lint("aap_workflow_job_template", dict(base, workflow_job_template_name="ok"))
        == []
    )
    assert (
        lint("aap_workflow_job_template", dict(base, workflow_job_template_id=3)) == []
    )
    # the wrong family's key, a bool or zero id, or no template at all
    assert lint("aap_workflow_job_template", dict(base, job_template_name="ok")) == [
        "job_template"
    ]
    assert lint("aap_job_template", dict(base, job_template_id=True)) == [
        "job_template"
    ]
    assert lint("aap_job_template", dict(base, job_template_id=0)) == ["job_template"]
    assert lint("aap_job_template", base) == ["job_template"]
    # the allowed list applies to whichever name is present
    assert lint(
        "aap_workflow_job_template", dict(base, workflow_job_template_name="x")
    ) == ["job_template_allowed"]
    for missing in ("organization_name", "credential_id"):
        params = dict(base, job_template_name="ok")
        del params[missing]
        assert lint("aap_job_template", params) == ["job_template"], missing
        params[missing] = "  "
        assert lint("aap_job_template", params) == ["job_template"], missing


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_each_fixture_trips_its_rule(path, ao_lint, allowed):
    problems = ao_lint.lint_file(path, allowed)
    rules = {p.rule for p in problems}
    assert _rule_of(path) in rules, [ao_lint.format_problem(p) for p in problems]
    for problem in problems:
        assert problem.file == str(path)
        assert problem.rule in ao_lint.RULES
        assert problem.message


def test_every_rule_has_a_fixture(ao_lint):
    # cap_size needs a 1.5 MB file, which test_oversize_definition builds
    # instead of committing it
    covered = {_rule_of(p) for p in FIXTURES} | {"cap_size"}
    assert set(ao_lint.RULES) <= covered, sorted(set(ao_lint.RULES) - covered)
    assert covered <= set(ao_lint.RULES), sorted(covered - set(ao_lint.RULES))


def test_oversize_definition(tmp_path, ao_lint, allowed):
    doc = json.loads((ROOT / "orchestrator" / "dr_drill_schedule.json").read_text())
    doc["description"] = "x" * (ao_lint.MAX_BYTES + 1)
    path = tmp_path / "huge.json"
    path.write_text(json.dumps(doc))
    rules = {p.rule for p in ao_lint.lint_file(path, allowed)}
    assert rules == {"cap_size"}


def test_loop_body_returns_over_an_iterate_edge(ao_lint):
    """A loop body hangs off the 'iterate' port and returns over an edge with
    to_port 'iterate', the one back edge the orchestrator's validator treats as
    feedback; 'complete' continues. Anything else is still a cycle."""
    doc = json.loads((ROOT / "orchestrator" / "dr_drill_schedule.json").read_text())
    doc["nodes"] += [
        {
            "id": "each",
            "type": "loop",
            "name": "Each",
            "parameters": {"type": "for_each", "items": "${dr_drill.artifacts.drills}"},
        },
        {"id": "body", "type": "wait", "name": "Body", "parameters": {"seconds": 1}},
        {"id": "done", "type": "wait", "name": "Done", "parameters": {"seconds": 1}},
    ]
    doc["edges"] += [
        {"from": "notify_summary", "to": "each"},
        {"from": "each", "to": "body", "from_port": "iterate"},
        {"from": "body", "to": "each", "to_port": "iterate"},
        {"from": "each", "to": "done", "from_port": "complete"},
    ]
    assert ao_lint.lint_definition(doc) == []
    # without the to_port the return is a plain back edge: a cycle and a second input
    plain = json.loads(json.dumps(doc))
    del plain["edges"][-2]["to_port"]
    rules = {p.rule for p in ao_lint.lint_definition(plain)}
    assert rules == {"cycle", "single_input", "loop_ports"}
    # a to_port on an edge into anything but a loop node
    odd = json.loads(json.dumps(doc))
    odd["edges"][-1]["to_port"] = "iterate"
    assert {p.rule for p in ao_lint.lint_definition(odd)} == {"loop_ports"}
    # no complete edge, a return from outside the body, an unknown port
    broken = json.loads(json.dumps(doc))
    broken["edges"][-1]["from_port"] = "finished"
    broken["edges"].append({"from": "done", "to": "each", "to_port": "iterate"})
    messages = [
        p.message for p in ao_lint.lint_definition(broken) if p.rule == "loop_ports"
    ]
    assert len(messages) == 3 and any("'complete'" in m for m in messages)
    assert any("unknown port finished" in m for m in messages)
    assert any("'done'" in m for m in messages)
    # the loop node's parameter keys follow the schema
    odd_param = json.loads(json.dumps(doc))
    odd_param["nodes"][-3]["parameters"]["overlap"] = "skip"
    assert {p.rule for p in ao_lint.lint_definition(odd_param)} == {"parameter_keys"}


def test_problem_line_format(ao_lint):
    problem = ao_lint.Problem(
        "orchestrator/x.json",
        "gate",
        "approval_ports",
        "no edge leaves port 'rejected'",
    )
    assert (
        ao_lint.format_problem(problem)
        == "orchestrator/x.json:gate:approval_ports:no edge leaves port 'rejected'"
    )


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-I", "tools/ao_lint.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_passes_the_shipped_definitions():
    result = _cli(
        "orchestrator/*.json", "--allowed-templates", "orchestrator/job_templates.txt"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"ao_lint: {len(DEFINITIONS)} definition(s) ok"


def test_cli_reports_one_line_per_problem_and_fails():
    result = _cli(
        "tests/fixtures/orchestrator/bad_cycle.json",
        "tests/fixtures/orchestrator/bad_approval_ports.json",
    )
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines, result.stderr
    for line in lines:
        match = PROBLEM_LINE.match(line)
        assert match, line
        assert match["file"].startswith("tests/fixtures/orchestrator/bad_")
    assert {PROBLEM_LINE.match(line)["rule"] for line in lines} == {
        "cycle",
        "approval_ports",
    }
    assert "2 problem(s) in 2 definition(s)" in result.stderr


def test_cli_usage_errors(tmp_path):
    assert _cli("orchestrator/does_not_exist_*.json").returncode == 2
    assert (
        _cli(
            "orchestrator/dr_drill_schedule.json",
            "--allowed-templates",
            str(tmp_path / "none.txt"),
        ).returncode
        == 2
    )
    unreadable = tmp_path / "gone.json"
    result = _cli(str(unreadable))
    assert result.returncode == 1
    assert ":json:" in result.stdout


def test_allowed_templates_file_ignores_comments_and_blanks(tmp_path, ao_lint):
    listing = tmp_path / "templates.txt"
    listing.write_text("# comment\n\nFlashArray - notify\n  FlashArray - DR drill  \n")
    assert ao_lint.load_allowed_templates(listing) == {
        "FlashArray - notify",
        "FlashArray - DR drill",
    }


def test_linter_is_standard_library_only():
    text = LINTER.read_text()
    imported = set(re.findall(r"^(?:import|from)\s+([a-z_]+)", text, re.MULTILINE))
    assert imported <= {
        "argparse",
        "ast",
        "collections",
        "glob",
        "ipaddress",
        "json",
        "pathlib",
        "re",
        "sys",
        "urllib",
    }
    assert shutil.which(sys.executable)
