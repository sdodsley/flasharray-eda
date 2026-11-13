# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Static checks over the controller schedules aap/seed.yml creates and the
playbooks they launch: the nightly DR drill (dr_drill.yml fails the job when
told and a drill failed, after publishing its artifacts), the weekly
notification-path self-check (self_check.yml) and the nightly definitions
drift check (definitions_check.yml, which carries the seed's rendering of
the activation vars); the iCal rules render from the example vars; and the
platform rulebooks carry no tick-only ruleset, because the schedule took the
drill over."""

import pathlib
import re

import jinja2
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED = ROOT / "aap" / "seed.yml"
EXAMPLE = ROOT / "aap" / "vars" / "example.yml"
PLATFORM = ROOT / "extensions" / "eda" / "rulebooks" / "platform"
SCHEDULE_MODULE = "ansible.controller.schedule"
SET_STATS = "ansible.builtin.set_stats"
SCHEDULE_NAMES = {
    "FlashArray - DR drill (nightly)": "FlashArray - DR drill",
    "FlashArray - self check (weekly)": "FlashArray - self check",
    "FlashArray - definitions check (nightly)": "FlashArray - definitions check",
}
RRULE = re.compile(
    r"^DTSTART;TZID=UTC:20260101T\d{6} RRULE:FREQ=(DAILY|WEEKLY);INTERVAL=1"
    r"(;BYDAY=(MO|TU|WE|TH|FR|SA|SU))?$"
)


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Ansible's !unsafe tag."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _load(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def _seed():
    return _load(SEED)[0]


def _playbook(name):
    return _load(ROOT / "playbooks" / name)


def _when(task):
    when = task.get("when")
    return " and ".join(when) if isinstance(when, list) else str(when or "")


def _render(text, context):
    """Render Jinja text with the seed's vars laid over the example vars; a
    var whose value is Jinja itself (schedule_dtstart) takes a second pass."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for _ in range(3):
        if "{{" not in text:
            break
        text = env.from_string(text).render(**context)
    return text


def _context():
    play = _seed()
    context = dict(_load(EXAMPLE))
    context.update(play["vars"])
    return context


def test_seed_creates_the_three_schedules_on_seeded_templates():
    play = _seed()
    schedules = play["vars"]["schedules"]
    assert {s["name"]: s["template"] for s in schedules} == SCHEDULE_NAMES
    templates = {t["name"] for t in play["vars"]["job_templates"]}
    for schedule in schedules:
        assert schedule["template"] in templates, schedule["name"]
        assert schedule["description"], schedule["name"]
        assert "enabled" in schedule, schedule["name"]
    task = next(t for t in play["tasks"] if SCHEDULE_MODULE in t)
    args = task[SCHEDULE_MODULE]
    assert task["loop"] == "{{ schedules }}"
    assert args["unified_job_template"] == "{{ item.template }}"
    assert args["rrule"] == "{{ item.rrule }}"
    assert args["extra_data"] == "{{ item.extra_data | default({}) }}"
    assert args["enabled"] == "{{ item.enabled | bool }}"
    assert args["state"] == "present"
    # the schedules come after the templates they sit on
    names = [t.get("name") for t in play["tasks"]]
    assert names.index("Schedules") > names.index("One job template per playbook")


def test_schedule_rules_render_to_icalendar_from_the_example_vars():
    context = _context()
    rendered = {}
    for schedule in _seed()["vars"]["schedules"]:
        rule = _render(schedule["rrule"], context)
        assert RRULE.match(rule), f"{schedule['name']}: {rule}"
        rendered[schedule["name"]] = rule
        assert _render(str(schedule["enabled"]), context) == "True"
    assert "FREQ=DAILY" in rendered["FlashArray - DR drill (nightly)"]
    assert "FREQ=DAILY" in rendered["FlashArray - definitions check (nightly)"]
    assert (
        "FREQ=WEEKLY;INTERVAL=1;BYDAY=" in rendered["FlashArray - self check (weekly)"]
    )
    # the example times are HH:MM and the weekday an iCal code
    example = _load(EXAMPLE)
    for key in (
        "aap_dr_drill_schedule",
        "aap_self_check_schedule",
        "aap_definitions_check_schedule",
    ):
        assert re.match(r"^\d\d:\d\d$", example[key]["at"]), key
        assert example[key]["enabled"] is True, key
    assert example["aap_self_check_schedule"]["day"] in {
        "MO",
        "TU",
        "WE",
        "TH",
        "FR",
        "SA",
        "SU",
    }


def test_the_drill_schedule_fails_the_job_and_the_playbook_publishes_first():
    schedule = next(
        s
        for s in _seed()["vars"]["schedules"]
        if s["name"] == "FlashArray - DR drill (nightly)"
    )
    assert schedule["extra_data"] == {"dr_drill_fail_job": True}
    tasks = _playbook("dr_drill.yml")[0]["tasks"]
    last = tasks[-1]
    assert "block" in last and "always" in last
    fail = last["block"][0]
    assert "ansible.builtin.fail" in fail
    assert "dr_drill_fail_job | default(false) | bool" in _when(fail)
    assert "'failed'" in _when(fail)
    assert SET_STATS in last["always"][-1]
    # the orchestrator's nightly definition judges failed_count itself and
    # must not pass the flag
    definition = (ROOT / "orchestrator" / "dr_drill_schedule.json").read_text()
    assert "dr_drill_fail_job" not in definition


def test_self_check_template_and_playbook_agree():
    template = next(
        t
        for t in _seed()["vars"]["job_templates"]
        if t["name"] == "FlashArray - self check"
    )
    assert template["playbook"] == "playbooks/self_check.yml"
    play = _playbook("self_check.yml")[0]
    assert play["hosts"] == "all:!localhost"
    for key in template["extra_vars"]:
        assert (
            key in play["vars"]
        ), f"template passes {key}, the playbook has no default"
    assert set(play["vars"]) == {
        "self_check_tests",
        "self_check_alert_watchers",
        "self_check_retry_s",
        "self_check_fail_job",
    }
    tasks = play["tasks"]
    assert tasks[0]["ansible.builtin.meta"] == "end_host"
    test = next(t for t in tasks if "ansible.builtin.command" in t)
    assert test.get("no_log") is True
    assert test.get("changed_when") is False
    assert set(test["environment"]) == {
        "FA_HOST",
        "FA_TOKEN",
        "TESTS",
        "WATCHERS",
        "RETRY_S",
    }
    stdin = test["ansible.builtin.command"]["stdin"]
    for call in (
        "get_alert_watchers_test(names=names)",
        "get_syslog_servers_test()",
        "get_support_test()",
        "get_arrays_ntp_test()",
        "get_snmp_managers_test()",
    ):
        assert call in stdin, call
    assert '"success"' in stdin and '"result_details"' in stdin
    record = next(t for t in tasks if "ansible.builtin.lineinfile" in t)
    line = record["ansible.builtin.lineinfile"]["line"]
    for key in (
        "'time'",
        "'kind': 'self_check'",
        "'severity'",
        "'array'",
        "'destination'",
    ):
        assert key in line, key
    assert record["delegate_to"] == "localhost"
    assert record.get("throttle") == 1  # the arrays share one alert log
    last = tasks[-1]
    fail = last["block"][0]
    assert "ansible.builtin.fail" in fail
    assert "self_check_fail_job | bool" in _when(fail)
    assert SET_STATS in last["always"][-1]


def test_definitions_check_carries_the_seeds_rendering_and_writes_nothing():
    play = _seed()
    template = next(
        t
        for t in play["vars"]["job_templates"]
        if t["name"] == "FlashArray - definitions check"
    )
    assert template["playbook"] == "playbooks/definitions_check.yml"
    extra = template["extra_vars"]
    assert extra["expected_activation_vars"] == "{{ activation_site_vars }}"
    assert extra["activation_organization"] == "{{ aap_organization }}"
    for key in (
        "orchestrator_project",
        "orchestrator_aap_credential",
        "orchestrator_aap_integration",
        "orchestrator_service_account",
    ):
        assert extra[key] == "{{ aap_" + key + " }}", key
    assert "platform_token_credential_name" in template["credentials"]
    assert "orchestrator_credential_name" in template["credentials"]
    # the rendering exists before the templates are created
    names = [t.get("name") for t in play["tasks"]]
    assert names.index(
        "Credentials and extra vars every activation carries"
    ) < names.index("One job template per playbook")
    assert names.index(
        "Refuse example placeholders in the extra vars the activations would carry"
    ) < names.index("One job template per playbook")
    # the platform token credential: the built-in type, token from the named
    # environment variable, never logged
    credential = next(
        t
        for t in play["tasks"]
        if t.get("name") == "Platform token credential for the definitions check"
    )
    args = credential["ansible.controller.credential"]
    assert args["credential_type"] == "Red Hat Ansible Automation Platform"
    assert set(args["inputs"]) == {"host", "oauth_token", "verify_ssl"}
    assert (
        args["inputs"]["oauth_token"] == "{{ lookup('env', aap_platform_token_env) }}"
    )
    assert credential.get("no_log") is True
    # the playbook compares and never writes
    doc = _playbook("definitions_check.yml")[0]
    text = (ROOT / "playbooks" / "definitions_check.yml").read_text()
    assert set(doc["vars"]) >= {
        "definitions_check_fail_job",
        "orchestrator_project",
        "orchestrator_aap_credential",
        "orchestrator_aap_integration",
        "orchestrator_service_account",
        "expected_activation_vars",
        "activation_organization",
    }
    # no backslash inside a Jinja expression: Ansible doubles them in block
    # scalars, so a regex escape there never matches
    assert "\\" not in text
    commands = [t for t in doc["tasks"] if "ansible.builtin.command" in t]
    assert len(commands) == 2
    for task in commands:
        assert task.get("failed_when") is False, task["name"]
        assert task.get("changed_when") is False, task["name"]
    sync, audit = commands
    assert "ao_sync.py" in sync["ansible.builtin.command"]["argv"]
    assert "'--check'" in sync["ansible.builtin.command"]["argv"]
    assert "--dry-run" not in sync["ansible.builtin.command"]["argv"]
    assert "activation_audit.py" in audit["ansible.builtin.command"]["argv"]
    assert "'--json'" in audit["ansible.builtin.command"]["argv"]
    assert "--recreate" not in audit["ansible.builtin.command"]["argv"]
    assert "everpure.flasharray" not in text and "api_token" not in text
    record = next(t for t in doc["tasks"] if "ansible.builtin.lineinfile" in t)
    assert "'kind': 'definitions_check'" in record["ansible.builtin.lineinfile"]["line"]
    last = doc["tasks"][-1]
    assert "definitions_check_fail_job | bool" in _when(last["block"][0])
    assert SET_STATS in last["always"][-1]


def test_platform_rulebooks_leave_the_tick_ruleset_to_the_schedule():
    standalone = _load(ROOT / "extensions" / "eda" / "rulebooks" / "replication.yml")
    tick_rulesets = [
        rs
        for rs in standalone
        if all("ansible.eda.tick" in entry for entry in rs["sources"])
    ]
    assert len(tick_rulesets) == 1, "the standalone drill ruleset is the tick one"
    for path in sorted(PLATFORM.glob("*.yml")):
        for ruleset in _load(path):
            for entry in ruleset["sources"]:
                assert "ansible.eda.tick" not in entry, f"{path.name}: tick source kept"


def test_seed_variables_for_the_schedules_are_documented():
    example = _load(EXAMPLE)
    for key in (
        "aap_schedule_timezone",
        "aap_self_check_tests",
        "aap_self_check_alert_watchers",
        "aap_platform_token_env",
        "aap_orchestrator_project",
        "aap_orchestrator_aap_credential",
        "aap_orchestrator_aap_integration",
        "aap_orchestrator_service_account",
    ):
        assert key in example, key
    assert example["aap_platform_token_env"] == "AAP_TOKEN"
    assert set(example["aap_self_check_tests"]) <= {
        "alert_watchers",
        "syslog_servers",
        "support",
        "ntp",
        "snmp_managers",
    }


def _assert_task(play, prefix):
    return next(t for t in play["tasks"] if t.get("name", "").startswith(prefix))


def test_seed_refuses_an_empty_token_a_bad_time_and_unset_orchestrator_names():
    play = _seed()
    names = [t.get("name", "") for t in play["tasks"]]
    first_object = names.index("Organization")
    token = _assert_task(play, "Refuse to run without the token the platform")
    assert (
        "lookup('env', aap_platform_token_env) | length > 0"
        in token["ansible.builtin.assert"]["that"]
    )
    times = _assert_task(play, "Refuse a schedule time that is not")
    that = times["ansible.builtin.assert"]["that"]
    assert "item.value.at is string" in that
    pattern = next(c for c in that if "is match(" in c)
    regex = re.search(r"is match\('([^']+)'\)", pattern).group(1)
    for good in ("00:00", "02:00", "23:59"):
        assert re.match(regex, good), good
    for bad in ("1410", "2:00", "24:00", "02:60", "0200"):
        assert not re.match(regex, bad), bad
    assert "'MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'" in "\n".join(that)
    for key in (
        "aap_dr_drill_schedule",
        "aap_self_check_schedule",
        "aap_definitions_check_schedule",
    ):
        assert key in times["loop"], key
    orchestrator = _assert_task(play, "Refuse unset orchestrator names")
    assert "orchestrator_seeded | bool" in orchestrator["when"]
    assert "aap_definitions_check_schedule.enabled | bool" in orchestrator["when"]
    for key in (
        "aap_orchestrator_aap_credential",
        "aap_orchestrator_aap_integration",
        "aap_orchestrator_service_account",
    ):
        assert f"{key} | length > 0" in orchestrator["ansible.builtin.assert"]["that"]
    # every refusal comes before the first object is created
    for task in (token, times, orchestrator):
        assert names.index(task["name"]) < first_object, task["name"]


def test_multi_array_playbooks_write_the_alert_log_one_array_at_a_time():
    """lineinfile is read-modify-rename; the plays that span every array
    (hosts all:!localhost) must not let two arrays write the shared alert
    log at once."""
    for name in ("apply_baseline.yml", "self_check.yml"):
        doc = _playbook(name)
        play = next(p for p in doc if p.get("hosts") == "all:!localhost")

        def walk(tasks):
            for task in tasks:
                for key in ("block", "rescue", "always"):
                    yield from walk(task.get(key, []))
                if "ansible.builtin.lineinfile" in task:
                    yield task

        records = [
            t
            for t in walk(play["tasks"])
            if t["ansible.builtin.lineinfile"]["path"] == "{{ alert_log }}"
        ]
        assert records, name
        for task in records:
            assert task.get("throttle") == 1, f"{name}: {task['name']} not throttled"
