# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
purefa_syslog.py - ansible-rulebook event source that listens for FlashArray
syslog messages (push path, sub-second).

Point the array at this host (GUI: Settings > System > Syslog Servers, or
everpure.flasharray.purefa_syslog with a tcp:// or udp:// URI). Purity sends
three programs: purity.alert, purity.audit and purity.test. Each line is
turned into one event; the audit lines are parsed into fields so rules can
match on user, action and result without regexes.

Arguments
---------
host              bind address (default 0.0.0.0; bind to the interface the
                  arrays reach)
port              port (default 5140; 514 needs root or a capability)
protocols         list or comma-separated string, udp and/or tcp (default
                  both)
arrays            optional mapping array name or syslog hostname -> inventory
                  host name, for arrays whose own name or logged hostname
                  differs from the inventory (default {}). It must be a
                  mapping; the purefa_events array list is rejected at
                  start-up. A JSON string (or a YAML string) that decodes to
                  the mapping is accepted too, so a platform credential can
                  inject it as one variable; it is decoded before validation
                  and never echoed into an error or a log.
automation_users  list or comma-separated string of user names counted as
                  automation (default: empty)
allowed_sources   list or comma-separated string of IPv4/IPv6 addresses or
                  CIDR prefixes allowed to send; datagrams and connections
                  from any other address are dropped and logged at debug
                  level (default: empty = accept any sender)

Security
--------
Syslog carries no authentication. The array name is taken from the message
text and a UDP source address can be forged, so `allowed_sources` is hygiene
rather than authentication. Prefer `protocols: [tcp]` plus a host firewall
limited to the arrays' management addresses, and let syslog events drive
notifications only; remediation belongs to the REST plugin's audit and
alert records.

Events (one dict per line, top-level key purefa_syslog)
-------------------------------------------------------
kind           alert | audit | test | other
array          inventory name: the message's "Array name" field, else the
               syslog hostname, whichever is first found in `arrays`
               (unmapped names pass through unchanged)
host           syslog hostname from the header, before mapping
severity       syslog severity word from the PRI (emerg..debug)
facility       syslog facility number
time           timestamp text from the header
program        purity.alert | purity.audit | purity.test | other program
message        the text after the program name
fields         key -> value for every `Key: 'value'` pair in an audit
               message (User, Interface, Session, Location, Action, Method,
               Result, Description, ...), lower-cased keys with spaces as
               underscores, plus message_id and message_kind from the
               "(login message ID: N)" prefix when present
user           fields.user when present
action         fields.action when present
result         fields.result when present
by_automation  user in automation_users
source         sender ip
protocol       udp | tcp
raw            the whole line

A line with no recognisable syslog header arrives with the same keys: kind
other, message and raw set to the line, fields {}, by_automation false and
every other field (array included) None; it is never named after the sender.

Format verification: the audit parsing follows the message layout published
by Pure's SIEM partners (`<PRI>Mon DD HH:MM:SS host purity.audit: (login
message ID: N) Array name: 'x' Controller: 0 Interface: 'REST' ... User: 'u'
... Action: '...' Method: '' Result: Success Description: ''`). Not
verified against a live array; tests/syslog_send.py replays representative
lines.
"""

DOCUMENTATION = r"""
---
name: purefa_syslog
short_description: Receive Everpure FlashArray syslog messages as events.
description:
  - Listens on UDP and/or TCP for syslog lines from FlashArrays and emits one
    C(purefa_syslog) event per line. C(purity.audit) lines are parsed into
    fields (user, action, result, ...) so rules can match without regexes.
  - Syslog carries no authentication and a UDP source address can be forged.
    Bind to the interface the arrays reach, prefer TCP behind a host
    firewall, and drive notifications from these events, not remediation.
options:
  host:
    description: Bind address.
    type: str
    default: 0.0.0.0
  port:
    description: Listening port; 514 needs root or a capability.
    type: int
    default: 5140
  protocols:
    description: Transports to listen on, udp and/or tcp (list or comma-separated string).
    type: list
    elements: str
    default: [udp, tcp]
  arrays:
    description:
      - Mapping of an array's own name or syslog hostname to the inventory
        host name, for arrays whose names differ from the inventory. Must be
        a mapping; the purefa_events array list is rejected at start-up.
      - A JSON string is accepted so a platform credential can inject the
        mapping as one extra var (a YAML string decodes the same way); the
        string is decoded before validation and is never written to a log.
    type: dict
    default: {}
  automation_users:
    description: User names whose audit lines count as automation (list or comma-separated string).
    type: list
    elements: str
    default: []
  allowed_sources:
    description:
      - IPv4/IPv6 addresses or CIDR prefixes allowed to send (list or
        comma-separated string). Empty accepts any sender. This is hygiene,
        not authentication.
    type: list
    elements: str
    default: []
"""

EXAMPLES = r"""
- name: FlashArray syslog
  hosts: all
  sources:
    - sdodsley.flasharray_eda.purefa_syslog:
        host: "{{ syslog_host }}"
        port: "{{ syslog_port }}"
        protocols: [tcp]
        arrays: "{{ syslog_hostnames }}"
        automation_users: "{{ automation_users }}"
        allowed_sources: "{{ syslog_allowed_sources }}"
  rules:
    - name: Human action via syslog audit - notify
      condition: >-
        event.purefa_syslog.kind == "audit" and
        event.purefa_syslog.by_automation == false
      action:
        debug:
          msg: "{{ event.purefa_syslog.user }} on {{ event.purefa_syslog.array }}: {{ event.purefa_syslog.action }}"
"""

import asyncio
import ipaddress
import json
import logging
import re

try:
    import yaml
except ImportError:  # ansible-rulebook ships PyYAML; a bare interpreter may not
    yaml = None

logger = logging.getLogger(__name__)

SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
# <PRI>Mon DD HH:MM:SS host program: message      (RFC 3164 style)
RFC3164 = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<time>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
    r"(?P<prog>purity\.\w+|\S+?):\s*(?P<msg>.*)$"
)
# <PRI>1 2026-10-20T18:00:00Z host program - - - message   (RFC 5424 style)
RFC5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>1\s+(?P<time>\S+)\s+(?P<host>\S+)\s+(?P<prog>\S+)\s+\S+\s+\S+\s+(?:\[.*?\]|-)\s*(?P<msg>.*)$"
)
# Key: 'value' pairs; the key starts with a capital after whitespace (so "UTC
# Time: 2026-...Z User: 'u'" yields User, not "Z User"), later words may be
# lower case ("Array name: 'x'") and the colon is optional because the
# published layout has "Location '203.0.113.1'" without one.
FIELD = re.compile(r"(?:^|(?<=\s))([A-Z][A-Za-z]*(?: [A-Za-z]+)*):?\s*'([^']*)'")
RESULT = re.compile(r"\bResult:\s*(\w+)")
MSGID = re.compile(r"\((?P<what>[^)]*?)\s*message ID:\s*(?P<id>\d+)\)")

# Every event carries these keys, parsed or not, so a rule that reads
# `severity` or `array` never meets a different shape.
EVENT_KEYS = (
    "kind",
    "array",
    "host",
    "severity",
    "facility",
    "time",
    "program",
    "message",
    "fields",
    "user",
    "action",
    "result",
    "by_automation",
    "raw",
)
# An RFC 3164/5424 syslog line is a few KiB at most; a longer "line" is not
# syslog and closes the TCP connection instead of growing the buffer.
TCP_LINE_LIMIT = 16384


def _as_list(value):
    # same helper as purefa_events._as_list, copied rather than imported so
    # this plugin does not pull in pypureclient
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def _networks(spec):
    """allowed_sources -> list of ip_network objects; [] means accept any."""
    return [
        ipaddress.ip_network(str(item).strip(), strict=False) for item in _as_list(spec)
    ]


def _source_allowed(source, networks):
    """True when `source` is inside one of `networks` (or there are none)."""
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(str(source).split("%", 1)[0])
    except ValueError:
        return False
    candidates = [addr]
    if addr.version == 6 and addr.ipv4_mapped:
        candidates.append(addr.ipv4_mapped)  # dual-stack socket: ::ffff:203.0.113.1
    return any(a in n for a in candidates for n in networks)


def _array_name(fields, host, arrays):
    """The event's array: the message's own "Array name", else the syslog
    hostname; the first of the two found in `arrays` is mapped, otherwise
    the name passes through unchanged."""
    candidates = [c for c in (fields.get("array_name"), host) if c]
    for c in candidates:
        if c in arrays:
            return arrays[c]
    return candidates[0] if candidates else None


def parse(line, arrays, automation):
    line = line.strip()
    event = dict.fromkeys(EVENT_KEYS)
    event.update(kind="other", message=line, fields={}, by_automation=False, raw=line)
    m = RFC3164.match(line) or RFC5424.match(line)
    if not m:
        return event
    pri = int(m.group("pri"))
    prog = m.group("prog")
    msg = m.group("msg")
    kind = (prog.split(".", 1)[1] if prog.startswith("purity.") else "") or "other"
    r = RESULT.search(msg)
    # take "Result: Success" out before pairing keys, or the next key reads as
    # "Success Description"
    body = RESULT.sub(" ", msg) if r else msg
    fields = {k.strip().lower().replace(" ", "_"): v for k, v in FIELD.findall(body)}
    if r:
        fields["result"] = r.group(1)
    mid = MSGID.search(msg)
    if mid:
        fields["message_id"] = mid.group("id")
        fields["message_kind"] = mid.group("what").strip()
    host = m.group("host")
    user = fields.get("user")
    event.update(
        kind=kind,
        array=_array_name(fields, host, arrays),
        host=host,
        severity=SEVERITIES[pri % 8],
        facility=pri // 8,
        time=m.group("time"),
        program=prog,
        message=msg,
        fields=fields,
        user=user,
        action=fields.get("action"),
        result=fields.get("result"),
        by_automation=bool(user and user in automation),
    )
    return event


def _emitter(local, arrays, automation, networks):
    """Build the synchronous emit(line, source, protocol) callback: filter the
    sender, parse, and hand the event to the local queue (unbounded, so
    put_nowait never fails and the UDP callback never blocks)."""

    def emit(line, source, protocol):
        if not _source_allowed(source, networks):
            logger.debug(
                "purefa_syslog: dropped %s line from %s (allowed_sources)",
                protocol,
                source,
            )
            return False
        event = parse(line, arrays, automation)
        event["source"] = source
        event["protocol"] = protocol
        local.put_nowait(event)
        return True

    return emit


class _UDP(asyncio.DatagramProtocol):
    def __init__(self, emit):
        self.emit = emit

    def datagram_received(self, data, addr):
        for line in data.decode("utf-8", "replace").splitlines():
            if line.strip():
                self.emit(line, addr[0], "udp")


async def _tcp_client(reader, writer, emit, networks):
    peer = writer.get_extra_info("peername")
    source = peer[0] if peer else None
    try:
        if not _source_allowed(source, networks):
            logger.debug(
                "purefa_syslog: refused tcp connection from %s (allowed_sources)",
                source,
            )
            return
        while not reader.at_eof():
            try:
                raw = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # readline raises ValueError once a line exceeds the limit
                logger.warning(
                    "purefa_syslog: line from %s longer than %d bytes; closing",
                    source,
                    TCP_LINE_LIMIT,
                )
                return
            if not raw:
                break
            line = raw.decode("utf-8", "replace")
            if line.strip():
                emit(line, source, "tcp")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _decode_arrays(value, plugin):
    """The arrays argument as the structure the plugin documents.

    A platform credential injects the list (or mapping) as one string
    variable, so a JSON string, or a YAML string, is decoded here before the
    argument is validated. The text is never echoed into an error message or
    a log record: it carries API tokens.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if not isinstance(decoded, (list, dict)) and yaml is not None:
        try:
            decoded = yaml.safe_load(text)
        except yaml.YAMLError:
            decoded = None
    if isinstance(decoded, (list, dict)):
        return decoded
    raise ValueError(
        f"{plugin}: arrays is a string that does not decode as JSON (or YAML) "
        "into the documented structure; the text is not shown because it may "
        "hold API tokens"
    )


async def main(queue: asyncio.Queue, args: dict) -> None:
    host = args.get("host") or "0.0.0.0"
    port = int(args.get("port", 5140))
    protocols = _as_list(args.get("protocols")) or ["udp", "tcp"]
    arrays = _decode_arrays(args.get("arrays"), "purefa_syslog") or {}
    if not isinstance(arrays, dict):
        raise ValueError(
            "purefa_syslog: arrays must be a mapping of array name or syslog "
            "hostname -> inventory name, not the purefa_events array list"
        )
    automation = set(_as_list(args.get("automation_users")))
    networks = _networks(args.get("allowed_sources"))
    loop = asyncio.get_running_loop()

    # One local unbounded queue and one forwarder task: events keep their
    # order, there is one task instead of one per line, and ansible-rulebook's
    # own (one-slot) queue applies back-pressure to the forwarder only.
    local: asyncio.Queue = asyncio.Queue()
    emit = _emitter(local, arrays, automation, networks)

    async def forward():
        while True:
            event = await local.get()
            await queue.put({"purefa_syslog": event})

    forwarder = asyncio.create_task(forward())
    servers = []
    if "udp" in protocols:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDP(emit), local_addr=(host, port)
        )
        servers.append(transport)
    if "tcp" in protocols:
        srv = await asyncio.start_server(
            lambda r, w: _tcp_client(r, w, emit, networks),
            host,
            port,
            limit=TCP_LINE_LIMIT,
        )
        servers.append(srv)
    logger.info(
        "purefa_syslog: listening on %s:%s (%s)%s",
        host,
        port,
        ",".join(protocols),
        f", sources {[str(n) for n in networks]}" if networks else ", any source",
    )
    try:
        await asyncio.Event().wait()
    finally:
        forwarder.cancel()
        for s in servers:
            s.close()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if len(sys.argv) > 1 and sys.argv[1] == "parse":
        for line in sys.stdin:
            print(json.dumps(parse(line, {}, set())))
        sys.exit(0)

    class _Q:
        async def put(self, event):
            print(json.dumps(event), flush=True)

    asyncio.run(main(_Q(), {"port": int(sys.argv[1]) if len(sys.argv) > 1 else 5140}))
