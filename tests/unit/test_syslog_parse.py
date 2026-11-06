# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Parser, source filter and listener-plumbing tests for the purefa_syslog
event source. Nothing here binds a socket."""

import asyncio

import pytest
import yaml

AUDIT = (
    "<182>Oct 20 18:00:00 array-a purity.audit: (operation message ID: 676887) "
    "Array name: 'array-a' Controller: 0 Interface: 'CLI' Module: 'purevol' "
    "Session: 'abc124' UTC Time: 2026-10-20T22:00:00Z User: 'eda-human' "
    "Location '198.51.100.9' Sublocation: 'ssh' Action: 'purevol destroy prod-vol' "
    "Method: '' Result: Success Description: 'volume destroyed'"
)
ALERT = "<180>Oct 20 18:00:00 array-a purity.alert: (hardware:ct0.eth7): failure [code 42, severity warning]"
TEST = "<182>Oct 20 18:00:00 array-a purity.test: This is a test message"
RFC5424 = "<182>1 2026-10-20T22:00:00Z array-a purity.test - - - structured header test"


def test_audit_fields_are_parsed(syslog):
    e = syslog.parse(AUDIT, {}, set())
    assert e["kind"] == "audit"
    assert e["array"] == "array-a"
    assert e["host"] == "array-a" and e["program"] == "purity.audit"
    assert e["user"] == "eda-human"
    assert e["action"] == "purevol destroy prod-vol"
    assert e["result"] == "Success"
    assert e["fields"]["array_name"] == "array-a"  # a lower-case second word
    assert e["fields"]["interface"] == "CLI"
    assert e["fields"]["module"] == "purevol" and e["fields"]["session"] == "abc124"
    assert e["fields"]["location"] == "198.51.100.9"
    assert e["fields"]["sublocation"] == "ssh" and e["fields"]["method"] == ""
    assert e["fields"]["message_id"] == "676887"
    assert e["fields"]["message_kind"] == "operation"
    # "UTC Time: ...Z User:" must not produce a key called "z_user"
    assert "z_user" not in e["fields"]
    # "Result: Success Description:" must not produce "success_description"
    assert "success_description" not in e["fields"]
    assert e["fields"]["description"] == "volume destroyed"
    assert e["severity"] == "info" and e["facility"] == 22


def test_automation_user_is_flagged(syslog):
    assert syslog.parse(AUDIT, {}, {"eda-human"})["by_automation"] is True
    assert syslog.parse(AUDIT, {}, {"svc-eda"})["by_automation"] is False


def test_automation_users_string_form_matches_whole_names(syslog):
    """A comma-separated string names users, never single characters."""
    users = set(syslog._as_list("svc-eda, eda-human"))
    assert users == {"svc-eda", "eda-human"}
    assert syslog.parse(AUDIT, {}, users)["by_automation"] is True
    assert (
        syslog.parse(AUDIT, {}, set(syslog._as_list("svc-eda")))["by_automation"]
        is False
    )
    assert syslog._as_list(None) == [] and syslog._as_list(["a"]) == ["a"]


def test_alert_and_test_lines(syslog):
    a = syslog.parse(ALERT, {}, set())
    assert a["kind"] == "alert" and a["severity"] == "warning"
    assert a["message"].startswith("(hardware:ct0.eth7)")
    assert a["user"] is None
    t = syslog.parse(TEST, {}, set())
    assert t["kind"] == "test" and t["message"] == "This is a test message"


def test_rfc5424_header(syslog):
    e = syslog.parse(RFC5424, {}, set())
    assert e["kind"] == "test" and e["host"] == "array-a"
    assert e["message"] == "structured header test"


def test_hostname_mapping(syslog):
    line = ALERT.replace("array-a", "fa-short")
    assert syslog.parse(line, {"fa-short": "array-a"}, set())["array"] == "array-a"


def test_hostname_map_applies_to_the_array_name_field_too(syslog):
    # audits carry "Array name: 'x'"; the map renames that as well
    assert (
        syslog.parse(AUDIT, {"array-a": "inventory-a"}, set())["array"] == "inventory-a"
    )
    # the array's own name wins when both candidates are mapped
    line = AUDIT.replace(
        "<182>Oct 20 18:00:00 array-a", "<182>Oct 20 18:00:00 fa-short"
    )
    both = {"array-a": "by-array-name", "fa-short": "by-host"}
    assert syslog.parse(line, both, set())["array"] == "by-array-name"
    # only the hostname is mapped: it is still consulted
    assert syslog.parse(line, {"fa-short": "by-host"}, set())["array"] == "by-host"
    # nothing mapped: the array's own name passes through, host is kept raw
    e = syslog.parse(line, {}, set())
    assert e["array"] == "array-a" and e["host"] == "fa-short"


def test_garbage_is_kind_other_with_the_full_event_shape(syslog):
    e = syslog.parse("garbage line", {}, set())
    assert e["kind"] == "other" and e["array"] is None and e["raw"] == "garbage line"
    assert e["message"] == "garbage line"
    assert set(e) == set(syslog.EVENT_KEYS)
    assert set(syslog.parse(AUDIT, {}, set())) == set(syslog.EVENT_KEYS)
    assert e["fields"] == {} and e["by_automation"] is False
    for key in (
        "host",
        "severity",
        "facility",
        "time",
        "program",
        "user",
        "action",
        "result",
    ):
        assert e[key] is None, key


def test_documentation_block_covers_every_argument(syslog):
    import pathlib
    import re

    doc = yaml.safe_load(syslog.DOCUMENTATION)
    assert doc["name"] == "purefa_syslog"
    options = set(doc["options"])
    source = pathlib.Path(syslog.__file__).read_text()
    read = set(re.findall(r'args\.get\(\s*"([a-z_]+)"', source))
    assert read <= options, f"undocumented arguments: {sorted(read - options)}"
    assert "allowed_sources" in options
    assert yaml.safe_load(syslog.EXAMPLES)[0]["sources"][0][
        "sdodsley.flasharray_eda.purefa_syslog"
    ]


# --- source allow-list -------------------------------------------------------
def test_allowed_sources_accept_lists_and_strings(syslog):
    nets = syslog._networks(["198.51.100.0/24", "192.0.2.5", "2001:db8:1::/48"])
    assert [str(n) for n in nets] == [
        "198.51.100.0/24",
        "192.0.2.5/32",
        "2001:db8:1::/48",
    ]
    assert [str(n) for n in syslog._networks("198.51.100.0/24, 192.0.2.5")] == [
        "198.51.100.0/24",
        "192.0.2.5/32",
    ]
    assert syslog._networks(None) == [] and syslog._networks("") == []
    with pytest.raises(ValueError):
        syslog._networks(["not-an-address"])


def test_source_allowed(syslog):
    nets = syslog._networks(["198.51.100.0/24", "192.0.2.5", "2001:db8:1::/48"])
    assert syslog._source_allowed("198.51.100.3", nets)
    assert syslog._source_allowed("192.0.2.5", nets)
    assert syslog._source_allowed("2001:db8:1::1", nets)
    assert syslog._source_allowed("fe80::1%eth0", nets) is False
    assert syslog._source_allowed("::ffff:198.51.100.3", nets)  # dual-stack socket
    assert syslog._source_allowed("192.0.2.6", nets) is False
    assert syslog._source_allowed("203.0.113.8", nets) is False
    assert syslog._source_allowed(None, nets) is False
    assert syslog._source_allowed("garbage", nets) is False
    # no allow-list: everything, even an unknown peer, is accepted
    assert syslog._source_allowed("203.0.113.8", []) and syslog._source_allowed(
        None, []
    )


def test_emitter_filters_senders_and_queues_locally(syslog):
    async def go():
        local = asyncio.Queue()
        emit = syslog._emitter(
            local, {}, {"eda-human"}, syslog._networks(["198.51.100.0/24"])
        )
        assert emit(AUDIT, "198.51.100.9", "udp") is True
        assert emit(TEST, "192.0.2.1", "udp") is False
        assert emit(TEST, "198.51.100.9", "tcp") is True
        assert local.qsize() == 2
        first = local.get_nowait()
        assert first["kind"] == "audit" and first["by_automation"] is True
        assert first["source"] == "198.51.100.9" and first["protocol"] == "udp"
        assert local.get_nowait()["protocol"] == "tcp"

    asyncio.run(go())


def test_udp_protocol_splits_datagrams_into_lines(syslog):
    got = []
    proto = syslog._UDP(
        lambda line, source, protocol: got.append((line, source, protocol))
    )
    proto.datagram_received(f"{TEST}\n\n{ALERT}\n".encode(), ("198.51.100.9", 514))
    assert [g[0] for g in got] == [TEST, ALERT]
    assert got[0][1:] == ("198.51.100.9", "udp")


# --- tcp client plumbing ---------------------------------------------------
class FakeReader:
    def __init__(self, items):
        self.items = list(items)

    def at_eof(self):
        return not self.items

    async def readline(self):
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeWriter:
    def __init__(self, peer):
        self.peer = peer
        self.closed = False
        self.waited = False

    def get_extra_info(self, name):
        return self.peer

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


def test_tcp_client_emits_lines_and_closes_on_an_over_long_line(syslog):
    got = []
    reader = FakeReader(
        [f"{TEST}\n".encode(), b"\n", ValueError("too long"), b"never\n"]
    )
    writer = FakeWriter(("198.51.100.9", 40000))
    asyncio.run(
        syslog._tcp_client(
            reader, writer, lambda l, s, p: got.append((l.strip(), s, p)), []
        )
    )
    assert got == [(TEST, "198.51.100.9", "tcp")]
    assert writer.closed and writer.waited
    assert reader.items == [b"never\n"]  # the connection was dropped, not drained


def test_tcp_client_refuses_a_peer_outside_allowed_sources(syslog):
    got = []
    reader = FakeReader([f"{TEST}\n".encode()])
    writer = FakeWriter(("192.0.2.1", 40000))
    nets = syslog._networks(["198.51.100.0/24"])
    asyncio.run(syslog._tcp_client(reader, writer, lambda *a: got.append(a), nets))
    assert got == [] and writer.closed and writer.waited
    assert reader.items  # nothing was read from the refused peer


# --- main() validation -------------------------------------------------------
class _Q:
    async def put(self, event):
        raise AssertionError("nothing should be queued")


def test_main_rejects_the_purefa_events_array_list(syslog):
    with pytest.raises(ValueError, match="mapping"):
        asyncio.run(syslog.main(_Q(), {"arrays": [{"name": "array-a", "fa_url": "x"}]}))


def test_arrays_argument_accepts_a_json_or_yaml_string(syslog):
    want = {"fa-short": "array-a"}
    assert syslog._decode_arrays('{"fa-short": "array-a"}', "purefa_syslog") == want
    assert syslog._decode_arrays("fa-short: array-a\n", "purefa_syslog") == want
    assert syslog._decode_arrays(want, "purefa_syslog") is want
    assert syslog._decode_arrays("", "purefa_syslog") is None
    # decoded before the type check: a string holding the purefa_events list
    # is refused as a list, a string that is neither JSON nor YAML as such
    with pytest.raises(ValueError, match="mapping"):
        asyncio.run(syslog.main(_Q(), {"arrays": '[{"name": "array-a"}]'}))
    with pytest.raises(ValueError, match="JSON") as exc:
        asyncio.run(syslog.main(_Q(), {"arrays": "{not json: s3cret"}))
    assert "s3cret" not in str(exc.value)


def test_main_rejects_a_bad_allowed_source_before_listening(syslog):
    with pytest.raises(ValueError):
        asyncio.run(syslog.main(_Q(), {"allowed_sources": ["198.51.100.0/24", "nope"]}))
