# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The run-twice contract: launching a template a second time with the same
extra vars leaves the array as it is, reports changed false where nothing
changed, and says so. These checks parse the playbooks and pin the guards
that make a second run harmless; docs/VERIFICATION.md records which second
runs happened on the arrays."""

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Ansible's !unsafe tag."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _plays(name):
    return yaml.load((ROOT / "playbooks" / name).read_text(), Loader=_Loader)


def _tasks(name, play_index=0):
    play = _plays(name)[play_index]
    flat = []
    for task in play.get("tasks", []):
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(task.get(key, []))
    return flat


def _task(name, startswith, play_index=0):
    tasks = _tasks(name, play_index)
    exact = [t for t in tasks if t.get("name") == startswith]
    hits = exact or [t for t in tasks if t.get("name", "").startswith(startswith)]
    assert len(hits) == 1, f"{name}: {len(hits)} tasks start with {startswith!r}"
    return hits[0]


def _when(task):
    when = task.get("when")
    if when is None:
        return ""
    return " and ".join(when) if isinstance(when, list) else str(when)


def _text(name):
    return (ROOT / "playbooks" / name).read_text()


def _set_fact(task, key):
    return task["ansible.builtin.set_fact"][key]


def test_expand_volume_treats_an_equal_size_as_already_done():
    refuse = _task("expand_volume.yml", "Refuse a volume that is missing or destroyed")
    that = refuse["ansible.builtin.assert"]["that"]
    assert any(">= before.provisioned" in c for c in that)
    assert not any("int > before.provisioned" in c for c in that)
    assert "already_sized" in _when(_task("expand_volume.yml", "Grow the volume"))
    text = _text("expand_volume.yml")
    assert (
        text.count("'already_at_size': already_sized | bool") == 2
    )  # record and artifacts


def test_replicate_now_reads_before_it_snapshots_and_skips_inside_the_cooldown():
    tasks = [t["name"] for t in _tasks("replicate_now.yml")]
    read = next(
        n for n in tasks if n.startswith("Read the group's newest eda snapshot")
    )
    snap = next(n for n in tasks if n.startswith("Snapshot the group"))
    assert tasks.index(read) < tasks.index(snap)
    assert "snapshot_wanted" in _when(_task("replicate_now.yml", "Snapshot the group"))
    pin = _task("replicate_now.yml", "Pin the snapshot suffix")
    assert "snapshot_suffix | default(" in _set_fact(
        pin, "snap_suffix"
    )  # a caller may pin
    assert "replicate_now_cooldown_s | default(300)" in _set_fact(
        pin, "snapshot_wanted"
    )
    assert "recent.pinned_exists" in _set_fact(pin, "snapshot_wanted")
    text = _text("replicate_now.yml")
    assert "'skipped': not (snapshot_wanted | bool)" in text
    assert "snap.changed | default(false)" in text  # the skipped task has no changed
    assert (
        '".eda-protect-" not in s["name"]' in text
    )  # protect-now snapshots do not count
    assert (
        "split('.', 1) | last" in text
    )  # no regex backslash inside block-scalar Jinja


def test_protect_now_skips_the_repeat_and_the_ratchet_inside_the_cooldown():
    snap = _task("protect_now.yml", "Snapshot every local protection group now")
    assert "protect_wanted" in _when(snap)
    lengthen = _task("protect_now.yml", "Lengthen the enabled eradication delay")
    assert "protect_wanted" in _when(lengthen)  # the delay is not ratcheted twice
    decide = _task(
        "protect_now.yml", "Decide whether this is a repeat inside the cooldown"
    )
    assert "protect_now_cooldown_s | default(600)" in _set_fact(
        decide, "protect_wanted"
    )
    text = _text("protect_now.yml")
    assert "newest_protect" in text and "eda-protect-*" in text
    # the cooldown reads local groups only: '<array>:<group>' was replicated in, 'pod::group' is local
    assert 're.match(r"^[^:]+:(?!:)", x["name"])' in text
    # a loop skipped by its when still registers one skipped result per item
    kept = _task("protect_now.yml", "Keep the snapshot results that ran")
    assert "if protect_wanted | bool else []" in _set_fact(kept, "snapped")
    assert text.count("snapped |") == 5
    assert "'snapshot_suffix': snap_suffix if protect_wanted | bool else" in text
    assert (
        "split('.', 1) | last" in text
    )  # no regex backslash inside block-scalar Jinja
    assert "'skipped': not (protect_wanted | bool)" in text


def test_orchestrator_post_looks_for_an_open_execution_before_posting():
    tasks = [t["name"] for t in _tasks("orchestrator_post.yml")]
    look = "Look for an execution already handling this event"
    keep = "Keep the execution found, if any"
    assert tasks.index(look) < tasks.index(keep) < tasks.index("Post the event")
    lookup = _task("orchestrator_post.yml", look)
    assert lookup["failed_when"] is False and lookup["no_log"] is True
    assert "orchestrator_dedupe" in _when(lookup)
    script = lookup["ansible.builtin.command"]["stdin"]
    for needle in (
        '("paused", "running")',
        "next_cursor",
        'ev.get("event_type") != want_type',
        'ev.get("array") != want_array',
        "scope.items()",
    ):
        assert needle in script, needle
    env = lookup["environment"]
    assert "'pgroup', 'volume', 'pod', 'component'" in env["SCOPE"]
    assert env["ORCH_TOKEN"] == "{{ ao_token }}"
    post = _task("orchestrator_post.yml", "Post the event")
    assert "pending_execution | length == 0" in _when(post)
    text = _text("orchestrator_post.yml")
    assert "'pending' if pending_execution | length > 0" in text
    assert "stage == 'pending'" in text  # severity info, status none
    # the local fallback does not raise a request for an event already handled
    fallback = _plays("orchestrator_post.yml")[1]
    assert any("!= 'pending'" in str(w) for w in fallback["when"])


def test_switchover_marks_only_a_source_still_promoted_and_waits_for_a_settled_pod():
    mark = _task("dr_switchover.yml", "Mark the source pod as planned work")
    assert "source_before.promotion_status != 'demoted'" in _when(mark)
    stop = _task(
        "dr_switchover.yml", "Stop when the planned-work tag on the source pod"
    )
    assert "source_before.promotion_status != 'demoted'" in _when(stop)
    read = _task("dr_switchover.yml", "Read the source pod and its replica links")
    assert read["retries"] >= 12 and "requested_promotion_state" in read["until"]
    assert read["ignore_errors"] is True  # the assert after it says why
    parse = _set_fact(_task("dr_switchover.yml", "Parse"), "source_before")
    assert (
        "last | default('no error text')" in parse
    )  # an empty stderr is not a second error


@pytest.mark.parametrize("name", ["dr_demote.yml", "dr_promote.yml"])
def test_demote_and_promote_wait_for_a_settled_pod_before_judging(name):
    read = _task(name, "Read the pod and its replica links")
    assert read["retries"] >= 12 and "requested_promotion_state" in read["until"]
    assert read["ignore_errors"] is True
    parse = _set_fact(_task(name, "Parse"), "before")
    assert "pod read failed" in parse and "last | default('no error text')" in parse


def test_capacity_request_is_keyed_and_reuses_an_open_request():
    pin = _task("capacity_request.yml", "Pin the request id and time")
    assert "request_key" in _set_fact(pin, "request_id")
    tasks = [t["name"] for t in _tasks("capacity_request.yml")]
    look = next(n for n in tasks if n.startswith("Look for a request already awaiting"))
    assert tasks.index(look) < tasks.index("Write the request file")
    assert "request_open" in _when(
        _task("capacity_request.yml", "Write the request file")
    )
    listing = _task("capacity_request.yml", "List what is pending eradication")
    assert "request_open" in _when(listing)
    text = _text("capacity_request.yml")
    assert "'already_raised' if open_request.status == 'awaiting_approval'" in text
    assert "'already_' ~ open_request.status" in text
    assert (
        'found = load(os.path.join(d, keyed + ".json"))' in text
    )  # a done request stays done
    assert "'already_raised': request_open | bool" in text  # artifacts
    # the launchers hand over a key so a retried launch computes the same id
    definition = (ROOT / "orchestrator" / "capacity_reclaim_approval.json").read_text()
    assert '"request_key": "${trigger.event.observed_at}"' in definition
    rulebook = (ROOT / "extensions" / "eda" / "rulebooks" / "capacity.yml").read_text()
    assert "request_key: \"{{ event.meta.received_at | default('') }}\"" in rulebook


def test_capacity_request_id_is_stable_for_one_key():
    jinja2 = pytest.importorskip("jinja2")
    pin = _task("capacity_request.yml", "Pin the request id and time")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["regex_replace"] = lambda value, pattern, repl="": re.sub(
        pattern, repl, str(value)
    )
    render = env.from_string(_set_fact(pin, "request_id")).render
    ctx = {
        "at_risk_array": "array-a",
        "request_key": "2026-11-12T20:00:00Z",
        "now": None,
    }
    assert render(**ctx) == render(**ctx) == "array-a-20261112T200000Z"
    assert re.match(
        r"^[A-Za-z0-9][A-Za-z0-9._-]*$", render(**ctx)
    )  # what the reclaim accepts


def test_capacity_reclaim_accepts_a_request_already_done_with_the_same_action():
    guard = _task(
        "capacity_reclaim.yml", "Refuse anything that is not awaiting approval"
    )
    conditions = " ".join(guard["ansible.builtin.assert"]["that"])
    assert (
        "request.status == 'done' and request.action | default('') == reclaim_action"
        in conditions
    )
    assert "already_done" in _when(
        _task("capacity_reclaim.yml", "Eradicate the listed candidates")
    )
    assert "already_done" in _when(
        _task("capacity_reclaim.yml", "Mark the request done")
    )
    parse = _set_fact(
        _task("capacity_reclaim.yml", "Parse the result"), "reclaim_result"
    )
    assert "request.result" in parse  # the first run's result is republished
    text = _text("capacity_reclaim.yml")
    assert text.count("'already_done': already_done | bool") == 2
    assert text.count("'nothing_pending'") == 2


def test_alertmanager_silence_reuses_an_active_silence_and_expires_only_an_active_one():
    tasks = [t["name"] for t in _tasks("alertmanager_silence.yml")]
    look = next(n for n in tasks if n.startswith("Look for an active silence of ours"))
    assert tasks.index(look) < tasks.index("Create the silence")
    create_when = _when(_task("alertmanager_silence.yml", "Create the silence"))
    assert "existing_silence | length == 0" in create_when
    pick = _set_fact(
        _task("alertmanager_silence.yml", "Pick the silence to reuse"),
        "existing_silence",
    )
    for needle in (
        "'active'",
        "silence_created_by",
        "'alertname'",
        "'array_name'",
        "left >= 300",
        "well_formed",
    ):
        assert needle in pick  # ours, with time left, parsed only when well formed
    assert (
        "\\" not in pick
    )  # Ansible doubles backslashes inside block-scalar Jinja literals
    read = next(n for n in tasks if n.startswith("Read the silence before expiring it"))
    assert tasks.index(read) < tasks.index("Expire the silence")
    expire_when = _when(_task("alertmanager_silence.yml", "Expire the silence"))
    assert "silence_read.json.status.state" in expire_when and "!= 404" in expire_when
    text = _text("alertmanager_silence.yml")
    assert text.count("'reused': silence_outcome.reused | bool") == 2
    assert text.count("'already_expired': silence_outcome.already_expired | bool") == 2


def test_dr_drill_copies_are_keyed_to_the_launch_and_a_drill_in_flight_is_left_alone():
    key = _set_fact(
        _task("dr_drill.yml", "Pin the drill key of this launch"), "dr_drill_key"
    )
    assert "tower_job_id" in key and "awx_job_id" in key
    one = yaml.load(
        (ROOT / "playbooks" / "dr_drill_one.yml").read_text(), Loader=_Loader
    )
    by_name = {t["name"]: t for t in one if "name" in t}
    start = by_name["Start the drill with a clean slate"]
    assert "dr_drill_key" in _set_fact(start, "drill_key")
    assert _set_fact(start, "drill_skipped") is False
    base = _set_fact(by_name["Keep the base within the name limit"], "drill_base")
    assert (
        "drill_full | length <= 40" in base and "hash('md5'))[:6]" in base
    )  # fixed width
    assert (
        _set_fact(by_name["Pin the copy name"], "drill_copy")
        == "{{ drill_base }}-{{ drill_key }}"
    )
    block = next(t for t in one if "block" in t)
    names = {t["name"]: t for t in block["block"]}
    assert "Leave a drill already in flight alone" in names
    for guarded in (
        "Copy the member volume from the replicated snapshot",
        "Verify the copy exists and matches the snapshot size",
        "Fail the drill when the copy does not match the snapshot",
    ):
        assert "not drill_skipped" in _when(names[guarded]), guarded
    stale = names["Destroy and eradicate a stale copy an earlier launch left"]
    assert stale["loop"] == "{{ drill_snap.stale | default([]) }}"
    assert "not drill_skipped" in _when(stale)
    always = {t["name"]: t for t in block["always"]}
    assert "not drill_skipped" in _when(
        always["Destroy and eradicate the copy"]
    )  # never another launch's copy
    text = (ROOT / "playbooks" / "dr_drill_one.yml").read_text()
    assert "in_progress" in text and "'skipped': drill_skipped | bool" in text
    assert 'v["name"][len(base) + 1:].isdigit()' in text  # exact match: <base>-<digits>
    assert "STALE_S" in text and "dr_drill_stale_s | default(1800)" in text
    assert (
        "if drill_skipped" in always["Show the drill"]["ansible.builtin.debug"]["msg"]
    )  # no snapshot fields then
    keep = _set_fact(always["Keep this drill for the run's artifacts"], "drills_done")
    assert "'skipped' if drill_skipped" in keep
    assert "'skipped_count'" in _text("dr_drill.yml")


def test_alert_runbook_records_carry_a_ticket_key():
    build = _set_fact(_task("alert_runbook.yml", "Build the record"), "alert_record")
    assert (
        build["ticket_key"]
        == "{{ target_array }}:{{ alert_id | default('') }}:{{ transition | default('opened') }}"
    )
