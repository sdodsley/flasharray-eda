"""
purefa_syslog.py - ansible-rulebook event source that listens for FlashArray
syslog messages (push path, sub-second).

Point the array at this host (GUI: Settings > System > Syslog Servers, or
everpure.flasharray.purefa_syslog with a tcp:// or udp:// URI). Purity sends
three programs: purity.alert, purity.audit and purity.test. Each line is
turned into one event; the audit lines are parsed into fields so rules can
match on user, action and result without regexes.

Arguments:
  host        bind address (default 0.0.0.0)
  port        port (default 5140; 514 needs root or a capability)
  protocols   list, udp and/or tcp (default both)
  arrays      optional map of syslog hostname -> array name as used in the
              inventory, for arrays that log a short or different hostname
  automation_users  user names counted as automation (default: empty)

Events:
  purefa_syslog:
    kind           alert | audit | test | other
    array          array name (from the message's "Array name" field, else
                   the syslog hostname, mapped through `arrays`)
    severity       syslog severity word from the PRI (emerg..debug)
    facility       syslog facility number
    time           timestamp text from the header
    message        the text after the program name
    fields         key -> value for every `Key: 'value'` pair in an audit
                   message (User, Interface, Session, Location, Action,
                   Method, Result, Description, ...), lower-cased keys with
                   spaces as underscores
    user           fields.user when present
    action         fields.action when present
    result         fields.result when present
    by_automation  user in automation_users
    source         sender ip
    protocol       udp | tcp
    raw            the whole line

Format verification: the audit parsing follows the message layout published
by Pure's SIEM partners (`<PRI>Mon DD HH:MM:SS host purity.audit: (login
message ID: N) Array name: 'x' Controller: 0 Interface: 'REST' ... User: 'u'
... Action: '...' Method: '' Result: Success Description: ''`). It has not
yet been checked against a live array from this lab (the arrays cannot reach
the EDA host); tests/syslog_send.py replays representative lines.
"""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
# <PRI>Mon DD HH:MM:SS host program: message      (RFC 3164 style)
RFC3164 = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<time>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
    r"(?P<prog>purity\.\w+|\S+?):\s*(?P<msg>.*)$")
# <PRI>1 2026-10-20T18:00:00Z host program - - - message   (RFC 5424 style)
RFC5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>1\s+(?P<time>\S+)\s+(?P<host>\S+)\s+(?P<prog>\S+)\s+\S+\s+\S+\s+(?:\[.*?\]|-)\s*(?P<msg>.*)$")
# Key: 'value' pairs; the key starts at a word boundary (so "UTC Time: 2026-...Z
# User: 'u'" yields User, not "Z User") and the colon is optional because the
# published layout has "Location '10.0.0.1'" without one.
FIELD = re.compile(r"(?:^|(?<=\s))([A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)*):?\s*'([^']*)'")
RESULT = re.compile(r"\bResult:\s*(\w+)")
MSGID = re.compile(r"\((?P<what>[^)]*?)\s*message ID:\s*(?P<id>\d+)\)")


def parse(line, arrays, automation):
    line = line.strip()
    m = RFC3164.match(line) or RFC5424.match(line)
    if not m:
        return {"kind": "other", "array": None, "message": line, "raw": line, "fields": {}}
    pri = int(m.group("pri"))
    prog = m.group("prog")
    msg = m.group("msg")
    kind = prog.split(".", 1)[1] if prog.startswith("purity.") and "." in prog else "other"
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
    array = fields.get("array_name") or arrays.get(host, host)
    user = fields.get("user")
    return {
        "kind": kind,
        "array": array,
        "host": host,
        "severity": SEVERITIES[pri % 8],
        "facility": pri // 8,
        "time": m.group("time"),
        "program": prog,
        "message": msg,
        "fields": fields,
        "user": user,
        "action": fields.get("action"),
        "result": fields.get("result"),
        "by_automation": bool(user and user in automation),
        "raw": line,
    }


class _UDP(asyncio.DatagramProtocol):
    def __init__(self, emit):
        self.emit = emit

    def datagram_received(self, data, addr):
        for line in data.decode("utf-8", "replace").splitlines():
            if line.strip():
                self.emit(line, addr[0], "udp")


async def _tcp_client(reader, writer, emit):
    peer = writer.get_extra_info("peername")
    try:
        while not reader.at_eof():
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace")
            if line.strip():
                emit(line, peer[0] if peer else None, "tcp")
    finally:
        writer.close()


async def main(queue: asyncio.Queue, args: dict) -> None:
    host = args.get("host", "0.0.0.0")
    port = int(args.get("port", 5140))
    protocols = args.get("protocols") or ["udp", "tcp"]
    if isinstance(protocols, str):
        protocols = [p.strip() for p in protocols.split(",")]
    arrays = args.get("arrays") or {}
    automation = set(args.get("automation_users") or [])
    loop = asyncio.get_running_loop()

    def emit(line, source, protocol):
        event = parse(line, arrays, automation)
        event["source"] = source
        event["protocol"] = protocol
        loop.create_task(queue.put({"purefa_syslog": event}))

    servers = []
    if "udp" in protocols:
        transport, _ = await loop.create_datagram_endpoint(lambda: _UDP(emit), local_addr=(host, port))
        servers.append(transport)
    if "tcp" in protocols:
        srv = await asyncio.start_server(lambda r, w: _tcp_client(r, w, emit), host, port)
        servers.append(srv)
    logger.info("purefa_syslog: listening on %s:%s (%s)", host, port, ",".join(protocols))
    try:
        await asyncio.Event().wait()
    finally:
        for s in servers:
            s.close()


if __name__ == "__main__":
    import json
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
