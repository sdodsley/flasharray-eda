# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
purefa_syslog.py - ansible-rulebook event source that listens for FlashArray
syslog messages (push path, sub-second).

Point the array at this host (GUI: Settings > System > Syslog Servers, or
everpure.flasharray.purefa_syslog with a tcp:// or udp:// URI). Purity sends
three programs of its own: purity.alert, purity.audit and purity.test. Each
line is turned into one event; the audit lines are parsed into fields so
rules can match on user, action and result without regexes. A syslog target
also receives the controllers' whole system log (sshd, sudo, kernel and the
rest), so by default only the three Purity programs are emitted and every
other program is dropped and counted.

Arguments
---------
host              bind address (default 0.0.0.0; bind to the interface the
                  arrays reach)
port              port (default 5140; 514 needs root or a capability)
protocols         list or comma-separated string, udp and/or tcp (default
                  both)
programs          list or comma-separated string of syslog programs to emit
                  (default purity.alert, purity.audit, purity.test). Lines
                  from any other program, and lines with no syslog header,
                  are dropped and counted; `all` (or an empty list) passes
                  everything through as kind `other`.
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
system_users      list or comma-separated string of the array's own service
                  accounts (default pure_datainsights); their audit lines
                  count as automation, as do command records whose auth
                  source is `system` (the array's `purity` account)
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
host           syslog hostname from the header, before mapping (Purity logs
               the controller's name, `<array>-ct0`; `array` is the array)
severity       syslog severity word from the PRI (emerg..debug); the array
               sends no PRI, so it is taken from a leading level word or a
               "severity <word>" phrase in the text when there is one, else
               None
facility       syslog facility number, None without a PRI
time           timestamp text from the header (the array's local time; the
               audit and test messages also carry `fields.utc_time`)
program        purity.alert | purity.audit | purity.test | other program,
               without the `[pid]` Purity appends
message        the text after the program name
fields         audit lines come in two layouts and both are parsed:
               - a command: `[user (auth)]@array(id) command arguments.
                 Message ID: N UTC Time: ... Array Name: x` gives user,
                 auth_source, array_name, array_id, command, subcommand,
                 arguments, action (the whole command line), message_id,
                 utc_time and message_kind `command`;
               - a session record: `(login message ID: N) Array name: 'x'
                 ... User: 'u' Action: 'session request' ...` gives every
                 `Key: 'value'` pair (lower-cased keys, spaces as
                 underscores), result, message_id and message_kind `login`
                 (`operation` and others as the prefix says).
               Every audit line carries message_kind (`other` when neither
               layout matched); test lines carry utc_time and array_name.
user           fields.user when present
action         fields.action when present
result         fields.result when present
by_automation  user in automation_users or system_users, or the command's
               auth source is `system`
source         sender ip
protocol       udp | tcp
raw            the whole line

A line with no recognisable syslog header arrives with the same keys: kind
other, message and raw set to the line, fields {}, by_automation false and
every other field (array included) None; it is never named after the sender.

Format verification: the layouts above are the ones Purity//FA 6.10.6 sends
over TCP (no PRI, `purity.audit[pid]:`, the command layout for every CLI,
GUI and REST command and the `Key: 'value'` layout for session records);
the `Key: 'value'` layout is also what Pure's SIEM partners publish. The
purity.alert text has not been seen from a live array; alert lines are
emitted whole with the severity read from the text when present.
"""

DOCUMENTATION = r"""
---
name: purefa_syslog
short_description: Receive Everpure FlashArray syslog messages as events.
description:
  - Listens on UDP and/or TCP for syslog lines from FlashArrays and emits one
    C(purefa_syslog) event per line. C(purity.audit) lines are parsed into
    fields (user, command, action, result, ...) so rules can match without
    regexes.
  - A FlashArray forwards its controllers' whole system log to a syslog
    target. By default only the C(purity.alert), C(purity.audit) and
    C(purity.test) programs are emitted; the rest is dropped and counted.
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
  programs:
    description:
      - Syslog programs to emit (list or comma-separated string). Lines from
        any other program, and lines with no syslog header, are dropped and
        counted. C(all) or an empty list passes everything through as kind
        C(other).
    type: list
    elements: str
    default: [purity.alert, purity.audit, purity.test]
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
  system_users:
    description:
      - The array's own service accounts (list or comma-separated string);
        their audit lines count as automation, as do command records whose
        auth source is C(system).
    type: list
    elements: str
    default: [pure_datainsights]
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
        programs: "{{ syslog_programs }}"
        arrays: "{{ syslog_hostnames }}"
        automation_users: "{{ automation_users }}"
        allowed_sources: "{{ syslog_allowed_sources }}"
  rules:
    - name: Human action via syslog audit - notify
      condition: >-
        event.purefa_syslog.kind == "audit" and
        event.purefa_syslog.by_automation == false and
        event.purefa_syslog.fields.message_kind != "login"
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
# Severity words as they appear in message text (purity.test starts with
# "INFO", alert texts may say "severity warning"), mapped to the PRI words.
SEVERITY_WORDS = {
    "emergency": "emerg",
    "emerg": "emerg",
    "alert": "alert",
    "critical": "crit",
    "crit": "crit",
    "error": "err",
    "err": "err",
    "warning": "warning",
    "warn": "warning",
    "notice": "notice",
    "info": "info",
    "informational": "info",
    "debug": "debug",
}
# The three programs Purity itself writes; everything else a syslog target
# receives is the controllers' system log.
DEFAULT_PROGRAMS = ["purity.alert", "purity.audit", "purity.test"]
# Local accounts the array's own services use; `purity` itself writes with
# auth source `system` and needs no listing.
DEFAULT_SYSTEM_USERS = ["pure_datainsights"]
# [<PRI>]Mon DD HH:MM:SS host program[pid]: message      (RFC 3164 style; the
# array sends no PRI and appends the pid to the program)
RFC3164 = re.compile(
    r"^(?:<(?P<pri>\d{1,3})>)?(?P<time>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
    r"(?P<prog>[^\s:\[]+)(?:\[\d+\])?:\s*(?P<msg>.*)$"
)
# <PRI>1 2026-10-20T18:00:00Z host program - - - message   (RFC 5424 style)
RFC5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>1\s+(?P<time>\S+)\s+(?P<host>\S+)\s+(?P<prog>\S+)\s+\S+\s+\S+\s+(?:\[.*?\]|-)\s*(?P<msg>.*)$"
)
# The command layout of a purity.audit message:
#   [user (auth)]@array(array-id) command arguments. Message ID: N UTC Time: T Array Name: x
COMMAND = re.compile(
    r"^\[(?P<user>[^\]\(]+?)(?:\s*\((?P<auth>[^)]*)\))?\]@(?P<array>[^(\s]+)\((?P<array_id>[^)]*)\)\s+"
    r"(?P<cmd>.*?)\s*\.?\s*Message ID:\s*(?P<id>\d+)(?P<rest>.*)$"
)
# Key: 'value' pairs; the key starts with a capital after whitespace (so "UTC
# Time: 2026-...Z User: 'u'" yields User, not "Z User"), later words may be
# lower case ("Array name: 'x'") and the colon is optional because the
# published layout has "Location '203.0.113.1'" without one.
FIELD = re.compile(r"(?:^|(?<=\s))([A-Z][A-Za-z]*(?: [A-Za-z]+)*):?\s*'([^']*)'")
RESULT = re.compile(r"\bResult:\s*(\w+)")
MSGID = re.compile(r"\((?P<what>[^)]*?)\s*message ID:\s*(?P<id>\d+)\)")
# Unquoted trailers the array appends to command and test messages
MSGID_TRAILER = re.compile(r"\bMessage ID:\s*(\d+)")
UTC_TRAILER = re.compile(
    r"\bUTC Time:\s*(.+?)(?=\s+(?:Array Name|Message ID|User|Location):|\s*$)"
)
ARRAY_TRAILER = re.compile(r"\bArray [Nn]ame:\s*'?([^'\s]+)'?")
SEVERITY_IN_TEXT = re.compile(
    r"^(?P<lead>[A-Z]{4,13})\b|\bseverity[:=\s]+'?(?P<word>[A-Za-z]+)'?", re.I
)

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
# How often a dropped program is mentioned in the log after its first line
DROP_LOG_EVERY = 1000


def _as_list(value):
    # same helper as purefa_events._as_list, copied rather than imported so
    # this plugin does not pull in pypureclient
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def _programs(value):
    """The programs argument as a list; [] means emit every program.

    None (argument absent) gives the Purity programs; `all` anywhere in the
    list, or an explicit empty list, gives [].
    """
    if value is None:
        return list(DEFAULT_PROGRAMS)
    programs = _as_list(value)
    if any(p.lower() == "all" for p in programs):
        return []
    return programs


def _system_users(value):
    """The system_users argument as a set; None (absent) gives the default."""
    if value is None:
        return set(DEFAULT_SYSTEM_USERS)
    return set(_as_list(value))


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


def _severity_from_text(msg):
    """A severity word from the message text: a leading upper-case level
    word (purity.test's "INFO [pure_middleware] ...") or a "severity <word>"
    phrase; None when the text carries neither."""
    m = SEVERITY_IN_TEXT.search(msg)
    if not m:
        return None
    word = (m.group("lead") or m.group("word") or "").lower()
    return SEVERITY_WORDS.get(word)


def _command_fields(m):
    """Fields of the command layout from its regex match."""
    action = " ".join(m.group("cmd").split())
    words = action.split()
    fields = {
        "user": m.group("user").strip(),
        "array_name": m.group("array"),
        "array_id": m.group("array_id"),
        "action": action,
        "command": words[0] if words else "",
        "subcommand": (
            words[1] if len(words) > 1 and not words[1].startswith("-") else ""
        ),
        "message_id": m.group("id"),
        "message_kind": "command",
    }
    fields["arguments"] = " ".join(words[2:] if fields["subcommand"] else words[1:])
    if m.group("auth") is not None:
        fields["auth_source"] = m.group("auth").strip()
    return fields


def parse(line, arrays, automation, system_users=None):
    if system_users is None:
        system_users = set(DEFAULT_SYSTEM_USERS)
    line = line.strip()
    event = dict.fromkeys(EVENT_KEYS)
    event.update(kind="other", message=line, fields={}, by_automation=False, raw=line)
    m = RFC3164.match(line) or RFC5424.match(line)
    if not m:
        return event
    pri = m.group("pri")
    prog = m.group("prog")
    msg = m.group("msg")
    kind = (prog.split(".", 1)[1] if prog.startswith("purity.") else "") or "other"
    cmd = COMMAND.match(msg)
    if cmd:
        fields = _command_fields(cmd)
        trailer = cmd.group("rest")
    else:
        r = RESULT.search(msg)
        # take "Result: Success" out before pairing keys, or the next key
        # reads as "Success Description"
        body = RESULT.sub(" ", msg) if r else msg
        fields = {
            k.strip().lower().replace(" ", "_"): v for k, v in FIELD.findall(body)
        }
        if r:
            fields["result"] = r.group(1)
        mid = MSGID.search(msg)
        if mid:
            fields["message_id"] = mid.group("id")
            fields["message_kind"] = mid.group("what").strip()
        trailer = msg
    # the unquoted trailers of the command and test layouts
    t = MSGID_TRAILER.search(trailer)
    if t and "message_id" not in fields:
        fields["message_id"] = t.group(1)
    t = UTC_TRAILER.search(trailer)
    if t:
        fields["utc_time"] = t.group(1).strip()
    t = ARRAY_TRAILER.search(trailer)
    if t and "array_name" not in fields:
        fields["array_name"] = t.group(1)
    if kind == "audit":
        fields.setdefault("message_kind", "other")
    host = m.group("host")
    user = fields.get("user")
    event.update(
        kind=kind,
        array=_array_name(fields, host, arrays),
        host=host,
        severity=(
            SEVERITIES[int(pri) % 8] if pri is not None else _severity_from_text(msg)
        ),
        facility=int(pri) // 8 if pri is not None else None,
        time=m.group("time"),
        program=prog,
        message=msg,
        fields=fields,
        user=user,
        action=fields.get("action"),
        result=fields.get("result"),
        by_automation=bool(user and (user in automation or user in system_users))
        or fields.get("auth_source") == "system",
    )
    return event


def _emitter(local, arrays, automation, networks, programs=None, system_users=None):
    """Build the synchronous emit(line, source, protocol) callback: filter the
    sender, parse, keep only the wanted programs, and hand the event to the
    local queue (unbounded, so put_nowait never fails and the UDP callback
    never blocks). `programs` None means the Purity programs; [] means all."""
    wanted = list(DEFAULT_PROGRAMS) if programs is None else list(programs)
    dropped = {}

    def emit(line, source, protocol):
        if not _source_allowed(source, networks):
            logger.debug(
                "purefa_syslog: dropped %s line from %s (allowed_sources)",
                protocol,
                source,
            )
            return False
        event = parse(line, arrays, automation, system_users)
        program = event["program"] or "(no syslog header)"
        if wanted and program not in wanted:
            n = dropped[program] = dropped.get(program, 0) + 1
            if n == 1:
                logger.info(
                    "purefa_syslog: dropping lines of program %s from %s (not in programs); counting from now on",
                    program,
                    source,
                )
            elif n % DROP_LOG_EVERY == 0:
                logger.debug(
                    "purefa_syslog: %d lines of program %s dropped", n, program
                )
            return False
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
    programs = _programs(args.get("programs"))
    arrays = _decode_arrays(args.get("arrays"), "purefa_syslog") or {}
    if not isinstance(arrays, dict):
        raise ValueError(
            "purefa_syslog: arrays must be a mapping of array name or syslog "
            "hostname -> inventory name, not the purefa_events array list"
        )
    automation = set(_as_list(args.get("automation_users")))
    system_users = _system_users(args.get("system_users"))
    networks = _networks(args.get("allowed_sources"))
    loop = asyncio.get_running_loop()

    # One local unbounded queue and one forwarder task: events keep their
    # order, there is one task instead of one per line, and ansible-rulebook's
    # own (one-slot) queue applies back-pressure to the forwarder only.
    local: asyncio.Queue = asyncio.Queue()
    emit = _emitter(local, arrays, automation, networks, programs, system_users)

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
        "purefa_syslog: listening on %s:%s (%s)%s, programs %s",
        host,
        port,
        ",".join(protocols),
        f", sources {[str(n) for n in networks]}" if networks else ", any source",
        programs or "all",
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
