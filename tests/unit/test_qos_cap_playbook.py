# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""qos_cap.yml never loosens a limit: the cap task is skipped when the volume's
own IOPS limit is already at or below cap_iops, and applied_iops reports the
limit in force. A member of a volume group that carries its own limit is
handled by qos_cap_scope (notify, vgroup, volume) and a lift follows the
tags. The playbook is loaded as YAML and the expressions rendered with Jinja2
the way Ansible would (a `when` is an expression, a set_fact a template)."""

import json

import pytest
import yaml

PLAYBOOK = "qos_cap.yml"
NO_LIMIT = 100000000
SKIPPED = {"skipped": True, "changed": False}


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Ansible's !unsafe tag."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _play(repo_root):
    (play,) = yaml.load(
        (repo_root / "playbooks" / PLAYBOOK).read_text(), Loader=_Loader
    )
    return play


def _tasks(repo_root):
    flat = []
    for task in _play(repo_root)["tasks"]:
        flat.append(task)
        flat.extend(task.get("block", []))
    return flat


def _task(repo_root, name):
    (task,) = [t for t in _tasks(repo_root) if t["name"].startswith(name)]
    return task


def _env():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    # the Ansible filters and tests the expressions use
    env.filters["bool"] = lambda v: (
        str(v).strip().lower() in ("true", "1", "yes", "on")
        if isinstance(v, str)
        else bool(v)
    )
    env.filters["from_json"] = json.loads
    env.tests["skipped"] = lambda v: isinstance(v, dict) and bool(v.get("skipped"))
    env.tests["none"] = lambda v: v is None
    return env


def _when(repo_root, name):
    when = _task(repo_root, name)["when"]  # a list is and-ed, as Ansible does
    return " and ".join(f"({w})" for w in when) if isinstance(when, list) else when


def _before(iops=None, tags=None, group="", group_iops=None, group_tags=None):
    has_qos = bool(group) and group_iops is not None
    return {
        "iops_limit": iops,
        "bw_limit": None,
        "tags": tags or {},
        "volume_group": group,
        "group_qos": {"iops_limit": group_iops, "bw_limit": None} if group else {},
        "group_tags": group_tags or {},
        "group_has_qos": has_qos,
    }


def _applied(repo_root, **context):
    expr = _task(repo_root, "Work out the limit in force")["ansible.builtin.set_fact"][
        "applied_iops"
    ]
    base = {
        "group_capped": SKIPPED,
        "group_lifted": SKIPPED,
        "refused": "",
        "cap_iops": 500,
    }
    base.update(context)
    return _env().from_string(expr).render(**base).strip()


def _target(repo_root, mode, before, scope="notify"):
    expr = _task(repo_root, "Decide the target of this run")[
        "ansible.builtin.set_fact"
    ]["cap_target"]
    return (
        _env()
        .from_string(expr)
        .render(mode=mode, before=before, qos_cap_scope=scope)
        .strip()
    )


@pytest.mark.parametrize(
    "previous, cap, expected",
    [
        (None, 500, True),  # no limit on the volume: cap it
        (0, 500, True),  # module's "no limit"
        (NO_LIMIT, 500, True),  # array's "no limit"
        (1000, 500, True),  # looser than the cap: tighten
        (500, 500, False),  # already at the cap
        (400, 500, False),  # already tighter: keep it
    ],
)
def test_cap_task_runs_only_when_it_tightens(repo_root, previous, cap, expected):
    when = _when(repo_root, "Set the IOPS limit")
    rendered = (
        _env()
        .from_string("{{ (" + when + ") }}")
        .render(before=_before(iops=previous), cap_iops=cap)
    )
    assert rendered == str(expected)


@pytest.mark.parametrize(
    "previous, cap, applied",
    [(None, 500, 500), (NO_LIMIT, 500, 500), (1000, 500, 500), (400, 500, 400)],
)
def test_applied_iops_is_the_limit_in_force_after_a_cap(
    repo_root, previous, cap, applied
):
    assert _applied(
        repo_root,
        mode="cap",
        cap_target="volume",
        cap_iops=cap,
        before=_before(iops=previous),
    ) == str(applied)


def test_lift_restores_the_tagged_limit(repo_root):
    for tagged, applied in (("1000", 1000), ("none", 0), (str(NO_LIMIT), 0)):
        assert _applied(
            repo_root,
            mode="lift",
            cap_target="volume",
            before=_before(iops=500, tags={"qos_cap": "t", "qos_prev_iops": tagged}),
        ) == str(applied)


def _whens(task):
    when = task.get("when", [])
    return when if isinstance(when, list) else [when]


def test_cap_and_lift_are_idempotent_on_the_tag(repo_root):
    for name in ("Tag the volume first", "Set the IOPS limit"):
        assert "before.tags.qos_cap is not defined" in _whens(
            _task(repo_root, name)
        ), name
    for name in ("Restore the previous IOPS limit", "Remove the cap tags"):
        assert "before.tags.qos_cap is defined" in _whens(_task(repo_root, name)), name
    group_cap = _task(repo_root, "Cap the volume group instead")
    assert "before.group_tags.qos_cap is not defined" in group_cap["when"]
    assert "cap_target == 'vgroup'" in group_cap["when"]
    lift_when = _whens(_task(repo_root, "Lift the volume group's cap"))
    assert "mode == 'lift' and cap_target == 'vgroup'" in lift_when
    assert "(before.group_tags.qos_cap_volume | default(volume)) == volume" in lift_when
    # only the member the cap named lifts the group
    env = _env()
    tags = {"qos_cap": "t", "qos_prev_iops": "50000", "qos_cap_volume": "db/data"}
    for vol, lifts in (("db/data", True), ("db/log", False)):
        rendered = env.from_string(
            "{{ " + _when(repo_root, "Lift the volume group's cap") + " }}"
        ).render(
            mode="lift",
            cap_target="vgroup",
            volume=vol,
            before=_before(iops=None, group="db", group_iops=500, group_tags=tags),
        )
        assert rendered == str(lifts), vol


def test_applied_iops_when_the_run_changes_nothing(repo_root):
    # a second lift finds no tag: the limit it reads stays (not "cleared")
    assert (
        _applied(repo_root, mode="lift", cap_target="volume", before=_before(iops=1000))
        == "1000"
    )
    # a second cap finds the tag: the limit it reads stays
    assert (
        _applied(
            repo_root,
            mode="cap",
            cap_target="volume",
            before=_before(iops=500, tags={"qos_cap": "t", "qos_prev_iops": "1000"}),
        )
        == "500"
    )
    # nothing capped (scope notify on a QoS'd group member): the volume's limit stays
    assert (
        _applied(
            repo_root,
            mode="cap",
            cap_target="none",
            before=_before(iops=None, group="db", group_iops=50000),
            refused="scope notify",
        )
        == "None"
    )


def test_lift_restores_through_the_module_and_clears_through_the_api(repo_root):
    restore = _task(repo_root, "Restore the previous IOPS limit")
    clear = _task(repo_root, "Remove the limit the cap added")
    assert "everpure.flasharray.purefa_volume" in restore
    assert "ansible.builtin.command" in clear
    assert 'Qos.construct(iops_limit="")' in clear["ansible.builtin.command"]["stdin"]
    assert (
        clear.get("no_log") is True
        and clear["environment"]["FA_TOKEN"] == "{{ api_token }}"
    )
    # the two are complementary on the previous-limit tag, both need the cap tag
    env = _env()
    for prev, restores in (
        ("1000", True),
        ("none", False),
        ("100000000", False),
        ("", False),
    ):
        before = _before(iops=500, tags={"qos_cap": "t", "qos_prev_iops": prev})
        r = env.from_string(
            "{{ " + _when(repo_root, "Restore the previous IOPS limit") + " }}"
        ).render(before=before)
        c = env.from_string(
            "{{ " + _when(repo_root, "Remove the limit the cap added") + " }}"
        ).render(before=before)
        assert (r, c) == (str(restores), str(not restores)), prev
    untagged = _before(iops=500)
    for name in (
        "Restore the previous IOPS limit",
        "Remove the limit the cap added",
        "Remove the cap tags",
    ):
        assert (
            env.from_string("{{ " + _when(repo_root, name) + " }}").render(
                before=untagged
            )
            == "False"
        ), name


def test_the_target_follows_the_scope_for_a_cap_and_the_tags_for_a_lift(repo_root):
    plain = _before(iops=None)
    member_free = _before(iops=None, group="db", group_iops=None)
    member_qos = _before(iops=None, group="db", group_iops=50000)
    for before in (plain, member_free):
        for scope in ("notify", "vgroup", "volume"):
            assert _target(repo_root, "cap", before, scope) == "volume"
    assert _target(repo_root, "cap", member_qos, "notify") == "none"
    assert _target(repo_root, "cap", member_qos, "vgroup") == "vgroup"
    assert _target(repo_root, "cap", member_qos, "volume") == "volume"
    group_capped = _before(
        iops=None,
        group="db",
        group_iops=500,
        group_tags={
            "qos_cap": "t",
            "qos_prev_iops": "50000",
            "qos_cap_volume": "db/data",
        },
    )
    assert _target(repo_root, "lift", group_capped) == "vgroup"
    assert _target(repo_root, "lift", member_qos) == "volume"
    assert _target(repo_root, "lift", plain) == "volume"
    refuse = _task(repo_root, "Refuse an unknown scope")
    assert (
        "qos_cap_scope in ['notify', 'vgroup', 'volume']"
        in refuse["ansible.builtin.assert"]["that"]
    )
    assert _play(repo_root)["vars"]["qos_cap_scope"] == "notify"


def test_a_qos_group_member_is_never_tagged_or_capped_as_a_volume_except_on_request(
    repo_root,
):
    env = _env()
    member_qos = _before(iops=None, group="db", group_iops=50000)
    for name in ("Tag the volume first", "Set the IOPS limit"):
        rendered = env.from_string("{{ " + _when(repo_root, name) + " }}").render(
            before=member_qos, cap_iops=500
        )
        assert rendered == "False", name
    attempt = _task(repo_root, "Try the volume cap anyway")
    assert attempt.get("failed_when") is False
    assert attempt["register"] == "refused_cap"
    assert (
        env.from_string(
            "{{ " + _when(repo_root, "Try the volume cap anyway") + " }}"
        ).render(before=member_qos)
        == "True"
    )
    assert (
        env.from_string(
            "{{ " + _when(repo_root, "Try the volume cap anyway") + " }}"
        ).render(before=_before(iops=None))
        == "False"
    )


def test_the_group_cap_and_lift_keep_their_state_on_the_group_through_the_api(
    repo_root,
):
    cap = _task(repo_root, "Cap the volume group instead")
    stdin = cap["ansible.builtin.command"]["stdin"]
    assert "put_volume_groups_tags_batch" in stdin and "patch_volume_groups" in stdin
    for key in ('key="qos_cap"', 'key="qos_prev_iops"', 'key="qos_cap_volume"'):
        assert key in stdin, key
    # tag first, then the limit; the limit only tightens
    assert stdin.index("put_volume_groups_tags_batch") < stdin.index(
        "patch_volume_groups"
    )
    assert 'in ("none", "0", "100000000") or int(prev) > cap' in stdin
    assert (
        cap.get("no_log") is True
        and cap["environment"]["FA_TOKEN"] == "{{ api_token }}"
    )
    assert cap["environment"]["GROUP"] == "{{ before.volume_group }}"
    lift = _task(repo_root, "Lift the volume group's cap")
    stdin = lift["ansible.builtin.command"]["stdin"]
    assert (
        'Qos.construct(iops_limit="")' in stdin and "delete_volume_groups_tags" in stdin
    )
    assert '"qos_cap", "qos_prev_iops", "qos_cap_volume"' in stdin
    assert lift.get("no_log") is True
    assert (
        lift["environment"]["PREV"]
        == "{{ before.group_tags.qos_prev_iops | default('none') }}"
    )
    # applied_iops on a group cap or lift is what the script reports
    assert (
        _applied(
            repo_root,
            mode="cap",
            cap_target="vgroup",
            before=_before(iops=None, group="db", group_iops=50000),
            group_capped={
                "changed": True,
                "stdout": '{"tagged": true, "capped": true, "applied_iops": 500}',
            },
        )
        == "500"
    )
    assert (
        _applied(
            repo_root,
            mode="lift",
            cap_target="vgroup",
            before=_before(iops=None, group="db", group_iops=500),
            group_lifted={
                "changed": True,
                "stdout": '{"restored": true, "applied_iops": 50000}',
            },
        )
        == "50000"
    )
    # a group already tagged: the cap is skipped and the group's limit stays
    assert (
        _applied(
            repo_root,
            mode="cap",
            cap_target="vgroup",
            before=_before(
                iops=None, group="db", group_iops=500, group_tags={"qos_cap": "t"}
            ),
        )
        == "500"
    )


def test_the_record_and_the_artifacts_say_where_the_cap_went(repo_root):
    record = _task(repo_root, "Record the action")["ansible.builtin.lineinfile"]["line"]
    for key in (
        "'volume_group'",
        "'scope'",
        "'cap_target'",
        "'refused'",
        "'previous_group_iops_limit'",
    ):
        assert key in record, key
    stats = _task(repo_root, "Publish the outcome as job artifacts")[
        "ansible.builtin.set_stats"
    ]["data"]
    for key in (
        "'cap_target'",
        "'volume_group'",
        "'refused'",
        "'applied_iops'",
        "'previous_iops'",
    ):
        assert key in stats, key
    refused = _task(repo_root, "Note a refusal")["ansible.builtin.set_fact"]["refused"]
    env = _env()
    text = env.from_string(refused).render(
        mode="cap",
        cap_target="none",
        volume="db/data",
        before=_before(iops=None, group="db", group_iops=50000),
        refused_cap=SKIPPED,
    )
    assert "member of volume group db" in text and "nothing was capped" in text
    text = env.from_string(refused).render(
        mode="cap",
        cap_target="volume",
        volume="db/data",
        before=_before(iops=None, group="db", group_iops=50000),
        refused_cap={
            "changed": False,
            "failed": False,  # failed_when false: the refusal is a run that changed nothing
            "msg": "Cannot set rate limits on individual volumes inside volume groups with rate limit.",
        },
    )
    assert text.strip().startswith("Cannot set rate limits")
    # an attempt the array allowed (changed) is no refusal
    assert (
        env.from_string(refused)
        .render(
            mode="cap",
            cap_target="volume",
            volume="v",
            before=_before(iops=None),
            refused_cap={"changed": True},
        )
        .strip()
        == ""
    )
    assert (
        env.from_string(refused)
        .render(
            mode="cap",
            cap_target="volume",
            volume="v",
            before=_before(iops=None),
            refused_cap=SKIPPED,
        )
        .strip()
        == ""
    )
