# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the purefa_events source plugin that need no array: the helpers,
the alert transition diff, the audit cursor and its pagination, the capped
volume inclusion, the protection group transfer ordering and the SDK client
construction, all driven by a fake SDK client."""

import asyncio
import contextlib
import json
import logging
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import yaml

NOW_MS = int(time.time() * 1000)
HOUR_MS = 3_600_000


class FakeResponse:
    """What py-pure-client returns: .items of model objects with to_dict()."""

    def __init__(self, items, total=None):
        self.status_code = 200
        self.items = [SimpleNamespace(to_dict=lambda d=d: d, **d) for d in items]
        if total is not None:
            self.total_item_count = total


class FakeError:
    """An ErrorResponse: no .items, a list of errors with a message."""

    def __init__(self, message):
        self.status_code = 400
        self.errors = [SimpleNamespace(message=message)]


class FakeClient:
    def __init__(
        self,
        alerts=None,
        audits=None,
        pgroups=None,
        transfers=None,
        volumes_perf=None,
        tags=None,
    ):
        self.alerts = alerts or []
        self.audits = audits or []
        self.pgroups = pgroups or []
        self.transfers = transfers or []  # in the array's default (name) order
        self.volumes_perf = volumes_perf or []
        self.tags = tags or []  # (volume, key, value)
        self.calls = []

    # --- alerts / audits -------------------------------------------------
    def get_alerts(self, **kw):
        self.calls.append(("get_alerts", kw))
        if "names" in kw:
            return FakeResponse([a for a in self.alerts if a["name"] in kw["names"]])
        return FakeResponse([a for a in self.alerts if a["state"] != "closed"])

    def get_audits(self, **kw):
        self.calls.append(("get_audits", kw))
        rows = sorted(
            self.audits, key=lambda r: (r["time"], int(r["name"])), reverse=True
        )
        off = kw.get("offset", 0)
        return FakeResponse(rows[off : off + kw.get("limit", 200)])

    def get_maintenance_windows(self):
        return FakeResponse([])

    # --- replication -----------------------------------------------------
    def get_array_connections_path(self, **kw):
        return FakeResponse([])

    def get_array_connections(self, **kw):
        return FakeResponse([])

    def get_pod_replica_links(self, **kw):
        return FakeResponse([])

    def get_pods(self, **kw):
        return FakeResponse([])

    def get_protection_groups(self, **kw):
        return FakeResponse(self.pgroups)

    def get_protection_groups_targets(self, **kw):
        return FakeResponse(
            [
                {"group": {"name": p["name"]}, "member": {"name": "array-b"}}
                for p in self.pgroups
            ]
        )

    def get_protection_group_snapshots_transfer(self, **kw):
        self.calls.append(("get_protection_group_snapshots_transfer", kw))
        rows = list(self.transfers)
        if kw.get("destroyed") is False:
            rows = [r for r in rows if not r.get("destroyed")]
        if kw.get("sort") == ["completed-"]:
            # Purity puts a null `completed` first under a descending sort
            rows.sort(
                key=lambda r: (
                    r.get("completed") is not None,
                    -(r.get("completed") or 0),
                )
            )
        return FakeResponse(rows[: kw.get("limit", len(rows))])

    # --- telemetry -------------------------------------------------------
    def get_arrays_space(self, **kw):
        self.calls.append(("get_arrays_space", kw))
        return FakeResponse(
            [
                {
                    "time": NOW_MS,
                    "capacity": 1000,
                    "space": {
                        "total_physical": 500,
                        "data_reduction": 3.0,
                        "snapshots": 50,
                    },
                }
            ]
        )

    def get_volumes_space(self, **kw):
        self.calls.append(("get_volumes_space", kw))
        return FakeResponse([])

    def get_volume_snapshots(self, **kw):
        return FakeResponse([], total=0)

    def get_arrays_performance(self, **kw):
        return FakeResponse(
            [
                {
                    "time": NOW_MS,
                    "usec_per_read_op": 300,
                    "usec_per_write_op": 400,
                    "reads_per_sec": 1000,
                    "writes_per_sec": 1000,
                    "read_bytes_per_sec": 1,
                    "write_bytes_per_sec": 1,
                    "queue_depth": 1,
                }
            ]
        )

    def get_volumes_performance(self, **kw):
        self.calls.append(("get_volumes_performance", kw))
        rows = list(self.volumes_perf)
        if "names" in kw:
            known = {r["name"] for r in rows}
            if any(n not in known for n in kw["names"]):
                return FakeError("Volume does not exist.")
            return FakeResponse([r for r in rows if r["name"] in kw["names"]])
        key = kw["sort"][0].rstrip("-")
        rows.sort(key=lambda r: r.get(key) or 0, reverse=True)
        return FakeResponse(rows[: kw.get("limit", len(rows))])

    def get_volumes(self, **kw):
        self.calls.append(("get_volumes", kw))
        return FakeResponse(
            [
                {"name": n, "qos": {"iops_limit": 20000, "bandwidth_limit": None}}
                for n in kw.get("names", [])
            ]
        )

    def get_volumes_tags(self, **kw):
        self.calls.append(("get_volumes_tags", kw))
        return FakeResponse(
            [
                {"key": k, "value": v, "namespace": "eda", "resource": {"name": n}}
                for n, k, v in self.tags
            ]
        )


class FakeArray:
    def __init__(self, client, name="array-a"):
        self.name = name
        self.client = client


def alert(name, state="open", updated=1, **extra):
    return {
        "name": name,
        "state": state,
        "updated": updated,
        "code": 42,
        "severity": "warning",
        **extra,
    }


# --- helpers ---------------------------------------------------------------
def test_csv_sorts_and_joins(events):
    assert events._csv(["b", "a"]) == "a,b"
    assert events._csv(None) == ""


def test_ratio_and_avg_tolerate_missing_values(events):
    assert events._ratio(300, 100) == 3.0
    assert events._ratio(None, 100) is None
    assert events._ratio(300, 0) is None
    assert events._avg([1, None, 2]) == 1.5
    assert events._avg([]) is None


def test_as_list_and_truthy(events):
    assert events._as_list("a, b") == ["a", "b"]
    assert events._as_list(["x"]) == ["x"]
    assert events._as_list(None) == []
    assert events._truthy("Yes") and events._truthy(1) and not events._truthy("off")
    assert events._truthy(None, True) is True


def test_poll_error_shape(events):
    e = events._poll_error("audits", "array-a", RuntimeError("boom"))
    assert e == {
        "purefa_poll_error": {
            "signal": "audits",
            "array": "array-a",
            "error": "boom",
            "consecutive": 1,
            "escalate": False,
        }
    }


def test_poll_streak_counts_consecutive_failures_per_array_and_signal(events):
    state = {}
    assert events._poll_streak(state, "alerts", "a", True) == 1
    assert events._poll_streak(state, "alerts", "a", True) == 2
    assert events._poll_streak(state, "alerts", "b", True) == 1
    assert events._poll_streak(state, "health", "a", True) == 1
    # a success ends the streak; the next failure starts a new one
    assert events._poll_streak(state, "alerts", "a", False) == 0
    assert events._poll_streak(state, "alerts", "a", True) == 1
    e = events._poll_error("alerts", "a", RuntimeError("x"), 10, True)
    assert e["purefa_poll_error"]["consecutive"] == 10
    assert e["purefa_poll_error"]["escalate"] is True


def test_documentation_block_covers_every_argument(events):
    """DOCUMENTATION parses as YAML and names every argument the code reads
    (the two threshold aliases are read as a fallback only)."""
    doc = yaml.safe_load(events.DOCUMENTATION)
    assert doc["name"] == "purefa_events"
    options = set(doc["options"])
    source = pathlib.Path(events.__file__).read_text()
    read = set(re.findall(r'args\.get\(\s*"([a-z_]+)"', source))
    read -= {"drr_drop_pct", "snapshot_growth_pct"}
    assert read <= options, f"undocumented arguments: {sorted(read - options)}"
    assert doc["options"]["request_timeout"]["default"] == 30
    assert doc["options"]["growth_volumes"]["default"] == 500
    assert yaml.safe_load(events.EXAMPLES)[0]["sources"][0][
        "sdodsley.flasharray_eda.purefa_events"
    ]


# --- SDK client construction -----------------------------------------------
def test_client_is_built_with_timeouts_and_user_agent(events, monkeypatch):
    monkeypatch.setenv("FA_URL_T", "https://192.0.2.1/")
    monkeypatch.setenv("FA_TOKEN_T", "secret")
    events.flasharray.Client.reset_mock()
    arr = events.Array(
        {"name": "a", "fa_url_env": "FA_URL_T", "api_token_env": "FA_TOKEN_T"},
        False,
        request_timeout=12,
    )
    assert arr.host == "192.0.2.1"
    arr.client
    kw = events.flasharray.Client.call_args.kwargs
    assert kw["timeout"] == (events.CONNECT_TIMEOUT, 12.0)
    assert kw["user_agent"] == events.USER_AGENT
    assert kw["api_token"] == "secret" and kw["verify_ssl"] is False
    # the default
    arr = events.Array({"name": "b", "fa_url": "192.0.2.2", "api_token": "t"}, True)
    arr.client
    assert events.flasharray.Client.call_args.kwargs["timeout"] == (10, 30.0)


def test_array_requires_url_and_token(events):
    with pytest.raises(ValueError):
        events.Array({"name": "a", "api_token": "t"}, False)
    with pytest.raises(ValueError):
        events.Array({"name": "a", "fa_url": "x"}, False)


# --- the arrays argument as a string ---------------------------------------
class _NoQueue:
    async def put(self, event):
        raise AssertionError("nothing should be queued")


def test_arrays_argument_accepts_json_and_yaml_strings(events):
    spec = [{"name": "a", "fa_url": "192.0.2.1", "api_token": "t"}]
    assert events._decode_arrays(json.dumps(spec), "purefa_events") == spec
    text = "- name: a\n  fa_url: 192.0.2.1\n  api_token: t\n"
    assert events._decode_arrays(text, "purefa_events") == spec
    assert events._decode_arrays(spec, "purefa_events") is spec
    assert events._decode_arrays("  ", "purefa_events") is None
    assert events._decode_arrays(None, "purefa_events") is None


def test_main_decodes_the_string_before_validating_and_never_logs_the_token(
    events, caplog
):
    token = "s3cret-token-value"
    caplog.set_level(logging.DEBUG)
    # the entries are validated as if they had arrived as a list
    arrays = json.dumps([{"name": "a", "fa_url": "192.0.2.1"}])
    with pytest.raises(ValueError, match="api_token or api_token_env"):
        asyncio.run(events.main(_NoQueue(), {"arrays": arrays}))
    # a complete entry passes that check; the signal check then fails, which
    # shows the string was decoded, and neither the error nor a log record
    # carries the token
    arrays = json.dumps([{"name": "a", "fa_url": "192.0.2.1", "api_token": token}])
    with pytest.raises(ValueError, match="unknown signals") as exc:
        asyncio.run(events.main(_NoQueue(), {"arrays": arrays, "signals": "bogus"}))
    assert token not in str(exc.value) and token not in caplog.text


@pytest.mark.parametrize(
    "text",
    ['[{"name": "a", "api_token": "s3cret"', "just words s3cret", "42"],
)
def test_main_rejects_a_malformed_arrays_string_without_echoing_it(
    events, caplog, text
):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError, match="JSON") as exc:
        asyncio.run(events.main(_NoQueue(), {"arrays": text}))
    assert "arrays" in str(exc.value)
    assert "s3cret" not in str(exc.value) and "s3cret" not in caplog.text


# --- alert diff ------------------------------------------------------------
def test_alerts_first_poll_emits_initial_then_transitions(events):
    client = FakeClient(alerts=[alert("1"), alert("2")])
    arr = FakeArray(client)
    state = {}
    first = events.poll_alerts(arr, state, {})
    assert sorted(e["purefa_alert"]["transition"] for e in first) == [
        "initial",
        "initial",
    ]

    # nothing changed: no events
    assert events.poll_alerts(arr, state, {}) == []

    # alert 2 moves to closing, alert 3 opens, alert 1 closes
    client.alerts = [
        alert("2", state="closing", updated=2),
        alert("3"),
        alert("1", state="closed", updated=3),
    ]
    third = events.poll_alerts(arr, state, {})
    by_id = {
        e["purefa_alert"]["alert_id"]: e["purefa_alert"]["transition"] for e in third
    }
    assert by_id == {"2": "closing", "3": "opened", "1": "closed"}
    assert all(e["purefa_alert"]["array"] == "array-a" for e in third)
    assert all("maintenance_active" in e["purefa_alert"] for e in third)


def test_alerts_initial_can_be_suppressed(events):
    arr = FakeArray(FakeClient(alerts=[alert("1")]))
    assert events.poll_alerts(arr, {}, {"include_initial_alerts": False}) == []


# --- audit cursor ----------------------------------------------------------
def audit(name, time, command="purevol", user="eda-human"):
    return {
        "name": name,
        "time": time,
        "user": user,
        "command": command,
        "subcommand": "destroy",
        "arguments": "x",
    }


def test_audits_start_at_newest_and_emit_only_new_rows(events):
    client = FakeClient(audits=[audit("1", 100), audit("2", 200)])
    arr = FakeArray(client)
    state = {}
    # first poll with no lookback: the newest record becomes the cursor, nothing emitted
    assert events.poll_audits(arr, state, {}) == []
    client.audits.append(audit("3", 300))
    client.audits.append(audit("4", 300))  # same timestamp as another new row
    out = events.poll_audits(arr, state, {"automation_users": ["svc-eda"]})
    assert [e["purefa_audit"]["audit_id"] for e in out] == ["3", "4"]
    assert out[0]["purefa_audit"]["by_automation"] is False
    # a repeat poll emits nothing, including the rows sharing the cursor timestamp
    assert events.poll_audits(arr, state, {}) == []
    client.audits.append(audit("5", 300, user="svc-eda"))
    again = events.poll_audits(arr, state, {"automation_users": ["svc-eda"]})
    assert [e["purefa_audit"]["audit_id"] for e in again] == ["5"]
    assert again[0]["purefa_audit"]["by_automation"] is True


def test_automation_users_accepts_a_comma_separated_string(events):
    client = FakeClient(audits=[audit("1", 1)])
    arr = FakeArray(client)
    state = {}
    events.poll_audits(arr, state, {})
    client.audits.append(audit("2", 2, user="svc-eda"))
    out = events.poll_audits(arr, state, {"automation_users": "ops, svc-eda"})
    assert out[0]["purefa_audit"]["by_automation"] is True


def test_audits_lookback_replays_history(events):
    client = FakeClient(audits=[audit("1", 1_000), audit("2", 61_000)])
    arr = FakeArray(client)
    out = events.poll_audits(arr, {}, {"audit_lookback": 120})
    assert [e["purefa_audit"]["audit_id"] for e in out] == ["1", "2"]


def test_system_user_is_marked(events):
    client = FakeClient(audits=[audit("1", 1)])
    arr = FakeArray(client)
    events.poll_audits(arr, state := {}, {})
    client.audits.append(audit("2", 2, user="purity"))
    out = events.poll_audits(arr, state, {})
    assert out[0]["purefa_audit"]["system_user"] is True


def test_audits_page_past_audit_batch(events):
    """More new records than audit_batch: every one is emitted, oldest first,
    across several offset pages, with no overflow report."""
    client = FakeClient(audits=[audit("1", 100)])
    arr = FakeArray(client)
    state = {}
    events.poll_audits(arr, state, {"audit_batch": 3})
    for i in range(2, 7):
        client.audits.append(audit(str(i), 100 * i))
    client.calls.clear()
    out = events.poll_audits(arr, state, {"audit_batch": 3})
    assert [e["purefa_audit"]["audit_id"] for e in out] == ["2", "3", "4", "5", "6"]
    assert all("purefa_audit" in e for e in out)
    # the second page ends on the row at the cursor time (always re-read for
    # ties), so one more, empty page settles it
    offsets = [kw["offset"] for name, kw in client.calls if name == "get_audits"]
    assert offsets == [0, 3, 6]
    assert events.poll_audits(arr, state, {"audit_batch": 3}) == []


def test_audits_overflow_is_reported_not_silent(events):
    client = FakeClient(audits=[audit("1", 100)])
    arr = FakeArray(client)
    state = {}
    events.poll_audits(arr, state, {"audit_batch": 2})
    for i in range(2, 27):  # 25 new rows, 2 per page, 10 pages: 20 read, 5 skipped
        client.audits.append(audit(str(i), 100 * i))
    out = events.poll_audits(arr, state, {"audit_batch": 2})
    audits = [e["purefa_audit"]["audit_id"] for e in out if "purefa_audit" in e]
    assert audits == [str(i) for i in range(7, 27)]
    errors = [e["purefa_poll_error"] for e in out if "purefa_poll_error" in e]
    assert len(errors) == 1
    assert errors[0]["signal"] == "audits" and errors[0]["array"] == "array-a"
    assert "audit_batch overflow" in errors[0]["error"]
    # the cursor still moved to the newest record
    assert events.poll_audits(arr, state, {"audit_batch": 2}) == []


# --- capped volumes are always reported --------------------------------------
def vol_perf(name, latency, iops):
    return {
        "name": name,
        "usec_per_read_op": latency,
        "usec_per_write_op": latency,
        "reads_per_sec": iops,
        "writes_per_sec": 0,
    }


def test_capped_volume_outside_top_n_is_still_reported(events):
    client = FakeClient(
        volumes_perf=[
            vol_perf("busy-1", 5000, 500),
            vol_perf("busy-2", 4000, 400),
            vol_perf("busy-3", 3000, 300),
            vol_perf("capped", 100, 0),  # idle: below min_iops, outside the top 2
        ],
        tags=[
            ("capped", "qos_cap", "2026-11-05T10:00:00Z"),
            ("capped", "qos_prev_iops", ""),
        ],
    )
    arr = FakeArray(client)
    state = {}
    args = {"top_volumes": 2, "min_iops": 1, "baseline": False}
    out = events._performance_events(arr, state, args)
    vols = {
        e["purefa_volume_performance"]["volume"]: e["purefa_volume_performance"]
        for e in out
        if "purefa_volume_performance" in e
    }
    assert set(vols) == {"busy-1", "busy-2", "capped"}
    assert vols["capped"]["eda_capped"] is True
    assert vols["capped"]["eda_cap_since"] == "2026-11-05T10:00:00Z"
    assert vols["capped"]["normal_polls"] == 1
    assert vols["busy-1"]["eda_capped"] is False
    assert vols["busy-1"]["high_latency_polls"] == 1
    # the tag fetch is array-wide and the capped volume alone is fetched by name
    tag_calls = [kw for n, kw in client.calls if n == "get_volumes_tags"]
    assert tag_calls == [{"namespaces": ["eda"]}]
    by_name = [
        kw["names"]
        for n, kw in client.calls
        if n == "get_volumes_performance" and "names" in kw
    ]
    assert by_name == [["capped"]]
    # the counter keeps accumulating poll after poll
    out = events._performance_events(arr, state, args)
    capped = next(
        e["purefa_volume_performance"]
        for e in out
        if e.get("purefa_volume_performance", {}).get("volume") == "capped"
    )
    assert capped["normal_polls"] == 2


def test_cap_tag_on_a_missing_volume_does_not_fail_the_poll(events):
    client = FakeClient(
        volumes_perf=[vol_perf("busy-1", 5000, 500), vol_perf("capped", 100, 0)],
        tags=[("capped", "qos_cap", "t"), ("gone", "qos_cap", "t")],
    )
    out = events._performance_events(
        FakeArray(client), {}, {"top_volumes": 1, "baseline": False}
    )
    vols = {
        e["purefa_volume_performance"]["volume"]
        for e in out
        if "purefa_volume_performance" in e
    }
    assert vols == {"busy-1", "capped"}


def test_counters_are_pruned_for_volumes_no_longer_seen(events):
    client = FakeClient(volumes_perf=[vol_perf("a", 5000, 10), vol_perf("b", 10, 10)])
    arr = FakeArray(client)
    state = {}
    events._performance_events(arr, state, {"top_volumes": 5, "baseline": False})
    client.volumes_perf = [vol_perf("a", 5000, 10)]
    events._performance_events(arr, state, {"top_volumes": 5, "baseline": False})
    assert set(state["telemetry"]["array-a"]["high_latency_polls"]) == {"a"}
    assert state["telemetry"]["array-a"]["high_latency_polls"]["a"] == 2


# --- protection group transfers ---------------------------------------------
def transfer(pg, i, completed=None, progress=None, data=None):
    return {
        "name": f"{pg}.{i}",
        "started": None,  # null even on completed rows, as the array reports
        "completed": completed,
        "progress": progress,
        "data_transferred": data,
        "destroyed": False,
    }


def pg_client(transfers):
    return FakeClient(
        pgroups=[
            {
                "name": "pg-prod",
                "replication_schedule": {"enabled": True, "frequency": HOUR_MS},
            }
        ],
        transfers=transfers,
    )


def test_pg_transfers_are_fetched_newest_first(events):
    # 25 retained transfers in name order; 1..24 completed hourly, the 25th
    # in progress. An unsorted page of 20 would end at the 20th: overdue by
    # five hours and no transfer in progress.
    rows = [
        transfer("pg-prod", i, completed=NOW_MS - (25 - i) * HOUR_MS)
        for i in range(1, 25)
    ]
    rows.append(transfer("pg-prod", 25, progress=0.4, data=1024))
    client = pg_client(rows)
    out = events.poll_replication(FakeArray(client), {}, {})
    pg = next(e["purefa_pg_replication"] for e in out if "purefa_pg_replication" in e)
    kw = next(
        kw for n, kw in client.calls if n == "get_protection_group_snapshots_transfer"
    )
    assert kw["sort"] == ["completed-"] and kw["destroyed"] is False
    assert kw["limit"] >= 25
    assert pg["last_completed"] == NOW_MS - HOUR_MS
    assert pg["overdue"] is False
    assert pg["in_progress"]["name"] == "pg-prod.25"
    assert pg["in_progress"]["progress"] == 0.4
    assert pg["targets"] == ["array-b"]
    assert pg["stuck_polls"] == 0 and pg["stuck"] is False


def test_pg_overdue_and_stuck(events):
    rows = [transfer("pg-prod", 1, completed=NOW_MS - 5 * HOUR_MS)]
    rows.append(transfer("pg-prod", 2, progress=0.1, data=10))
    client = pg_client(rows)
    arr = FakeArray(client)
    state = {}
    args = {"stuck_transfer_polls": 3}

    def poll():
        out = events.poll_replication(arr, state, args)
        return next(
            e["purefa_pg_replication"] for e in out if "purefa_pg_replication" in e
        )

    pg = poll()
    assert pg["overdue"] is True and pg["stuck_polls"] == 0
    assert poll()["stuck_polls"] == 1
    assert poll()["stuck_polls"] == 2
    pg = poll()
    assert pg["stuck_polls"] == 3 and pg["stuck"] is True
    # bytes moving with the same progress value resets the counter
    client.transfers[1]["data_transferred"] = 20
    pg = poll()
    assert pg["stuck_polls"] == 0 and pg["stuck"] is False
    # the transfer completes: nothing in progress, no longer overdue
    client.transfers[1].update(completed=NOW_MS, progress=None)
    pg = poll()
    assert pg["in_progress"] is None and pg["overdue"] is False


# --- capacity events across arrays -----------------------------------------
def test_capacity_events_list_the_other_arrays_least_used_first(events):
    rows = [
        {"array": "a", "used_pct": 50.0},
        {"array": "b", "used_pct": 20.0},
        {"array": "c", "used_pct": None},
    ]
    out = events._capacity_events(rows)
    assert [e["purefa_capacity"]["array"] for e in out] == ["a", "b", "c"]
    assert out[0]["purefa_capacity"]["others"] == [
        {"array": "b", "used_pct": 20.0},
        {"array": "c", "used_pct": None},
    ]
    assert out[2]["purefa_capacity"]["others"][0]["array"] == "b"
    assert events._capacity_events([]) == []


def test_growth_volumes_argument_bounds_the_space_query(events):
    client = FakeClient()
    events._capacity_row(FakeArray(client), {}, {"growth_volumes": 50})
    kw = next(kw for n, kw in client.calls if n == "get_volumes_space")
    assert kw["limit"] == 50 and kw["destroyed"] is False
    client.calls.clear()
    events._capacity_row(FakeArray(client), {}, {})
    assert (
        next(kw for n, kw in client.calls if n == "get_volumes_space")["limit"] == 500
    )


def test_threshold_arguments_and_aliases(events):
    arr = FakeArray(FakeClient())
    # drr 3.0 now and in the (single-point) history: drop 0 %, never a drop
    row = events._capacity_row(
        arr, {}, {"drr_drop_threshold_pct": 0, "drr_drop_pct": 99}
    )
    assert row["drr_drop_pct"] == 0.0 and row["drr_drop"] is True  # primary name wins
    row = events._capacity_row(arr, {}, {"drr_drop_pct": 0})
    assert row["drr_drop"] is True  # alias accepted
    row = events._capacity_row(arr, {}, {})
    assert row["drr_drop"] is False and row["time"] == NOW_MS


# --- the asyncio driver: telemetry under the per-array lock -------------------
class LockedArray(FakeArray):
    def __init__(self, client, name):
        super().__init__(client, name)
        self.lock = asyncio.Lock()
        self.resets = 0

    def reset(self):
        self.resets += 1


def test_signal_loop_telemetry_polls_each_array_under_its_lock(events):
    """One cycle of the telemetry signal: both pollers run per array through
    run() (lock held, reset() on failure), the failing array yields one
    purefa_poll_error and no performance events, and the capacity events
    come last, after every array's row is in."""
    good = LockedArray(FakeClient(volumes_perf=[vol_perf("a", 10, 10)]), "array-a")
    bad_client = FakeClient()
    bad_client.get_arrays_space = lambda **kw: FakeError("array down")
    bad = LockedArray(bad_client, "array-b")
    got = []

    async def go():
        done = asyncio.Event()

        class Q:
            async def put(self, event):
                # emitted after run() returned, never while an array lock is held
                assert good.lock.locked() is False and bad.lock.locked() is False
                got.append(event)
                if "purefa_capacity" in event:
                    done.set()

        with ThreadPoolExecutor(max_workers=3) as ex:
            task = asyncio.create_task(
                events._signal_loop(
                    Q(), "telemetry", [good, bad], {}, {"baseline": False}, ex
                )
            )
            await asyncio.wait_for(done.wait(), 10)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(go())
    kinds = [next(iter(e)) for e in got]
    assert kinds == [
        "purefa_array_performance",
        "purefa_volume_performance",
        "purefa_poll_error",
        "purefa_capacity",
    ]
    err = got[2]["purefa_poll_error"]
    assert err == {
        "signal": "telemetry",
        "array": "array-b",
        "error": "arrays/space: array down",
        "consecutive": 1,
        "escalate": False,
    }
    assert bad.resets == 1 and good.resets == 0
    cap = got[3]["purefa_capacity"]
    assert cap["array"] == "array-a" and cap["others"] == []
    assert cap["used_pct"] == 50.0


def test_signal_loop_other_signals_report_a_failed_array_and_carry_on(events):
    good = LockedArray(FakeClient(alerts=[alert("1")]), "array-a")
    bad_client = FakeClient()
    bad_client.get_alerts = lambda **kw: FakeError("nope")
    bad = LockedArray(bad_client, "array-b")
    got = []

    async def go():
        done = asyncio.Event()

        class Q:
            async def put(self, event):
                got.append(event)
                if len(got) == 2:
                    done.set()

        with ThreadPoolExecutor(max_workers=3) as ex:
            task = asyncio.create_task(
                events._signal_loop(Q(), "alerts", [bad, good], {}, {}, ex)
            )
            await asyncio.wait_for(done.wait(), 10)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(go())
    assert got[0]["purefa_poll_error"]["array"] == "array-b"
    assert got[0]["purefa_poll_error"]["signal"] == "alerts"
    assert got[1]["purefa_alert"]["array"] == "array-a"
    assert bad.resets == 1


def test_signal_loop_counts_a_streak_and_escalates_at_the_threshold(events):
    # fail, fail, succeed, fail: the streak reaches the threshold on the
    # second failure, the success ends it, and the next failure starts again
    client = FakeClient(alerts=[alert("1")])
    outcomes = iter([False, False, True, False])
    real_get_alerts = client.get_alerts

    def get_alerts(**kw):
        return real_get_alerts(**kw) if next(outcomes, True) else FakeError("nope")

    client.get_alerts = get_alerts
    arr = LockedArray(client, "array-b")
    got = []

    async def go():
        done = asyncio.Event()

        class Q:
            async def put(self, event):
                got.append(event)
                if len(got) >= 4:  # error, error, the initial alert, error
                    done.set()

        args = {"alerts_interval": 1, "poll_error_escalate": 2}
        with ThreadPoolExecutor(max_workers=2) as ex:
            task = asyncio.create_task(
                events._signal_loop(Q(), "alerts", [arr], {}, args, ex)
            )
            await asyncio.wait_for(done.wait(), 20)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(go())
    errors = [e["purefa_poll_error"] for e in got if "purefa_poll_error" in e]
    assert [(e["consecutive"], e["escalate"]) for e in errors] == [
        (1, False),
        (2, True),
        (1, False),
    ]
    assert any("purefa_alert" in e for e in got)


def test_normal_polls_count_from_the_cap(events):
    # a volume that has read normal for a while is capped by hand (the day-2
    # route writes the tag): the lift's counter starts again at the cap
    client = FakeClient(volumes_perf=[vol_perf("v", 100, 500)], tags=[])
    arr = FakeArray(client)
    state = {}
    args = {"top_volumes": 5, "min_iops": 1, "baseline": False}

    def volume_event():
        out = events._performance_events(arr, state, args)
        return next(
            e["purefa_volume_performance"]
            for e in out
            if e.get("purefa_volume_performance", {}).get("volume") == "v"
        )

    assert volume_event()["normal_polls"] == 1
    assert volume_event()["normal_polls"] == 2
    client.tags.append(("v", "qos_cap", "2026-11-09T15:48:41"))
    client.tags.append(("v", "qos_prev_iops", "none"))
    capped = volume_event()
    assert capped["eda_capped"] is True
    assert capped["normal_polls"] == 0
    assert volume_event()["normal_polls"] == 1
    assert state["telemetry"]["array-a"]["capped_last_poll"] == ["v"]
