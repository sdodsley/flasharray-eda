# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Bursts and digests. playbooks/notify.yml counts this array's earlier
notifications in the burst window (the controller's notify jobs with the
platform token, the alert log without), holds the record past the threshold
and says so in the record and the artifacts; playbooks/digest.yml posts one
Slack message per array for the held records since the previous digest; the
alerts rulebook ticks the digest standalone, the seed schedules it on the
platform and puts the platform token credential on both templates. The
playbooks are plain Ansible, so these checks are structural: the tasks,
their conditions and their expressions, rendered where plain Jinja can."""

import ast
import importlib.util
import json
import pathlib

import jinja2
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
NOTIFY = ROOT / "playbooks" / "notify.yml"
DIGEST = ROOT / "playbooks" / "digest.yml"
RULEBOOKS = ROOT / "extensions" / "eda" / "rulebooks"
SEED = ROOT / "aap" / "seed.yml"
EXAMPLE = ROOT / "aap" / "vars" / "example.yml"
GENERATOR = ROOT / "tools" / "platform_rulebooks.py"
JOBS = "/api/controller/v2/jobs/?name="
SET_STATS = "ansible.builtin.set_stats"
MODULES = ("ansible.builtin.uri", "ansible.builtin.set_fact", "ansible.builtin.debug")


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Ansible's !unsafe tag."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _load(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def _play(path):
    return _load(path)[0]


def _tasks(play):
    """Every task in run order, blocks flattened."""
    out = []

    def walk(tasks):
        for task in tasks:
            out.append(task)
            for key in ("block", "rescue", "always"):
                walk(task.get(key, []))

    walk(play["tasks"])
    return out


def _task(play, name):
    return next(t for t in _tasks(play) if t.get("name") == name)


def _when(task):
    when = task.get("when")
    return " and ".join(when) if isinstance(when, list) else str(when or "")


def _module(task):
    return next(task[k] for k in task if k.startswith("ansible."))


def _env():
    """Plain Jinja with stand-ins for the Ansible filters the expressions
    use; ChainableUndefined lets `a.b.c | default(x)` stand in for an
    unregistered task's result as Ansible's undefined does."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["bool"] = lambda v: (
        v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
    )
    env.filters["from_json"] = json.loads
    return env


def _render(expr, **context):
    return _env().from_string(expr).render(**context).strip()


def _literal(expr, **context):
    return ast.literal_eval(_render(expr, **context))


# ---------------------------------------------------------------- notify.yml
def test_notify_digest_defaults_match_the_seeds_job_extra_vars():
    play = _play(NOTIFY)
    defaults = {k: v for k, v in play["vars"].items() if k.startswith("digest_")}
    assert defaults == {
        "digest_window_minutes": 15,
        "digest_burst_threshold": 6,
        "digest_hold_critical": False,
    }
    # the platform passes the same three through every template's extra vars
    extra = _load(EXAMPLE)["aap_job_extra_vars"]
    assert {k: v for k, v in extra.items() if k.startswith("digest_")} == defaults
    assert _play(DIGEST)["vars"]["digest_window_minutes"] == 15


def test_notify_counts_the_window_before_it_appends_and_never_fails_on_it():
    play = _play(NOTIFY)
    names = [t.get("name") for t in play["tasks"]]
    count = _task(play, "Count this array's notifications in the burst window")
    assert "block" in count and "rescue" in count
    rescue = count["rescue"][0]
    assert rescue["ansible.builtin.set_fact"] == {"digest_count": 0}
    # the record carries held, so the count and the decision come before
    # the append, which still has no condition
    for before, after in (
        (
            "Build the alert record",
            "Count this array's notifications in the burst window",
        ),
        (
            "Count this array's notifications in the burst window",
            "Decide whether the burst holds this record",
        ),
        ("Decide whether the burst holds this record", "Mark the record held or not"),
        ("Mark the record held or not", "Append to the alert log"),
        (
            "Settle the Slack severity floor",
            "Decide whether the burst holds this record",
        ),
    ):
        assert names.index(before) < names.index(after), (before, after)
    assert "when" not in _task(play, "Append to the alert log")
    mark = _task(play, "Mark the record held or not")["ansible.builtin.set_fact"]
    assert (
        mark["alert_record"]
        == "{{ alert_record | combine({'held': digest_hold | bool}) }}"
    )
    # the window is now minus the window length, from the local clock
    start = _task(play, "Note where the burst window starts")[
        "ansible.builtin.set_fact"
    ]
    assert "now().timestamp()" in start["digest_window_start"]
    assert "(digest_window_minutes | int) * 60" in start["digest_window_start"]
    assert "utc=true" in start["digest_window_start"]


def test_notify_counts_the_controllers_notify_jobs_with_the_platform_token():
    play = _play(NOTIFY)
    gate = _task(play, "Note whether the platform token is at hand")
    assert (
        gate["ansible.builtin.set_fact"]["digest_from_platform"]
        == "{{ lookup('env', 'AAP_TOKEN') | length > 0 }}"
    )
    token = _task(play, "Read the platform token and address from the environment")
    assert token.get("no_log") is True
    facts = token["ansible.builtin.set_fact"]
    assert "lookup('env', 'AAP_TOKEN')" in facts["platform_token"]
    assert "lookup('env', 'AAP_HOSTNAME')" in facts["platform_url"]
    assert "lookup('env', 'AAP_VALIDATE_CERTS')" in facts["platform_verify"]
    jobs = _task(play, "Read the notify jobs of the window from the controller")
    assert _when(jobs) == "digest_from_platform | bool"
    uri = jobs["ansible.builtin.uri"]
    assert uri["url"].startswith("{{ platform_url }}" + JOBS)
    assert "{{ 'FlashArray - notify' | urlencode }}" in uri["url"]
    assert "created__gte={{ digest_window_start | urlencode }}" in uri["url"]
    assert "page_size=200" in uri["url"]
    assert uri["headers"] == {"Authorization": "Bearer {{ platform_token }}"}
    assert uri["validate_certs"] == "{{ platform_verify | bool }}"
    assert uri["status_code"] == [200]
    assert jobs.get("no_log") is True and jobs.get("failed_when") is False
    # a job without artifacts (running, failed) is not counted
    count = _task(play, "Count the notifications")["ansible.builtin.set_fact"][
        "digest_count"
    ]
    assert "selectattr('artifacts.array', 'defined')" in count
    assert "selectattr('artifacts.array', 'equalto', alert_record.array)" in count
    jobs = [
        {"id": 1, "artifacts": {"array": "array-a", "held": False}},
        {"id": 2, "artifacts": {"array": "array-b", "held": False}},
        {"id": 3, "artifacts": {}},
        {"id": 4, "artifacts": {"array": "array-a", "held": True}},
    ]
    rendered = _render(
        count,
        digest_from_platform=True,
        window_jobs={"json": {"results": jobs}},
        alert_record={"array": "array-a"},
    )
    assert rendered == "2"
    # a failed read registers no json: nothing is counted
    assert (
        _render(
            count,
            digest_from_platform=True,
            window_jobs={"status": -1},
            alert_record={"array": "array-a"},
        )
        == "0"
    )


def test_notify_counts_the_alert_logs_notify_records_without_the_token():
    play = _play(NOTIFY)
    read = _task(play, "Read the alert log")
    assert _when(read) == "not (digest_from_platform | bool) and alert_log is file"
    expr = read["ansible.builtin.set_fact"]["window_records"]
    assert "lookup('file', alert_log)" in expr and "from_json" in expr
    count = _task(play, "Count the notifications")["ansible.builtin.set_fact"][
        "digest_count"
    ]
    # only notify.yml's own records carry held; alert_runbook.yml's route
    # notify records are log-only and never a burst
    assert (
        "selectattr('kind', 'equalto', 'notify') | selectattr('held', 'defined')"
        in count
    )
    records = [
        {
            "time": "2026-01-01T00:01:00Z",
            "kind": "notify",
            "array": "array-a",
            "held": False,
        },
        {
            "time": "2026-01-01T00:02:00Z",
            "kind": "notify",
            "array": "array-a",
            "held": False,
        },
        {
            "time": "2026-01-01T00:02:30Z",
            "kind": "notify",
            "array": "array-a",
            "route": "notify",
            "ticket_key": "array-a:1:opened",
        },
        {
            "time": "2026-01-01T00:03:00Z",
            "kind": "notify",
            "array": "array-b",
            "held": False,
        },
        {
            "time": "2025-12-31T23:50:00Z",
            "kind": "notify",
            "array": "array-a",
            "held": False,
        },
        {"time": "2026-01-01T00:04:00Z", "kind": "digest", "array": "array-a"},
        {"time": "2026-01-01T00:05:00Z", "kind": "self_check", "array": "array-a"},
    ]
    rendered = _render(
        count,
        digest_from_platform=False,
        window_records=records,
        alert_record={"array": "array-a"},
        digest_window_start="2026-01-01T00:00:00Z",
    )
    assert rendered == "2"
    assert (
        _render(
            count,
            digest_from_platform=False,
            alert_record={"array": "array-a"},
            digest_window_start="x",
        )
        == "0"
    )


@pytest.mark.parametrize(
    "count, threshold, severity, hold_critical, floor, held",
    [
        (5, 6, "warning", False, "warning", False),  # under the threshold
        (6, 6, "warning", False, "warning", True),  # the threshold holds
        (9, 6, "info", False, "info", True),
        (9, 6, "critical", False, "warning", False),  # critical still pages
        (9, 6, "critical", True, "warning", True),  # unless asked
        (9, 6, "info", False, "warning", False),  # under the floor is not held
        (9, 6, "odd", False, "warning", True),  # unknown severity posts, so it is held
        (0, 6, "warning", True, "warning", False),
    ],
)
def test_notify_holds_past_the_threshold(
    count, threshold, severity, hold_critical, floor, held
):
    play = _play(NOTIFY)
    expr = _task(play, "Decide whether the burst holds this record")[
        "ansible.builtin.set_fact"
    ]["digest_hold"]
    assert (
        "digest_count | default(0)" in expr
    )  # a count that never happened holds nothing
    rendered = _render(
        expr,
        digest_count=count,
        digest_burst_threshold=threshold,
        digest_hold_critical=hold_critical,
        alert_record={"severity": severity},
        slack_floor=floor,
        severity_rank=play["vars"]["severity_rank"],
    )
    assert rendered == str(held)


@pytest.mark.parametrize("hold, wanted", [(True, False), (False, True)])
def test_notify_keeps_a_held_record_out_of_slack(hold, wanted):
    play = _play(NOTIFY)
    expr = _task(play, "Decide whether this record reaches Slack")[
        "ansible.builtin.set_fact"
    ]["slack_wanted"]
    rendered = _render(
        expr,
        slack_webhook="https://hooks.example/x",
        alert_record={"severity": "warning"},
        slack_floor="warning",
        severity_rank=play["vars"]["severity_rank"],
        digest_hold=hold,
    )
    assert rendered == str(wanted)
    note = _task(play, "Note a record held for the digest")
    assert _when(note) == "digest_hold | bool"
    floor_note = _task(play, "Note a record kept out of Slack by its severity")
    assert "not (digest_hold | bool)" in _when(floor_note)


def test_notify_artifacts_carry_held_and_the_array():
    play = _play(NOTIFY)
    last = play["tasks"][-1]
    assert SET_STATS in last
    data = last[SET_STATS]["data"]
    for key in ("'record_kind'", "'severity'", "'array'", "'held': alert_record.held"):
        assert key in data, key


# ---------------------------------------------------------------- digest.yml
def test_digest_reads_the_platform_since_the_previous_digest_job():
    play = _play(DIGEST)
    assert play["hosts"] == "localhost" and play.get("gather_facts") is False
    path = _task(play, "Note whether the platform token is at hand")
    assert (
        path["ansible.builtin.set_fact"]["digest_from_platform"]
        == "{{ lookup('env', 'AAP_TOKEN') | length > 0 }}"
    )
    previous = _task(play, "Find the previous successful digest job")
    assert _when(previous) == "digest_from_platform | bool"
    url = previous["ansible.builtin.uri"]["url"]
    assert url.startswith(
        "{{ platform_url }}" + JOBS + "{{ 'FlashArray - digest' | urlencode }}"
    )
    for part in ("status=successful", "order_by=-finished", "page_size=1"):
        assert part in url, part
    jobs = _task(play, "Read the notify jobs since then from the controller")
    assert _when(jobs) == "digest_from_platform | bool"
    url = jobs["ansible.builtin.uri"]["url"]
    assert url.startswith(
        "{{ platform_url }}" + JOBS + "{{ 'FlashArray - notify' | urlencode }}"
    )
    # a notify job's artifacts appear when it finishes, so the boundary is a
    # finish time: a job still running at one read is read by the next
    for part in (
        "finished__gt={{ digest_since | urlencode }}",
        "finished__isnull=false",
        "order_by=finished",
        "page_size=200",
    ):
        assert part in url, part
    assert "created__gte" not in url
    for task in (previous, jobs):
        uri = task["ansible.builtin.uri"]
        assert uri["headers"] == {"Authorization": "Bearer {{ platform_token }}"}
        assert uri["validate_certs"] == "{{ platform_verify | bool }}"
        assert task.get("no_log") is True and task.get("failed_when") is False
    # the order: previous digest -> since -> notify jobs -> read_until
    names = [t.get("name") for t in play["tasks"]]
    assert (
        names.index("Find the previous successful digest job")
        < names.index("Settle where this digest starts")
        < names.index("Read the notify jobs since then from the controller")
        < names.index("Note where this read ended")
    )
    # the cursor the next digest starts from: the latest finish read, else
    # where this one started (a failed or empty read moves nothing)
    cursor = _task(play, "Note where this read ended")
    assert _when(cursor) == "digest_from_platform | bool"
    expr = cursor["ansible.builtin.set_fact"]["digest_read_until"]
    since = "2026-01-01T00:05:00.123456Z"
    results = [
        {"id": 8, "finished": "2026-01-01T00:07:00.000001Z"},
        {"id": 9, "finished": "2026-01-01T00:09:30.500000Z"},
        {"id": 10, "finished": None},
    ]
    assert (
        _render(expr, notify_jobs={"json": {"results": results}}, digest_since=since)
        == "2026-01-01T00:09:30.500000Z"
    )
    assert (
        _render(expr, notify_jobs={"json": {"results": []}}, digest_since=since)
        == since
    )
    assert _render(expr, notify_jobs={"status": -1}, digest_since=since) == since
    # held jobs become records shaped like the alert log's: the subject
    # from the job's extra vars, array and severity from its artifacts
    shape = _task(play, "Shape the held notify jobs as records")
    assert _when(shape) == "digest_from_platform | bool"
    assert "selectattr('artifacts.held', 'equalto', true)" in shape["loop"]
    expr = shape["ansible.builtin.set_fact"]["held_records"]
    job = {
        "id": 7,
        "artifacts": {"array": "array-a", "severity": "warning", "held": True},
        "extra_vars": json.dumps(
            {"subject": "array-a alert 42: it's <bad> & worse", "severity": "warning"}
        ),
    }
    assert _literal(expr, held_records=[], item=job) == [
        {
            "array": "array-a",
            "severity": "warning",
            "subject": "array-a alert 42: it's <bad> & worse",
        }
    ]


def test_digest_settles_its_start_from_the_platform_the_log_or_the_window():
    play = _play(DIGEST)
    expr = _task(play, "Settle where this digest starts")["ansible.builtin.set_fact"][
        "digest_since"
    ]
    window = "2026-01-01T00:00:00Z"
    # the previous successful digest job's read_until artifact on the
    # platform; its finish before that artifact existed
    assert (
        _render(
            expr,
            digest_from_platform=True,
            previous_digest={
                "json": {
                    "results": [
                        {
                            "finished": "2026-01-01T00:05:00.123456Z",
                            "artifacts": {
                                "record_kind": "digest",
                                "read_until": "2026-01-01T00:04:58.000001Z",
                            },
                        }
                    ]
                }
            },
            digest_window_start=window,
        )
        == "2026-01-01T00:04:58.000001Z"
    )
    assert (
        _render(
            expr,
            digest_from_platform=True,
            previous_digest={
                "json": {
                    "results": [
                        {
                            "finished": "2026-01-01T00:05:00.123456Z",
                            "artifacts": {"record_kind": "digest"},
                        }
                    ]
                }
            },
            digest_window_start=window,
        )
        == "2026-01-01T00:05:00.123456Z"
    )
    assert (
        _render(
            expr,
            digest_from_platform=True,
            previous_digest={
                "json": {"results": [{"finished": "2026-01-01T00:05:00.123456Z"}]}
            },
            digest_window_start=window,
        )
        == "2026-01-01T00:05:00.123456Z"
    )
    # no previous digest job (or a failed read): the window's start
    assert (
        _render(
            expr,
            digest_from_platform=True,
            previous_digest={"status": -1},
            digest_window_start=window,
        )
        == window
    )
    # the last digest record of the alert log standalone
    records = [
        {"time": "2026-01-01T00:03:00Z", "kind": "digest", "array": "array-a"},
        {"time": "2026-01-01T00:03:00Z", "kind": "digest", "array": "array-b"},
        {
            "time": "2026-01-01T00:04:00Z",
            "kind": "notify",
            "array": "array-a",
            "held": True,
        },
    ]
    assert (
        _render(
            expr,
            digest_from_platform=False,
            log_records=records,
            digest_window_start=window,
        )
        == "2026-01-01T00:03:00Z"
    )
    # no digest record standalone: no bound, every held record is undigested
    assert (
        _render(
            expr, digest_from_platform=False, log_records=[], digest_window_start=window
        )
        == ""
    )
    assert _render(expr, digest_from_platform=False, digest_window_start=window) == ""
    keep = _task(play, "Keep the held records after the previous digest")[
        "ansible.builtin.set_fact"
    ]["held_records"]
    held = [
        {"time": "2025-06-01T00:00:00Z", "kind": "notify", "array": "a", "held": True}
    ]
    assert _literal(keep, log_records=held, digest_since="") == held
    # standalone the marker is taken right before the log is read: it is the
    # next run's cursor, so nothing may be written between the two
    names = [t.get("name") for t in play["tasks"]]
    assert names.index(
        "Note when this digest runs and where the window starts"
    ) + 1 == names.index("Read the alert log")


def test_digest_reads_the_alert_log_standalone_and_keeps_the_held_records_after_the_last_digest():
    play = _play(DIGEST)
    read = _task(play, "Read the alert log")
    assert _when(read) == "not (digest_from_platform | bool) and alert_log is file"
    assert (
        "lookup('file', alert_log)" in read["ansible.builtin.set_fact"]["log_records"]
    )
    keep = _task(play, "Keep the held records after the previous digest")
    assert _when(keep) == "not (digest_from_platform | bool)"
    expr = keep["ansible.builtin.set_fact"]["held_records"]
    records = [
        {
            "time": "2026-01-01T00:01:00Z",
            "kind": "notify",
            "array": "array-a",
            "held": True,
            "subject": "old",
        },
        {
            "time": "2026-01-01T00:03:00Z",
            "kind": "digest",
            "array": "array-a",
            "count": 1,
        },
        {
            "time": "2026-01-01T00:04:00Z",
            "kind": "notify",
            "array": "array-a",
            "held": True,
            "subject": "a1",
        },
        {
            "time": "2026-01-01T00:05:00Z",
            "kind": "notify",
            "array": "array-a",
            "held": False,
            "subject": "posted",
        },
        {
            "time": "2026-01-01T00:06:00Z",
            "kind": "notify",
            "array": "array-b",
            "held": True,
            "subject": "b1",
        },
        {
            "time": "2026-01-01T00:07:00Z",
            "kind": "self_check",
            "array": "array-b",
            "held": True,
        },
        {
            "time": "2026-01-01T00:08:00Z",
            "kind": "notify",
            "array": "array-b",
            "subject": "no held key",
        },
    ]
    kept = _literal(expr, log_records=records, digest_since="2026-01-01T00:03:00Z")
    assert [r["subject"] for r in kept] == ["a1", "b1"]
    assert _literal(expr, digest_since="2026-01-01T00:03:00Z") == []


def test_digest_groups_the_held_records_per_array():
    play = _play(DIGEST)
    arrays = _task(play, "Group the held records by array")["ansible.builtin.set_fact"][
        "digest_arrays"
    ]
    held = [
        {"array": "array-b", "severity": "warning", "subject": "b1"},
        {"array": "array-a", "severity": "critical", "subject": "a1"},
        {"array": "array-a", "severity": "warning", "subject": "a2"},
        {"array": "array-a", "severity": "info", "subject": "a3"},
        {"array": "array-a", "severity": "warning", "subject": "a4"},
    ]
    assert _literal(arrays, held_records=held) == ["array-a", "array-b"]
    group_task = _task(play, "Sum up each array's held records")
    assert group_task["loop"] == "{{ digest_arrays }}"
    group = _literal(group_task["vars"]["group"], held_records=held, item="array-a")
    summed = _literal(
        group_task["ansible.builtin.set_fact"]["digest_groups"],
        digest_groups=[],
        item="array-a",
        group=group,
    )
    assert summed == [
        {
            "array": "array-a",
            "count": 4,
            "critical": 1,
            "warning": 2,
            "info": 1,
            "subjects": ["a1", "a2", "a3", "a4"],
        }
    ]


def test_digest_posts_one_message_per_array_and_records_each():
    play = _play(DIGEST)
    webhook = _task(play, "Read the Slack webhook from the environment")
    assert webhook.get("no_log") is True
    post = _task(play, "Post one digest per array to Slack")
    assert _when(post) == "slack_webhook | length > 0"
    assert post["loop"] == "{{ digest_groups | default([]) }}"
    uri = post["ansible.builtin.uri"]
    assert uri["url"] == "{{ slack_webhook }}" and uri["method"] == "POST"
    assert uri["body"] == {"text": "{{ digest_text | trim }}"}
    assert uri["status_code"] == [200]
    assert post.get("no_log") is True and post.get("failed_when") is False
    text = post["vars"]["digest_text"]
    lines = text.splitlines()
    # the header names the array, the count and the window; then the
    # counts per severity; then the first subjects as bullets, escaped
    assert "{{ item.array }}" in lines[0] and "{{ item.count }}" in lines[0]
    assert "{{ digest_window_minutes }}" in lines[0]
    # an empty start (standalone, no digest record yet) reads as words
    assert "{{ digest_since or 'the alert log began' }}" in lines[0]
    assert (
        lines[1]
        == "critical {{ item.critical }}, warning {{ item.warning }}, info {{ item.info }}"
    )
    assert "{% for subject in item.subjects[:digest_subjects_max] %}" in text
    assert "replace('&', '&amp;') | replace('<', '&lt;') | replace('>', '&gt;')" in text
    assert "\\n" not in text  # the block scalar carries the line breaks
    assert play["vars"]["digest_subjects_max"] == 5
    # the record per array, in the alert log's shape, with the count
    record = _task(play, "Append one digest record per array to the alert log")
    assert record["loop"] == "{{ digest_groups | default([]) }}"
    line = record["ansible.builtin.lineinfile"]["line"]
    for key in (
        "'time'",
        "'kind': 'digest'",
        "'severity'",
        "'array': item.array",
        "'count': item.count",
    ):
        assert key in line, key
    assert record["ansible.builtin.lineinfile"]["path"] == "{{ alert_log }}"
    # the report of a failed post never fails the digest
    report = _task(play, "Report a failed digest post without failing the digest")
    assert "item.status | default(0) != 200" in _when(report)
    # artifacts close the play; nothing here touches an array
    last = play["tasks"][-1]
    assert SET_STATS in last
    assert "'record_kind': 'digest'" in last[SET_STATS]["data"]
    assert "'arrays':" in last[SET_STATS]["data"]
    # the next platform digest starts from read_until; standalone it is the
    # start this one used
    assert (
        "'read_until': digest_read_until | default(digest_since)"
        in last[SET_STATS]["data"]
    )
    assert "api_token" not in DIGEST.read_text(encoding="utf-8")


# where a module argument carries the value of the token or the webhook
# (reading it from the environment, or templating it in); a length test
# carries nothing and may sit on a logged task
SECRET_VALUES = (
    "lookup('env', 'AAP_TOKEN') }}",
    "lookup('env', 'SLACK_WEBHOOK_URL') }}",
    "{{ platform_token }}",
    "{{ slack_webhook }}",
)


@pytest.mark.parametrize("path", [NOTIFY, DIGEST], ids=lambda p: p.name)
def test_secrets_never_reach_the_output(path):
    """Every task whose arguments carry the value of the platform token or
    the webhook is no_log, and each playbook has such tasks to check."""
    found = 0
    for task in _tasks(_play(path)):
        # JSON keeps the quotes as written; YAML would double them
        args = json.dumps({k: task[k] for k in task if k in MODULES})
        if any(value in args for value in SECRET_VALUES):
            found += 1
            assert task.get("no_log") is True, task.get("name")
    assert found >= 3, path.name  # the reads and the requests that use them


# ---------------------------------------------------------------- the wiring
def test_alerts_rulebook_ticks_the_digest_standalone_only():
    doc = _load(RULEBOOKS / "alerts.yml")
    assert [rs["name"] for rs in doc] == [
        "FlashArray alerts",
        "FlashArray notification digest",
    ]
    digest = doc[1]
    assert digest["hosts"] == "all"
    assert digest["sources"] == [
        {"ansible.eda.tick": {"delay": "{{ digest_interval_s }}"}}
    ]
    (rule,) = digest["rules"]
    assert rule["name"] == "Post the digest of the held notifications"
    assert rule["condition"] == "event.i is defined"
    assert rule["action"] == {
        "run_playbook": {
            "name": "playbooks/digest.yml",
            "extra_vars": {
                "alert_log": "{{ alert_log }}",
                "digest_window_minutes": "{{ digest_window_minutes }}",
            },
        }
    }
    # the platform copy keeps the alerts ruleset only; the schedule digests there
    platform = _load(RULEBOOKS / "platform" / "alerts.yml")
    assert [rs["name"] for rs in platform] == ["FlashArray alerts"]
    spec = importlib.util.spec_from_file_location("platform_rulebooks", GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.PLAYBOOK_TEMPLATES["playbooks/digest.yml"] == "FlashArray - digest"
    assert generator.tick_only(digest)


def test_seed_templates_schedules_and_credentials_for_the_digest():
    play = _play(SEED)
    templates = {t["name"]: t for t in play["vars"]["job_templates"]}
    digest = templates["FlashArray - digest"]
    assert digest["playbook"] == "playbooks/digest.yml"
    assert (ROOT / digest["playbook"]).exists()
    # the digest reaches no array: the platform token and the webhook only
    assert digest["credentials"].startswith(
        "{{ [platform_token_credential_name] + ([slack_credential_name] if slack_seeded"
    )
    assert "fleet_credential_name" not in digest["credentials"]
    # the notify template counts with the platform token beside the fleet
    # credential and the webhook
    notify = templates["FlashArray - notify"]
    assert notify["credentials"].startswith(
        "{{ [fleet_credential_name, platform_token_credential_name] + ([slack_credential_name] if slack_seeded"
    )
    # the schedule, every quarter hour from the example vars
    schedules = {s["name"]: s for s in play["vars"]["schedules"]}
    schedule = schedules["FlashArray - digest (every quarter hour)"]
    assert schedule["template"] == "FlashArray - digest"
    assert schedule["enabled"] == "{{ aap_digest_schedule.enabled }}"
    assert "extra_data" not in schedule
    context = dict(_load(EXAMPLE))
    context.update(play["vars"])
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    rrule = schedule["rrule"]
    for _ in range(3):
        if "{{" not in rrule:
            break
        rrule = env.from_string(rrule).render(**context)
    assert rrule == "DTSTART;TZID=UTC:20260101T000000 RRULE:FREQ=MINUTELY;INTERVAL=15"
    assert _load(EXAMPLE)["aap_digest_schedule"] == {
        "enabled": True,
        "every_minutes": 15,
    }
    # the platform token credential is the one credential, seeded once
    credential = next(
        t
        for t in play["tasks"]
        if t.get("name", "").startswith(
            "Platform token credential for the definitions check"
        )
    )
    assert credential.get("no_log") is True
    assert (
        credential["ansible.controller.credential"]["name"]
        == "{{ platform_token_credential_name }}"
    )
