"""The replication rulebook's rules for a schedule switched off: a group with
a target and no replication schedule is notified at critical once an hour, a
group with members and no snapshot schedule at warning once a day, both silent
inside a maintenance window; the overdue rules are untouched; purepgroup is
among the example governed commands."""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _rules(path):
    doc = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return {r["name"]: r for rs in doc for r in rs.get("rules", [])}


def test_disabled_schedules_are_notified_and_the_overdue_rules_are_unchanged():
    rules = _rules("extensions/eda/rulebooks/replication.yml")
    crit = rules["Protection group replication schedule disabled with targets - notify"]
    assert "schedule_disabled_with_targets == true" in crit["condition"]
    assert "maintenance_active == false" in crit["condition"]
    assert crit["throttle"]["once_within"] == "1 hour"
    assert crit["throttle"]["group_by_attributes"] == [
        "event.purefa_pg_replication.array",
        "event.purefa_pg_replication.pgroup",
    ]
    extra = crit["action"]["run_playbook"]["extra_vars"]
    assert crit["action"]["run_playbook"]["name"] == "playbooks/notify.yml"
    assert extra["severity"] == "critical"
    assert "replication schedule disabled" in extra["subject"]
    warn = rules["Protection group snapshot schedule disabled with members - notify"]
    assert "snapshot_schedule_disabled == true" in warn["condition"]
    assert "maintenance_active == false" in warn["condition"]
    assert warn["throttle"]["once_within"] == "24 hours"
    assert warn["action"]["run_playbook"]["extra_vars"]["severity"] == "warning"
    for name in (
        "Protection group replication overdue or stuck - replicate now",
        "Protection group replication overdue or stuck - hand the event to the orchestrator",
        "Protection group replication overdue or stuck - notify only",
    ):
        assert "schedule_disabled" not in rules[name]["condition"]
        assert "overdue == true" in rules[name]["condition"]


def test_only_a_group_with_members_is_replicated_or_handed_on():
    for path in (
        "extensions/eda/rulebooks/replication.yml",
        "extensions/eda/rulebooks/platform/replication.yml",
    ):
        rules = _rules(path)
        for name in (
            "Protection group replication overdue or stuck - replicate now",
            "Protection group replication overdue or stuck - hand the event to the orchestrator",
        ):
            assert "member_count > 0" in rules[name]["condition"], (path, name)
        notify = rules["Protection group replication overdue or stuck - notify only"]
        assert "member_count == 0" in notify["condition"], path


def test_the_platform_rulebook_carries_the_same_two_rules():
    rules = _rules("extensions/eda/rulebooks/platform/replication.yml")
    for name in (
        "Protection group replication schedule disabled with targets - notify",
        "Protection group snapshot schedule disabled with members - notify",
    ):
        job = rules[name]["action"]["run_job_template"]
        assert job["name"] == "FlashArray - notify"


def test_purepgroup_is_governed_and_its_destructive_subcommands_still_notify():
    """purepgroup is in both example lists; ansible-rulebook takes the first
    matching rule, so the destructive rule must sit before the governed ones
    in both forms of the change-control rulebook."""
    vars_ = yaml.safe_load((ROOT / "vars/example.yml").read_text(encoding="utf-8"))
    assert "purepgroup" in vars_["governed_commands"]
    assert "purepgroup" in vars_["destructive_commands"]
    for path in (
        "extensions/eda/rulebooks/change_control.yml",
        "extensions/eda/rulebooks/platform/change_control.yml",
    ):
        names = list(_rules(path))
        assert names.index("Destructive command by a human - notify") < names.index(
            "Governed setting changed by a human - reconcile from the baseline"
        ), path
