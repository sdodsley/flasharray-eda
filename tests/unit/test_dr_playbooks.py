# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Static checks over the two ActiveDR actuators of the DR failover test.
playbooks/dr_promote.yml and playbooks/dr_demote.yml resolve the array
first, keep the project's guards (one inventory array, the attached
credential's array, a plain pod name, a pod that is the target of a replica
link and not quiescing), never show the token, act through
everpure.flasharray.purefa_pod only after every guard, write one alert-log
record of the documented kind, and end with one set_stats publishing the
artifacts the orchestrator definition reads. orchestrator/dr_failover_test.json
passes exactly the variables the playbooks take and reads only artifacts
they publish."""

import json
import re

import pytest
import yaml

POD_MODULE = "everpure.flasharray.purefa_pod"
SET_STATS = "ansible.builtin.set_stats"
CREDENTIAL_GUARD = (
    "credential_array is not defined or credential_array == inventory_hostname"
)
POD_GUARDS = (
    "before.error is not defined",
    "before.link_source_count | default(0) | int > 0",
    "before.links | selectattr('direction', 'equalto', 'inbound') | list | length > 0",
    "before.promotion_status != 'quiescing'",
)
ARTIFACT_KEYS = {
    "array",
    "pod",
    "remote_array",
    "remote_pod",
    "promotion_status_before",
    "promotion_status_after",
    "link_status_before",
    "link_status_after",
    "changed",
}
DATA_KEY = re.compile(r"'([a-z][a-z0-9_]*)':")
ARTIFACT_REF = re.compile(r"\$\{(dr_promote|dr_demote)\.artifacts\.([a-z_]+)\}")

# playbook -> (record kind, promote value, the extra_vars its template takes)
PLAYBOOKS = {
    "dr_promote.yml": ("dr_promote", True, {"target_array", "pod"}),
    "dr_demote.yml": ("dr_demote", False, {"target_array", "pod", "quiesce"}),
}
TEMPLATE_OF = {
    "dr_promote.yml": "FlashArray - DR promote",
    "dr_demote.yml": "FlashArray - DR demote",
}


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Ansible's !unsafe tag."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _play(repo_root, name):
    doc = yaml.load((repo_root / "playbooks" / name).read_text(), Loader=_Loader)
    assert len(doc) == 1, f"{name}: one play"
    return doc[0]


def _tasks_with(tasks, key):
    return [t for t in tasks if key in t]


def _artifact_keys(tasks):
    data = _tasks_with(tasks, SET_STATS)[-1][SET_STATS]["data"]
    return set(data) if isinstance(data, dict) else set(DATA_KEY.findall(data))


@pytest.mark.parametrize("name", sorted(PLAYBOOKS))
def test_play_targets_one_array_and_resolves_it_first(name, repo_root):
    play = _play(repo_root, name)
    assert play["hosts"] == "{{ target_array }}"
    assert play.get("gather_facts") is False
    first = play["tasks"][0]
    assert first.get("ansible.builtin.include_tasks") == {
        "file": "{{ playbook_dir }}/tasks/resolve_array.yml"
    }
    assert first["vars"] == {"resolve_array_name": "{{ inventory_hostname }}"}


@pytest.mark.parametrize("name", sorted(PLAYBOOKS))
def test_guards_come_before_the_pod_module(name, repo_root):
    tasks = _play(repo_root, name)["tasks"]
    asserts = _tasks_with(tasks, "ansible.builtin.assert")
    conditions = "\n".join(
        "\n".join(a["ansible.builtin.assert"]["that"]) for a in asserts
    )
    assert "inventory_hostname == target_array" in conditions
    assert CREDENTIAL_GUARD in conditions
    assert "pod is match('^[A-Za-z0-9][A-Za-z0-9_-]*$')" in conditions
    for guard in POD_GUARDS:
        assert guard in conditions, f"{name}: missing guard {guard}"
    modules = _tasks_with(tasks, POD_MODULE)
    assert len(modules) == 1, f"{name}: one {POD_MODULE} task"
    assert tasks.index(modules[0]) > max(tasks.index(a) for a in asserts)
    # the pod is read (and parsed) before the guards that look at it
    reads = _tasks_with(tasks, "ansible.builtin.command")
    assert len(reads) == 2, f"{name}: a read before and a read after"
    assert tasks.index(reads[0]) < tasks.index(asserts[-1])
    assert tasks.index(reads[1]) > tasks.index(modules[0])


@pytest.mark.parametrize("name", sorted(PLAYBOOKS))
def test_reads_never_show_the_token(name, repo_root):
    tasks = _play(repo_root, name)["tasks"]
    for task in _tasks_with(tasks, "ansible.builtin.command"):
        assert task.get("no_log") is True, f"{name}: {task['name']} without no_log"
        assert task["environment"]["FA_TOKEN"] == "{{ api_token }}"
        assert task.get("changed_when") is False
    # the shared read script carries no secret: it reads the token from the
    # environment the command task sets
    script = next(
        t["ansible.builtin.set_fact"]["read_pod_script"]
        for t in _tasks_with(tasks, "ansible.builtin.set_fact")
        if "read_pod_script" in t["ansible.builtin.set_fact"]
    )
    assert 'os.environ["FA_TOKEN"]' in script
    assert "{{" not in script


@pytest.mark.parametrize("name", sorted(PLAYBOOKS))
def test_pod_module_call(name, repo_root):
    kind, promote, _ = PLAYBOOKS[name]
    tasks = _play(repo_root, name)["tasks"]
    args = _tasks_with(tasks, POD_MODULE)[0][POD_MODULE]
    assert args["name"] == "{{ pod }}"
    assert args["promote"] is promote
    assert args["fa_url"] == "{{ fa_url }}"
    assert args["api_token"] == "{{ api_token }}"
    if promote:
        # undo (the module default) promotes from an undo-demote pod and
        # reports no change when there is none; a failover test wants the
        # pod's current replicated content
        assert args["undo"] is False
        assert "quiesce" not in args
    else:
        assert args["quiesce"] == "{{ quiesce | default(true) | bool }}"
        assert "undo" not in args


@pytest.mark.parametrize("name", sorted(PLAYBOOKS))
def test_one_record_of_the_documented_kind(name, repo_root):
    kind, promote, _ = PLAYBOOKS[name]
    tasks = _play(repo_root, name)["tasks"]
    records = _tasks_with(tasks, "ansible.builtin.lineinfile")
    assert len(records) == 1, f"{name}: one alert-log record"
    record = records[0]["ansible.builtin.lineinfile"]
    assert record["path"] == "{{ alert_log }}"
    assert records[0].get("delegate_to") == "localhost"
    line = record["line"]
    for key in (
        "'time'",
        f"'kind': '{kind}'",
        "'severity'",
        "'array': inventory_hostname",
        "'pod': pod",
        "'promotion_status_before'",
        "'promotion_status_after'",
        "'link_status_after'",
        "'changed'",
    ):
        assert key in line, f"{name}: record lacks {key}"
    if not promote:
        # demoting a promoted target discards the test's writes; the record says so
        assert "'test_writes_discarded'" in line
        assert "discarded" in line


@pytest.mark.parametrize("name", sorted(PLAYBOOKS))
def test_ends_with_one_set_stats_publishing_the_artifacts(name, repo_root):
    tasks = _play(repo_root, name)["tasks"]
    found = _tasks_with(tasks, SET_STATS)
    assert len(found) == 1, f"{name}: one {SET_STATS} task"
    task = found[0]
    assert tasks[-1] is task, f"{name}: {SET_STATS} is not the last task"
    args = task[SET_STATS]
    assert args.get("aggregate") is False
    assert args.get("per_host") is False
    assert task.get("run_once") is True
    assert task.get("delegate_to") == "localhost"
    missing = ARTIFACT_KEYS - _artifact_keys(tasks)
    assert not missing, f"{name}: artifacts missing {sorted(missing)}"


def test_failover_definition_matches_the_playbooks(repo_root):
    doc = json.loads((repo_root / "orchestrator" / "dr_failover_test.json").read_text())
    nodes = {n["id"]: n for n in doc["nodes"]}
    published = {
        node_id: _artifact_keys(_play(repo_root, name)["tasks"])
        for node_id, name in (
            ("dr_promote", "dr_promote.yml"),
            ("dr_demote", "dr_demote.yml"),
        )
    }
    for name, (kind, _, extra_vars) in PLAYBOOKS.items():
        node = nodes[kind]
        extra = node["parameters"]["extra_vars"]
        assert node["type"] == "aap_job_template"
        assert node["parameters"]["job_template_name"] == TEMPLATE_OF[name]
        assert set(extra) == extra_vars, kind
        assert extra["pod"] == "${trigger.pod}"
        assert extra["target_array"] == "${trigger.target_array}"
    assert nodes["dr_demote"]["parameters"]["extra_vars"]["quiesce"] is True
    # every artifact a node reads is one the playbook publishes
    for node_id, node in nodes.items():
        for job, key in ARTIFACT_REF.findall(json.dumps(node)):
            assert key in published[job], f"{node_id} reads {job}.artifacts.{key}"
    # the revert approval says that the test's writes are discarded, and a
    # rejected or expired revert is a critical notification
    assert "discarded" in nodes["approve_revert"]["parameters"]["prompt"]
    edges = {(e["from"], e.get("from_port")): e["to"] for e in doc["edges"]}
    rejected = nodes[edges[("approve_revert", "rejected")]]
    assert rejected["parameters"]["extra_vars"]["severity"] == "critical"
    assert edges[("approve_revert", "approved")] == "dr_demote"
    assert edges[("approve_promote", "approved")] == "dr_promote"
    # the form's pod pattern is the playbooks' plain-name pattern
    pod = doc["triggers"][0]["parameters"]["input_schema"]["properties"]["pod"]
    assert pod["pattern"] == "^[A-Za-z0-9][A-Za-z0-9_-]*$"
