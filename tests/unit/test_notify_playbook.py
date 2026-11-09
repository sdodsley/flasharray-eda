"""playbooks/notify.yml writes every record to the alert log and posts to
Slack only at or above the severity floor (default warning)."""

import pytest
import yaml


def _play(repo_root):
    return yaml.safe_load((repo_root / "playbooks" / "notify.yml").read_text())[0]


def _task(play, name):
    return next(t for t in play["tasks"] if t["name"] == name)


def test_slack_tasks_are_gated_on_the_decision(repo_root):
    play = _play(repo_root)
    assert play["vars"]["severity_rank"] == {"info": 0, "warning": 1, "critical": 2}
    for name in ("Format the Slack message", "Post the alert to Slack"):
        assert _task(play, name)["when"] == "slack_wanted | bool"
    report = _task(play, "Report a failed Slack post without failing the notification")
    assert report["when"].startswith("slack_wanted | bool and")
    names = [t["name"] for t in play["tasks"]]
    assert names.index("Settle the Slack severity floor") < names.index(
        "Decide whether this record reaches Slack"
    )
    assert names.index("Decide whether this record reaches Slack") < names.index(
        "Format the Slack message"
    )
    # the floor comes from an extra var, else the environment, else warning
    floor = _task(play, "Settle the Slack severity floor")["ansible.builtin.set_fact"]
    assert "slack_min_severity | default(" in floor["slack_floor"]
    assert "SLACK_MIN_SEVERITY" in floor["slack_floor"]
    assert "'warning'" in floor["slack_floor"]
    # the record is logged whatever the floor says, and the artifacts still close the play
    assert "when" not in _task(play, "Append to the alert log")
    assert "ansible.builtin.set_stats" in play["tasks"][-1]


@pytest.mark.parametrize(
    "webhook, severity, floor, wanted",
    [
        ("https://hooks.example/x", "info", "warning", False),
        ("https://hooks.example/x", "warning", "warning", True),
        ("https://hooks.example/x", "critical", "warning", True),
        ("https://hooks.example/x", "info", "info", True),
        ("https://hooks.example/x", "warning", "critical", False),
        (
            "https://hooks.example/x",
            "odd",
            "warning",
            True,
        ),  # unknown severity is posted
        (
            "https://hooks.example/x",
            "warning",
            "",
            True,
        ),  # an empty floor keeps nothing out
        ("", "critical", "info", False),  # no webhook, nothing to post to
    ],
)
def test_decision_renders(repo_root, webhook, severity, floor, wanted):
    jinja2 = pytest.importorskip("jinja2")
    play = _play(repo_root)
    expr = _task(play, "Decide whether this record reaches Slack")[
        "ansible.builtin.set_fact"
    ]["slack_wanted"]
    # Ansible's default undefined lets default() stand in for a missing key
    env = jinja2.Environment(undefined=jinja2.Undefined)
    out = env.from_string(expr).render(
        slack_webhook=webhook,
        alert_record={"severity": severity},
        slack_floor=floor,
        severity_rank=play["vars"]["severity_rank"],
    )
    assert out.strip() == str(wanted)
