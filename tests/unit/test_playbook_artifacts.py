# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Static checks over the job artifacts the playbooks publish: every playbook
an Automation Platform workflow or an orchestrator node may follow ends with
one ansible.builtin.set_stats task (aggregate off, per host off, run once on
localhost) that publishes the documented keys; expand_volume.yml keeps the
project's guard rails; every array-facing playbook refuses to run with
another array's credential (credential_array, injected by the FlashArray
API credential type); every array-facing playbook resolves the array's
credentials through playbooks/tasks/resolve_array.yml before it touches the
token; and aap/seed.yml carries the fixed job template names and loads its
credential types from JSON that parses."""

import json
import pathlib
import re

import pytest
import yaml

# playbook -> keys every orchestrator workflow may rely on
ARTIFACT_KEYS = {
    "capacity_request.yml": {
        "request_id",
        "candidate_count",
        "candidates",
        "at_risk_array",
    },
    "capacity_reclaim.yml": {"request_id", "eradicated", "refused", "result"},
    "dr_drill.yml": {"drills", "failed_count"},
    "dr_drill_one.yml": {"drills", "failed_count"},
    "protect_now.yml": {
        "snapshot_suffix",
        "groups_snapshotted",
        "groups_failed",
        "eradication_delay_days",
    },
    "qos_cap.yml": {"volume", "mode", "applied_iops", "previous_iops"},
    "identify_led.yml": {"component", "led_state"},
    "reconcile.yml": {"ntp_changed", "dns_changed"},
    "apply_baseline.yml": {"ntp_changed", "dns_changed", "git_head"},
    "notify.yml": {"record_kind"},
    "alertmanager_silence.yml": {"silence_id", "mode"},
    "expand_volume.yml": {"volume", "old_size", "new_size"},
    "replicate_now.yml": {"pgroup", "reason", "snapshot_suffix", "changed"},
    "dr_switchover.yml": {
        "source_pod",
        "target_pod",
        "source_status_after",
        "target_status_after",
        "link_direction_after",
        "undo_demote_eradicated",
        "changed",
    },
}

# task files included by a playbook: the set_stats sits in the block's always
TASK_FILES = {"dr_drill_one.yml"}

# playbook -> the array whose credential it must have been given; every
# playbook addressed with hosts: "{{ target_array }}" compares with
# inventory_hostname and is added at test time
CREDENTIAL_ARRAY_CHECKS = {
    "capacity_request.yml": "at_risk_array",
    "dr_demote.yml": "inventory_hostname",
    "capacity_reclaim.yml": "request.array",
    "dr_drill_one.yml": "drill.target_array",
    "dr_promote.yml": "inventory_hostname",
    "dr_switchover.yml": "inventory_hostname",
}
CREDENTIAL_GUARD = "credential_array is not defined or credential_array == {}"

JOB_TEMPLATE_NAMES = {
    "FlashArray - capacity request",
    "FlashArray - capacity reclaim",
    "FlashArray - notify",
    "FlashArray - expand volume",
    "FlashArray - DR drill",
    "FlashArray - identify LED",
    "FlashArray - reconcile",
    "FlashArray - apply baseline",
    "FlashArray - protect now",
    "FlashArray - QoS cap",
    "FlashArray - remediate DNS",
    "FlashArray - remediate NTP",
    "FlashArray - replicate now",
    "FlashArray - preferred array",
    "FlashArray - alert runbook",
    "FlashArray - orchestrator post",
    "FlashArray - DR promote",
    "FlashArray - DR demote",
    "FlashArray - DR switchover",
    "FlashArray - Alertmanager silence",
}

RESOLVE_ARRAY = "{{ playbook_dir }}/tasks/resolve_array.yml"
# playbook -> the expression it resolves; alert_runbook resolves only for a
# per-code runbook (its other routes touch no array) and capacity_request
# only when the array is an inventory host (the Alertmanager path may name
# one that is not, and the request must still be written)
RESOLVES = {
    "alert_runbook.yml": "target_array",
    "apply_baseline.yml": "inventory_hostname",
    "capacity_reclaim.yml": "request.array",
    "capacity_request.yml": "at_risk_array",
    "dr_demote.yml": "inventory_hostname",
    "dr_drill_one.yml": "drill.target_array",
    "dr_promote.yml": "inventory_hostname",
    # two plays, one per side of the link, each resolving its own array
    "dr_switchover.yml": ["inventory_hostname", "inventory_hostname"],
    "expand_volume.yml": "inventory_hostname",
    "identify_led.yml": "inventory_hostname",
    "preferred_array.yml": "inventory_hostname",
    "protect_now.yml": "inventory_hostname",
    "qos_cap.yml": "inventory_hostname",
    "reconcile.yml": "inventory_hostname",
    "remediate_dns.yml": "inventory_hostname",
    "remediate_ntp.yml": "inventory_hostname",
    "replicate_now.yml": "inventory_hostname",
}

SET_STATS = "ansible.builtin.set_stats"
DATA_KEY = re.compile(r"'([a-z][a-z0-9_]*)':")


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Ansible's !unsafe tag."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _load(path):
    return yaml.load(path.read_text(), Loader=_Loader)


def _task_lists(tasks):
    """Yield every list of tasks under `tasks`, blocks included."""
    yield tasks
    for task in tasks:
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _task_lists(task[key])


def _all_task_lists(doc, task_file):
    if task_file:
        yield from _task_lists(doc)
        return
    for play in doc:
        for key in ("pre_tasks", "tasks", "post_tasks"):
            if key in play:
                yield from _task_lists(play[key])


def _data_keys(data):
    """Keys of a set_stats data block, written as a mapping or as one
    templated dict ("{{ {'key': value, ...} }}", which keeps native types)."""
    if isinstance(data, dict):
        return set(data)
    return set(DATA_KEY.findall(data))


@pytest.mark.parametrize("name", sorted(ARTIFACT_KEYS))
def test_playbook_ends_with_one_set_stats_publishing_the_documented_keys(
    name, repo_root
):
    path = repo_root / "playbooks" / name
    doc = _load(path)
    task_file = name in TASK_FILES
    found = [
        (tasks, i)
        for tasks in _all_task_lists(doc, task_file)
        for i, task in enumerate(tasks)
        if SET_STATS in task
    ]
    assert len(found) == 1, f"{name}: expected one {SET_STATS} task, found {len(found)}"
    tasks, index = found[0]
    task = tasks[index]
    assert index == len(tasks) - 1, f"{name}: {SET_STATS} is not the last task"
    # the last task of the last play (or of the block's always for a task file)
    if task_file:
        assert doc[-1]["always"][-1] is task, f"{name}: set_stats is not last in always"
    else:
        assert (
            doc[-1]["tasks"][-1] is task
        ), f"{name}: set_stats is not in the last play"
    args = task[SET_STATS]
    assert args.get("aggregate") is False, f"{name}: aggregate must be false"
    assert args.get("per_host") is False, f"{name}: per_host must be false"
    assert task.get("run_once") is True, f"{name}: set_stats must run once"
    assert task.get("delegate_to") == "localhost", f"{name}: delegate to localhost"
    missing = ARTIFACT_KEYS[name] - _data_keys(args["data"])
    assert not missing, f"{name}: artifacts missing {sorted(missing)}"


def test_expand_volume_keeps_the_guard_rails(repo_root):
    doc = _load(repo_root / "playbooks" / "expand_volume.yml")
    assert len(doc) == 1
    play = doc[0]
    assert play["hosts"] == "{{ target_array }}"
    assert play.get("gather_facts") is False
    tasks = play["tasks"]
    asserts = [
        t["ansible.builtin.assert"] for t in tasks if "ansible.builtin.assert" in t
    ]
    conditions = "\n".join("\n".join(a["that"]) for a in asserts)
    assert "inventory_hostname == target_array" in conditions
    assert "volume is match(" in conditions
    assert "new_size | string is match(" in conditions
    assert "new_size_bytes | int > before.provisioned" in conditions
    # the size read never shows the token, and the assertions come before the write
    read = next(t for t in tasks if "ansible.builtin.command" in t)
    assert read.get("no_log") is True
    grow = next(t for t in tasks if "everpure.flasharray.purefa_volume" in t)
    assert tasks.index(grow) > max(
        tasks.index(t) for t in tasks if "ansible.builtin.assert" in t
    )
    assert grow["everpure.flasharray.purefa_volume"]["size"] == "{{ new_size }}"
    assert grow["everpure.flasharray.purefa_volume"]["name"] == "{{ volume }}"
    record = next(t for t in tasks if "ansible.builtin.lineinfile" in t)
    line = record["ansible.builtin.lineinfile"]["line"]
    assert record["ansible.builtin.lineinfile"]["path"] == "{{ alert_log }}"
    for key in ("'time'", "'kind': 'volume_expand'", "'severity'", "'array'"):
        assert key in line, f"record lacks {key}"
    assert record.get("delegate_to") == "localhost"


def _seed(repo_root):
    return _load(repo_root / "aap" / "seed.yml")


def test_seed_carries_the_fixed_job_template_names(repo_root):
    play = _seed(repo_root)[0]
    templates = play["vars"]["job_templates"]
    assert {t["name"] for t in templates} == JOB_TEMPLATE_NAMES
    for template in templates:
        playbook = repo_root / template["playbook"]
        if template.get("requires") == "orchestrator":
            continue  # playbooks/orchestrator_post.yml belongs to another lane
        assert playbook.exists(), f"{template['name']}: {template['playbook']} missing"
    groups = set(play["module_defaults"])
    assert groups == {
        "group/ansible.platform.gateway",
        "group/ansible.controller.controller",
        "group/ansible.eda.eda",
    }


def test_seed_refuses_the_orchestrator_placeholder_in_activation_vars(repo_root):
    play = _seed(repo_root)[0]
    names = [t.get("name", "") for t in play["tasks"]]
    guard = next(
        t
        for t in play["tasks"]
        if t.get("name", "").startswith("Refuse example placeholders")
    )
    that = "\n".join(guard["ansible.builtin.assert"]["that"])
    for needle in ("orchestrator_enabled", "orchestrator_seeded", "example.com"):
        assert needle in that
    assert names.index(guard["name"]) < names.index(
        "One rulebook activation per platform rulebook"
    )


def test_seed_workflow_has_request_approval_reclaim(repo_root):
    play = _seed(repo_root)[0]
    workflow = next(
        t["ansible.controller.workflow_job_template"]
        for t in play["tasks"]
        if "ansible.controller.workflow_job_template" in t
    )
    nodes = {n["identifier"]: n for n in workflow["workflow_nodes"]}
    assert {"request", "approve", "reclaim"} <= set(nodes)
    assert nodes["approve"]["unified_job_template"]["type"] == "workflow_approval"
    assert nodes["request"]["related"]["success_nodes"] == [{"identifier": "approve"}]
    assert nodes["approve"]["related"]["success_nodes"] == [{"identifier": "reclaim"}]
    assert nodes["reclaim"]["extra_data"]["reclaim_action"] == "eradicate_pending"
    for identifier in ("request", "approve", "reclaim"):
        assert nodes[identifier]["related"].get("failure_nodes"), identifier


def test_seed_variables_are_documented_and_hold_no_lab_specifics(repo_root):
    seed_text = (repo_root / "aap" / "seed.yml").read_text()
    example = _load(repo_root / "aap" / "vars" / "example.yml")
    referenced = set(re.findall(r"\baap_[a-z0-9_]+", seed_text))
    missing = sorted(referenced - set(example))
    assert (
        not missing
    ), f"aap/seed.yml references vars not in aap/vars/example.yml: {missing}"
    tree = "\n".join(
        p.read_text() for p in (repo_root / "aap").rglob("*") if p.is_file()
    )
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", tree), "an address in aap/"
    # the project URL and <placeholders> are fine; a real host is not
    urls = {u for u in re.findall(r"https?://[^\s'\")]+", tree) if "<" not in u}
    assert urls <= {"https://github.com/sdodsley/flasharray-eda"}, urls
    assert "/home/" not in tree and "C:\\" not in tree
    # every task that writes a credential (its inputs hold a token) is no_log
    credential_tasks = [
        t
        for t in _seed(repo_root)[0]["tasks"]
        if any(k.endswith(".credential") for k in t)
    ]
    # controller: per array, fleet, orchestrator, Slack webhook; EDA: per
    # array, fleet, orchestrator, platform user
    assert len(credential_tasks) == 8
    for task in credential_tasks:
        assert task.get("no_log") is True, f"{task['name']}: credential without no_log"


def test_seed_credential_type_files_parse_and_match(repo_root):
    files = repo_root / "aap" / "files"
    for kind in ("flasharray_api", "orchestrator_sa", "slack_webhook"):
        inputs = json.loads((files / f"credential_type_{kind}_inputs.json").read_text())
        injectors = json.loads(
            (files / f"credential_type_{kind}_injectors.json").read_text()
        )
        ids = {f["id"] for f in inputs["fields"]}
        assert set(inputs["required"]) <= ids
        used = set()
        for section in injectors.values():
            for value in section.values():
                used |= set(re.findall(r"{{\s*([a-z_]+)\s*}}", value))
        assert used == ids, f"{kind}: injectors use {used}, inputs define {ids}"
    api = json.loads((files / "credential_type_flasharray_api_inputs.json").read_text())
    secret = {f["id"] for f in api["fields"] if f.get("secret")}
    assert secret == {"api_token"}
    sa = json.loads((files / "credential_type_orchestrator_sa_inputs.json").read_text())
    assert {f["id"] for f in sa["fields"] if f.get("secret")} == {"client_secret"}
    sa_inj = json.loads(
        (files / "credential_type_orchestrator_sa_injectors.json").read_text()
    )
    assert set(sa_inj["env"]) == {"AO_CLIENT_ID", "AO_CLIENT_SECRET"}
    assert set(sa_inj["extra_vars"]) == {"orchestrator_url"}


def test_credential_type_names_its_array(repo_root):
    """The FlashArray API credential carries the array's inventory name as a
    plain (non-secret, required) input and injects it as credential_array."""
    files = repo_root / "aap" / "files"
    inputs = json.loads(
        (files / "credential_type_flasharray_api_inputs.json").read_text()
    )
    field = next(f for f in inputs["fields"] if f["id"] == "array_name")
    assert not field.get("secret")
    assert "array_name" in inputs["required"]
    injectors = json.loads(
        (files / "credential_type_flasharray_api_injectors.json").read_text()
    )
    assert injectors["extra_vars"]["credential_array"] == "{{ array_name }}"
    # the seed fills it from the array's name, for the controller and for EDA
    play = _seed(repo_root)[0]
    credential_inputs = [
        t[k]["inputs"]
        for t in play["tasks"]
        for k in t
        if k in ("ansible.controller.credential", "ansible.eda.credential")
        and t[k].get("credential_type", t[k].get("credential_type_name"))
        == "FlashArray API"
    ]
    assert len(credential_inputs) == 2
    for inputs in credential_inputs:
        assert inputs["array_name"] == "{{ item.name }}"


def _assert_conditions(doc, task_file):
    out = []
    for tasks in _all_task_lists(doc, task_file):
        for task in tasks:
            if "ansible.builtin.assert" in task:
                out.extend(task["ansible.builtin.assert"]["that"])
    return out


def test_playbooks_refuse_another_arrays_credential(repo_root):
    """With one credential attached, hostvars[<any array>].fa_url is the
    attached array's, so each playbook must compare credential_array with the
    array it was asked to act on."""
    expected = dict(CREDENTIAL_ARRAY_CHECKS)
    for path in sorted((repo_root / "playbooks").glob("*.yml")):
        doc = _load(path)
        if isinstance(doc[0], dict) and doc[0].get("hosts") == "{{ target_array }}":
            expected[path.name] = "inventory_hostname"
    assert len(expected) >= 4 + 7, sorted(expected)
    for name, array in sorted(expected.items()):
        doc = _load(repo_root / "playbooks" / name)
        conditions = _assert_conditions(doc, name in TASK_FILES)
        assert CREDENTIAL_GUARD.format(array) in conditions, f"{name}: no guard"
    # the drill resolves its target's credentials and then checks the guard
    # as the first two tasks of its block, so a missing entry or a mismatch
    # is recorded as a failed drill and the cleanup never touches another
    # array
    drill = _load(repo_root / "playbooks" / "dr_drill_one.yml")
    block = next(t for t in drill if "block" in t)
    assert "ansible.builtin.include_tasks" in block["block"][0]
    second = block["block"][1]["ansible.builtin.assert"]["that"]
    assert CREDENTIAL_GUARD.format("drill.target_array") in second
    cleanup = next(
        t for t in block["always"] if "everpure.flasharray.purefa_volume" in t
    )
    assert "credential_array" in cleanup["when"]
    # apply_baseline spans every array (hosts: all:!localhost), so instead of
    # an assert that would fail every multi-array launch its first task ends
    # the hosts whose credential is not attached, before any baseline is read
    baseline = _load(repo_root / "playbooks" / "apply_baseline.yml")
    assert baseline[1]["hosts"] == "all:!localhost"
    first = baseline[1]["tasks"][0]
    assert (
        "ansible.builtin.meta" in first
    ), "apply_baseline.yml: no end_host guard first"
    assert first["ansible.builtin.meta"] == "end_host"
    assert "credential_array" in first["when"]
    assert "inventory_hostname" in first["when"]


def test_seed_limits_apply_baseline_to_the_attached_credentials_array(repo_root):
    """One launch applies one array's baseline: the template is limited to
    localhost (the play that refreshes the checkout) plus the default array,
    and every template prompts for the limit so another array's launch can
    change the credential and the limit together."""
    play = _seed(repo_root)[0]
    baseline = next(
        t
        for t in play["vars"]["job_templates"]
        if t["name"] == "FlashArray - apply baseline"
    )
    assert baseline["limit"] == "localhost,{{ aap_default_array }}"
    others = [t for t in play["vars"]["job_templates"] if "limit" in t]
    assert others == [baseline]
    template = next(
        t["ansible.controller.job_template"]
        for t in play["tasks"]
        if "ansible.controller.job_template" in t
    )
    assert template["limit"] == "{{ item.limit | default(omit) }}"
    assert template["ask_limit_on_launch"] is True


def _ordered_tasks(doc, task_file):
    """Every task in run order, blocks flattened (rescue and always last)."""
    out = []

    def walk(tasks):
        for task in tasks:
            if "block" in task:
                walk(task["block"])
                walk(task.get("rescue", []))
                walk(task.get("always", []))
            else:
                out.append(task)

    if task_file:
        walk(doc)
    else:
        for play in doc:
            for key in ("pre_tasks", "tasks", "post_tasks"):
                walk(play.get(key, []))
    return out


def _include_file(task):
    """The file an include_tasks task names, written as a string or a dict."""
    args = task.get("ansible.builtin.include_tasks")
    return args.get("file") if isinstance(args, dict) else args


def test_array_facing_playbooks_resolve_the_array_before_touching_the_token(
    repo_root,
):
    """Every playbook that uses api_token first includes resolve_array.yml
    with resolve_array_name set to the array it acts on, and the include
    comes before any task that references the token; playbooks that never
    touch an array (notify, orchestrator_post) include nothing."""
    playbooks = repo_root / "playbooks"
    for path in sorted(playbooks.glob("*.yml")):
        doc = _load(path)
        tasks = _ordered_tasks(doc, path.name in TASK_FILES)
        includes = [t for t in tasks if _include_file(t) == RESOLVE_ARRAY]
        if path.name not in RESOLVES:
            assert not includes, f"{path.name}: includes resolve_array.yml"
            assert "api_token" not in path.read_text(), f"{path.name}: unresolved"
            continue
        wanted = RESOLVES[path.name]
        wanted = wanted if isinstance(wanted, list) else [wanted]
        assert len(includes) == len(wanted), f"{path.name}: resolve includes"
        for include, name in zip(includes, wanted):
            expected = "{{ " + name + " }}"
            assert include["vars"]["resolve_array_name"] == expected, path.name
        include = includes[0]
        # alert_runbook.yml itself never names the token: its per-code task
        # files do, delegated to the array, so only the include is checked
        first_token = next(
            (
                i
                for i, t in enumerate(tasks)
                if "api_token" in yaml.safe_dump(t, default_flow_style=False)
            ),
            None,
        )
        if first_token is not None:
            assert tasks.index(include) < first_token, f"{path.name}: token late"
    tasks = _load(playbooks / "tasks" / "resolve_array.yml")
    # every task that handles the token is no_log, and the fleet entry is
    # applied to the array's own host so hostvars[<array>] sees it
    for task in tasks:
        if "set_fact" in str(task):
            assert task.get("no_log") is True, task["name"]
    last = tasks[-1]
    assert set(last["ansible.builtin.set_fact"]) == {
        "fa_url",
        "api_token",
        "credential_array",
    }
    assert last["delegate_to"] == "{{ resolve_array_name }}"
    assert last["delegate_facts"] is True
    refusal = next(t for t in tasks if "ansible.builtin.assert" in t)
    assert "purefa_fleet_json is defined" in refusal["ansible.builtin.assert"]["that"]
    assert "FlashArray fleet" in refusal["ansible.builtin.assert"]["fail_msg"]
    assert "FlashArray API" in refusal["ansible.builtin.assert"]["fail_msg"]


def test_fleet_credential_type_files_parse_and_match(repo_root):
    """The FlashArray fleet type has one secret multiline input, arrays, that
    the controller injects as purefa_fleet_json (resolve_array.yml reads it)
    and the EDA controller as purefa_arrays (the source plugins decode the
    JSON string)."""
    files = repo_root / "aap" / "files"
    inputs = json.loads(
        (files / "credential_type_flasharray_fleet_inputs.json").read_text()
    )
    assert [f["id"] for f in inputs["fields"]] == ["arrays"]
    field = inputs["fields"][0]
    assert field["secret"] is True and field["multiline"] is True
    assert field["label"] == "Arrays (JSON)"
    assert inputs["required"] == ["arrays"]
    controller = json.loads(
        (files / "credential_type_flasharray_fleet_injectors.json").read_text()
    )
    assert controller == {"extra_vars": {"purefa_fleet_json": "{{ arrays }}"}}
    eda = json.loads(
        (files / "credential_type_flasharray_fleet_eda_injectors.json").read_text()
    )
    assert eda == {"extra_vars": {"purefa_arrays": "{{ arrays }}"}}


def test_every_rulebook_gets_an_activation_name(repo_root):
    """The seed derives activation names from the rulebook files; every file
    must be a plain name that survives the template."""
    for path in sorted((repo_root / "extensions" / "eda" / "rulebooks").glob("*.yml")):
        assert re.fullmatch(r"[a-z_]+\.yml", path.name), path.name
    assert pathlib.Path(repo_root / "aap" / "requirements.yml").exists()


# Inside a Jinja expression Ansible doubles backslashes before Jinja parses
# the string, so a '\n' written there is a backslash and an n, never a line
# break (a real one belongs to the YAML: a block scalar, or "\n" in a
# double-quoted scalar). Regex arguments are the exception: the doubling is
# what carries '\r' to the regex engine as a carriage-return class. The scan
# runs over the loaded YAML, where a YAML escape is already a newline and only
# a Jinja literal is still two characters.
ESCAPE_LITERAL = re.compile(r"""(['"])(?:\\.|(?!\1).)*\\[nrt](?:\\.|(?!\1).)*\1""")
REGEX_FILTER = re.compile(r"regex_(?:replace|search|findall|escape)\(\s*$")
JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that passes any local tag (!unsafe, !vault) through."""


def _any_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TolerantLoader.add_multi_constructor("!", _any_tag)


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_no_escape_literals_inside_jinja_expressions(repo_root):
    files = (
        list((repo_root / "playbooks").rglob("*.yml"))
        + list((repo_root / "extensions" / "eda" / "rulebooks").rglob("*.yml"))
        + list((repo_root / "aap").glob("*.yml"))
        + list((repo_root / "tests").glob("*.yml"))
    )
    offenders = []
    for path in sorted(files):
        documents = list(yaml.load_all(path.read_text(), Loader=_TolerantLoader))
        for text in _strings(documents):
            for expression in JINJA_EXPRESSION.finditer(text):
                body = expression.group(0)
                for literal in ESCAPE_LITERAL.finditer(body):
                    if REGEX_FILTER.search(body[: literal.start()]):
                        continue
                    offenders.append(
                        f"{path.relative_to(repo_root)}: {literal.group(0)} in {body[:70]!r}"
                    )
    assert offenders == [], "\n".join(offenders)
