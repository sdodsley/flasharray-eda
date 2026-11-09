"""qos_cap.yml never loosens a limit: the cap task is skipped when the volume's
own IOPS limit is already at or below cap_iops, and applied_iops reports the
limit in force. The playbook is loaded as YAML and the expressions rendered with
Jinja2 the way Ansible would (a `when` is an expression, the artifact a template)."""

import pytest
import yaml

PLAYBOOK = "qos_cap.yml"
NO_LIMIT = 100000000


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Ansible's !unsafe tag."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _tasks(repo_root):
    (play,) = yaml.load(
        (repo_root / "playbooks" / PLAYBOOK).read_text(), Loader=_Loader
    )
    flat = []
    for task in play["tasks"]:
        flat.append(task)
        flat.extend(task.get("block", []))
    return flat


def _env():
    jinja2 = pytest.importorskip("jinja2")
    return jinja2.Environment(undefined=jinja2.StrictUndefined)


def _when(repo_root, name):
    (task,) = [t for t in _tasks(repo_root) if t["name"].startswith(name)]
    when = task["when"]  # a list is and-ed, as Ansible does
    return " and ".join(f"({w})" for w in when) if isinstance(when, list) else when


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
        .render(before={"iops_limit": previous, "tags": {}}, cap_iops=cap)
    )
    assert rendered == str(expected)


@pytest.mark.parametrize(
    "previous, cap, applied",
    [(None, 500, 500), (NO_LIMIT, 500, 500), (1000, 500, 500), (400, 500, 400)],
)
def test_applied_iops_is_the_limit_in_force_after_a_cap(
    repo_root, previous, cap, applied
):
    (stats,) = [t for t in _tasks(repo_root) if "ansible.builtin.set_stats" in t]
    data = (
        _env()
        .from_string(stats["ansible.builtin.set_stats"]["data"])
        .render(
            mode="cap",
            inventory_hostname="a",
            volume="v",
            cap_iops=cap,
            before={"iops_limit": previous, "tags": {}},
            capped={"changed": True},
            lifted={},
            cleared={},
        )
    )
    assert f"'applied_iops': {applied}," in data.replace("\n", " ")


def test_lift_restores_the_tagged_limit(repo_root):
    (stats,) = [t for t in _tasks(repo_root) if "ansible.builtin.set_stats" in t]
    for tagged, applied in (("1000", 1000), ("none", 0), (str(NO_LIMIT), 0)):
        data = (
            _env()
            .from_string(stats["ansible.builtin.set_stats"]["data"])
            .render(
                mode="lift",
                inventory_hostname="a",
                volume="v",
                before={
                    "iops_limit": 500,
                    "tags": {"qos_cap": "t", "qos_prev_iops": tagged},
                },
                capped={},
                lifted={"changed": True},
                cleared={},
            )
        )
        assert f"'applied_iops': {applied}," in data.replace("\n", " ")


def _whens(task):
    when = task.get("when", [])
    return when if isinstance(when, list) else [when]


def test_cap_and_lift_are_idempotent_on_the_tag(repo_root):
    tasks = _tasks(repo_root)
    for name in ("Tag the volume first", "Set the IOPS limit"):
        (task,) = [t for t in tasks if t["name"].startswith(name)]
        assert "before.tags.qos_cap is not defined" in _whens(task), name
    for name in ("Restore the previous IOPS limit", "Remove the cap tags"):
        (task,) = [t for t in tasks if t["name"].startswith(name)]
        assert "before.tags.qos_cap is defined" in _whens(task), name


def test_applied_iops_when_the_run_changes_nothing(repo_root):
    (stats,) = [t for t in _tasks(repo_root) if "ansible.builtin.set_stats" in t]
    template = stats["ansible.builtin.set_stats"]["data"]
    # a second lift finds no tag: the limit it reads stays (not "cleared")
    data = (
        _env()
        .from_string(template)
        .render(
            mode="lift",
            inventory_hostname="a",
            volume="v",
            before={"iops_limit": 1000, "tags": {}},
            capped={},
            lifted={},
            cleared={},
        )
    )
    assert "'applied_iops': 1000," in data.replace("\n", " ")
    # a second cap finds the tag: the limit it reads stays
    data = (
        _env()
        .from_string(template)
        .render(
            mode="cap",
            inventory_hostname="a",
            volume="v",
            cap_iops=500,
            before={
                "iops_limit": 500,
                "tags": {"qos_cap": "t", "qos_prev_iops": "1000"},
            },
            capped={},
            lifted={},
            cleared={},
        )
    )
    assert "'applied_iops': 500," in data.replace("\n", " ")


def test_lift_restores_through_the_module_and_clears_through_the_api(repo_root):
    tasks = _tasks(repo_root)
    (restore,) = [
        t for t in tasks if t["name"].startswith("Restore the previous IOPS limit")
    ]
    (clear,) = [
        t for t in tasks if t["name"].startswith("Remove the limit the cap added")
    ]
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
        before = {"iops_limit": 500, "tags": {"qos_cap": "t", "qos_prev_iops": prev}}
        r = env.from_string(
            "{{ " + _when(repo_root, "Restore the previous IOPS limit") + " }}"
        ).render(before=before)
        c = env.from_string(
            "{{ " + _when(repo_root, "Remove the limit the cap added") + " }}"
        ).render(before=before)
        assert (r, c) == (str(restores), str(not restores)), prev
    untagged = {"iops_limit": 500, "tags": {}}
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
