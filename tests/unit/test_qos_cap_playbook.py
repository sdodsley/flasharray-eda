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
    return task["when"]


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
                before={"iops_limit": 500, "tags": {"qos_prev_iops": tagged}},
                capped={},
                lifted={"changed": True},
            )
        )
        assert f"'applied_iops': {applied}," in data.replace("\n", " ")
