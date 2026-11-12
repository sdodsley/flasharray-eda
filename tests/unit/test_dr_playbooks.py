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
    "dr_demote.yml": (
        "dr_demote",
        False,
        {"target_array", "pod", "quiesce", "eradicate_undo_demote"},
    ),
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
    # the demote play eradicates an earlier demotion's undo-demote pods before
    # the demotion; the promote play has the one module task
    expected = 2 if name == "dr_demote.yml" else 1
    assert len(modules) == expected, f"{name}: {expected} {POD_MODULE} task(s)"
    assert tasks.index(modules[0]) > max(tasks.index(a) for a in asserts)
    # the pod is read (and parsed) before the guards that look at it
    reads = _tasks_with(tasks, "ansible.builtin.command")
    assert len(reads) == 2, f"{name}: a read before and a read after"
    assert tasks.index(reads[0]) < tasks.index(asserts[-1])
    assert tasks.index(reads[1]) > tasks.index(modules[-1])


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
    # the promotion or demotion itself is the last module task (the demote
    # play eradicates undo-demote pods before it)
    args = _tasks_with(tasks, POD_MODULE)[-1][POD_MODULE]
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
    # the revert eradicates the undo-demote pod an earlier demotion left, so
    # a test can be repeated inside the eradication delay, and its prompt says so
    assert (
        nodes["dr_demote"]["parameters"]["extra_vars"]["eradicate_undo_demote"] is True
    )
    assert "undo-demote" in nodes["approve_revert"]["parameters"]["prompt"]
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


def test_demote_eradicates_the_undo_demote_pods_only_when_told(repo_root):
    tasks = _play(repo_root, "dr_demote.yml")["tasks"]
    eradicate, demote = _tasks_with(tasks, POD_MODULE)
    assert demote[POD_MODULE]["promote"] is False
    # an earlier demotion's undo-demote pods block the next one; the play
    # names them and refuses unless told to eradicate them, then does so
    # after every guard and before the demotion
    assert eradicate[POD_MODULE] == {
        "fa_url": "{{ fa_url }}",
        "api_token": "{{ api_token }}",
        "disable_warnings": True,
        "name": "{{ item }}",
        "state": "absent",
        "eradicate": True,
        "delete_contents": True,
    }
    assert eradicate["loop"] == "{{ before.undo_demote_pods | default([]) }}"
    assert "eradicate_undo_demote | default(false) | bool" in eradicate["when"]
    assert "before.promotion_status != 'demoted'" in eradicate["when"]
    assert eradicate["register"] == "undo_eradicated"
    asserts = _tasks_with(tasks, "ansible.builtin.assert")
    assert (
        max(tasks.index(a) for a in asserts)
        < tasks.index(eradicate)
        < tasks.index(demote)
    )
    guard = "\n".join("\n".join(a["ansible.builtin.assert"]["that"]) for a in asserts)
    assert "before.undo_demote_pods | default([]) | length == 0" in guard
    assert "eradicate_undo_demote | default(false) | bool" in guard
    read = _tasks_with(tasks, "ansible.builtin.set_fact")[0]
    assert (
        '"undo_demote_pods": undo_names'
        in read["ansible.builtin.set_fact"]["read_pod_script"]
    )
    # the record and the artifacts say which pods went
    record = _tasks_with(tasks, "ansible.builtin.lineinfile")[0]
    assert "'undo_demote_eradicated'" in record["ansible.builtin.lineinfile"]["line"]
    assert "undo_demote_eradicated" in _artifact_keys(tasks)


# ---- the planned switchover: one playbook, two plays, one per side -------
SWITCHOVER = "dr_switchover.yml"
SWITCHOVER_KEYS = {
    "source_array",
    "source_pod",
    "target_array",
    "target_pod",
    "source_status_before",
    "source_status_after",
    "target_status_before",
    "target_status_after",
    "link_direction_after",
    "link_status_after",
    "undo_demote_eradicated",
    "changed",
}
SWITCHOVER_REF = re.compile(r"\$\{(dr_failover|dr_failback)\.artifacts\.([a-z_]+)\}")


def _plays(repo_root, name):
    return yaml.load((repo_root / "playbooks" / name).read_text(), Loader=_Loader)


def _conditions(play):
    return "\n".join(
        "\n".join(a["ansible.builtin.assert"]["that"])
        for a in _tasks_with(play["tasks"], "ansible.builtin.assert")
    )


def test_switchover_has_two_plays_each_resolving_and_guarding_its_own_array(
    repo_root,
):
    plays = _plays(repo_root, SWITCHOVER)
    assert [p["hosts"] for p in plays] == ["{{ source_array }}", "{{ target_array }}"]
    for play in plays:
        assert play.get("gather_facts") is False
        first = play["tasks"][0]
        assert first.get("ansible.builtin.include_tasks") == {
            "file": "{{ playbook_dir }}/tasks/resolve_array.yml"
        }
        assert first["vars"] == {"resolve_array_name": "{{ inventory_hostname }}"}
        assert CREDENTIAL_GUARD in _conditions(play)
    source, target = plays
    s = _conditions(source)
    assert "inventory_hostname == source_array" in s
    assert "source_array != target_array" in s
    assert "source_before.error is not defined" in s and "'outbound'" in s
    t = _conditions(target)
    assert "inventory_hostname == target_array" in t
    assert "target_before.promotion_status != 'quiescing'" in t and "'inbound'" in t


def test_switchover_demotes_with_quiesce_then_promotes_after_the_copy_is_complete(
    repo_root,
):
    source, target = _plays(repo_root, SWITCHOVER)
    eradicate, demote = _tasks_with(source["tasks"], POD_MODULE)
    assert demote[POD_MODULE]["name"] == "{{ source_pod }}"
    assert demote[POD_MODULE]["promote"] is False
    assert demote[POD_MODULE]["quiesce"] is True
    asserts = _tasks_with(source["tasks"], "ansible.builtin.assert")
    assert source["tasks"].index(demote) > max(
        source["tasks"].index(a) for a in asserts
    )
    # an earlier demotion's undo-demote pods block the next one; the play
    # names them and refuses unless told to eradicate them, then does so
    # before the demotion, after every guard
    assert eradicate[POD_MODULE] == {
        "fa_url": "{{ fa_url }}",
        "api_token": "{{ api_token }}",
        "disable_warnings": True,
        "name": "{{ item }}",
        "state": "absent",
        "eradicate": True,
        "delete_contents": True,
    }
    assert eradicate["loop"] == "{{ source_before.undo_demote_pods | default([]) }}"
    assert "eradicate_undo_demote | default(false) | bool" in eradicate["when"]
    assert eradicate["register"] == "undo_eradicated"
    assert max(source["tasks"].index(a) for a in asserts) < source["tasks"].index(
        eradicate
    )
    guard = _conditions(source)
    assert "source_before.undo_demote_pods | default([]) | length == 0" in guard
    assert "eradicate_undo_demote | default(false) | bool" in guard
    assert (
        '"undo_demote_pods": undo_names'
        in source["tasks"][3]["ansible.builtin.set_fact"]["read_pod_script"]
    )
    (promote,) = _tasks_with(target["tasks"], POD_MODULE)
    assert promote[POD_MODULE]["name"] == "{{ target_pod }}"
    assert promote[POD_MODULE]["promote"] is True
    assert promote[POD_MODULE]["undo"] is False
    reads = _tasks_with(target["tasks"], "ansible.builtin.command")
    # a read that waits for the link to report quiesced sits between the
    # guards and the promotion, so stale data is never promoted
    wait = next(r for r in reads if "quiesced" in str(r.get("until", "")))
    guards = _tasks_with(target["tasks"], "ansible.builtin.assert")
    assert target["tasks"].index(wait) > max(target["tasks"].index(a) for a in guards)
    assert target["tasks"].index(wait) < target["tasks"].index(promote)
    assert "ignore_errors" not in wait
    for play in (source, target):
        for task in _tasks_with(play["tasks"], "ansible.builtin.command"):
            assert task.get("no_log") is True, task["name"]
            if task["name"].startswith(("Mark ", "Clear ")):
                # the planned-work tags: one task per pod, each with its array's token
                assert task["environment"]["FA_TOKEN"] in (
                    "{{ api_token }}",
                    "{{ peer_api_token }}",
                )
                assert task.get("changed_when") is True
                continue
            assert task["environment"]["FA_TOKEN"] == "{{ api_token }}"
            assert task.get("changed_when") is False


def test_switchover_records_and_publishes_the_outcome(repo_root):
    source, target = _plays(repo_root, SWITCHOVER)
    assert not _tasks_with(source["tasks"], SET_STATS)
    assert not _tasks_with(source["tasks"], "ansible.builtin.lineinfile")
    (record,) = _tasks_with(target["tasks"], "ansible.builtin.lineinfile")
    line = record["ansible.builtin.lineinfile"]["line"]
    assert "'kind': 'dr_switchover'" in line and "'severity'" in line
    assert record["ansible.builtin.lineinfile"]["path"] == "{{ alert_log }}"
    (stats,) = _tasks_with(target["tasks"], SET_STATS)
    assert target["tasks"][-1] is stats
    assert stats[SET_STATS].get("aggregate") is False
    assert stats.get("run_once") is True and stats.get("delegate_to") == "localhost"
    assert SWITCHOVER_KEYS <= _artifact_keys(target["tasks"])


def test_switchover_marks_both_pods_as_planned_work_and_clears_them(repo_root):
    source, target = _plays(repo_root, SWITCHOVER)
    names = [t["name"] for t in source["tasks"]]
    mark_source = names.index("Mark the source pod as planned work")
    mark_target = names.index("Mark the target pod as planned work")
    demote = next(
        i
        for i, t in enumerate(source["tasks"])
        if POD_MODULE in t and t[POD_MODULE].get("promote") is False
    )
    asserts = _tasks_with(source["tasks"], "ansible.builtin.assert")
    # after every guard, before anything changes
    assert max(source["tasks"].index(a) for a in asserts) < mark_source
    assert mark_source < mark_target < demote
    script = next(
        t["ansible.builtin.set_fact"]["planned_work_script"]
        for t in source["tasks"]
        if "planned_work_script" in t.get("ansible.builtin.set_fact", {})
    )
    assert 'key="planned_work"' in script
    assert "put_pods_tags_batch" in script and "delete_pods_tags" in script
    target_mark = source["tasks"][mark_target]
    assert target_mark["environment"]["FA_TOKEN"] == "{{ peer_api_token }}"
    assert "peer_fa_url | length > 0" in target_mark["when"]
    assert source["tasks"][mark_source]["environment"]["MODE"] == "set"
    # the target play clears both tags last, before the artifacts, never failing on it
    tnames = [t["name"] for t in target["tasks"]]
    clear_t = tnames.index("Clear the planned-work tags on the target pod")
    clear_s = tnames.index("Clear the planned-work tags on the source pod")
    assert clear_t < clear_s < len(target["tasks"]) - 1
    assert SET_STATS in target["tasks"][-1]
    for i in (clear_t, clear_s):
        assert target["tasks"][i].get("failed_when") is False
        assert target["tasks"][i]["environment"]["MODE"] == "clear"
    # each play still resolves only its own array; the peer's credentials come
    # from the inventory or the fleet credential without a second include
    for play in (source, target):
        includes = _tasks_with(play["tasks"], "ansible.builtin.include_tasks")
        assert len(includes) == 1


def test_switchover_definition_matches_the_playbook(repo_root):
    doc = json.loads(
        (repo_root / "orchestrator" / "dr_failover_failback.json").read_text()
    )
    nodes = {n["id"]: n for n in doc["nodes"]}
    published = _artifact_keys(_plays(repo_root, SWITCHOVER)[1]["tasks"])
    for node_id in ("dr_failover", "dr_failback"):
        params = nodes[node_id]["parameters"]
        assert params["job_template_name"] == "FlashArray - DR switchover"
        assert set(params["extra_vars"]) == {
            "source_array",
            "source_pod",
            "target_array",
            "target_pod",
            "eradicate_undo_demote",
        }
        # a repeated switchover inside the eradication delay would otherwise
        # stop at the undo-demote pod the last one left
        assert params["extra_vars"]["eradicate_undo_demote"] is True
        # the playbook acts on two arrays, so the node leaves the template's
        # fleet credential in place instead of pinning one array's
        assert "credential_names" not in params
    over = nodes["dr_failover"]["parameters"]["extra_vars"]
    back = nodes["dr_failback"]["parameters"]["extra_vars"]
    assert over["source_array"] == "${trigger.source_array}"
    assert over["target_pod"] == "${trigger.target_pod}"
    assert back["source_array"] == "${trigger.target_array}"
    assert back["source_pod"] == "${trigger.target_pod}"
    assert back["target_pod"] == "${trigger.source_pod}"
    for node_id, node in nodes.items():
        for job, key in SWITCHOVER_REF.findall(json.dumps(node)):
            assert key in published, f"{node_id} reads {job}.artifacts.{key}"
    edges = {(e["from"], e.get("from_port")): e["to"] for e in doc["edges"]}
    # each switchover sits between an Alertmanager silence and its expiry
    assert edges[("approve_failover", "approved")] == "silence_failover"
    assert edges[("silence_failover", None)] == "dr_failover"
    assert edges[("notify_failed_over", None)] == "unsilence_failover"
    assert edges[("unsilence_failover", None)] == "approve_failback"
    assert edges[("approve_failback", "approved")] == "silence_failback"
    assert edges[("silence_failback", None)] == "dr_failback"
    assert edges[("notify_failed_back", None)] == "unsilence_failback"
    for node_id in ("silence_failover", "silence_failback"):
        params = nodes[node_id]["parameters"]
        assert params["job_template_name"] == "FlashArray - Alertmanager silence"
        assert params["extra_vars"]["mode"] == "create"
        assert (
            params["extra_vars"]["silence_array_regex"]
            == "${trigger.source_array}|${trigger.target_array}"
        )
    for node_id, maker in (
        ("unsilence_failover", "silence_failover"),
        ("unsilence_failback", "silence_failback"),
    ):
        assert nodes[node_id]["parameters"]["extra_vars"] == {
            "mode": "expire",
            "silence_id": "${" + maker + ".artifacts.silence_id}",
        }
    rejected = nodes[edges[("approve_failback", "rejected")]]
    assert rejected["parameters"]["extra_vars"]["severity"] == "critical"


def _render(template, **vars):
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(template).render(**vars)


@pytest.mark.parametrize(
    "name, var, extra",
    [
        ("dr_promote.yml", "before", {"pod": "p", "target_array": "a"}),
        ("dr_demote.yml", "before", {"pod": "p", "target_array": "a"}),
        (
            "dr_switchover.yml",
            "source_before",
            {
                "source_pod": "p",
                "source_array": "a",
                "target_pod": "q",
                "target_array": "b",
            },
        ),
        (
            "dr_switchover.yml",
            "target_before",
            {
                "source_pod": "p",
                "source_array": "a",
                "target_pod": "q",
                "target_array": "b",
            },
        ),
    ],
)
def test_pod_guard_messages_render_the_read_error_for_a_missing_pod(
    repo_root, name, var, extra
):
    # the message's default(...) branch is evaluated even when the read
    # returned an error, so every field it touches must tolerate absence
    plays = _plays(repo_root, name)
    guards = [
        a
        for play in plays
        for a in _tasks_with(play["tasks"], "ansible.builtin.assert")
        if f"{var}.error is not defined"
        in "\n".join(a["ansible.builtin.assert"]["that"])
    ]
    assert guards, f"{name} has no pod guard on {var}"
    for guard in guards:
        message = guard["ansible.builtin.assert"]["fail_msg"]
        rendered = _render(message, **{var: {"error": "Pod does not exist."}}, **extra)
        assert rendered.strip() == "Pod does not exist."
        rendered = _render(
            message,
            **{
                var: {
                    "promotion_status": "quiescing",
                    "links": [],
                    "link_source_count": 0,
                    "link_target_count": 0,
                }
            },
            **extra,
        )
        assert "quiescing" in rendered
