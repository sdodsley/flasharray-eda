#!/usr/bin/env python3
"""Send representative FlashArray syslog lines to the purefa_syslog listener.

    python3 tests/syslog_send.py <host> <port> [udp|tcp] [array-name]

The lines follow the layout Pure's SIEM partners publish for purity.audit
(login/operation records as `Key: 'value'` pairs) plus purity.alert and
purity.test examples. They are representative, not captured from an array.
"""
import socket
import sys
import time

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5140
proto = sys.argv[3] if len(sys.argv) > 3 else "udp"
array = sys.argv[4] if len(sys.argv) > 4 else "array-a"
stamp = time.strftime("%b %d %H:%M:%S")
utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
lines = [
    f"<182>{stamp} {array} purity.audit: (login message ID: 676886) Array name: '{array}' Controller: 0 Interface: 'REST' "
    f"Module: '' Session: 'abc123' UTC Time: {utc} User: 'eda-human' Location '10.0.0.9' Sublocation: 'python-requests/2.32' "
    f"Action: 'login' Method: '' Result: Success Description: ''",
    f"<182>{stamp} {array} purity.audit: (operation message ID: 676887) Array name: '{array}' Controller: 0 Interface: 'CLI' "
    f"Module: 'purevol' Session: 'abc124' UTC Time: {utc} User: 'eda-human' Location '10.0.0.9' Sublocation: 'ssh' "
    f"Action: 'purevol destroy prod-vol' Method: '' Result: Success Description: 'volume destroyed'",
    f"<180>{stamp} {array} purity.alert: (hardware:ct0.eth7): failure [code 42, severity warning]",
    f"<182>{stamp} {array} purity.test: This is a test message from {array}",
]
if proto == "tcp":
    s = socket.create_connection((host, port))
    for line in lines:
        s.sendall((line + "\n").encode())
    s.close()
else:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for line in lines:
        s.sendto(line.encode(), (host, port))
    s.close()
print(f"sent {len(lines)} lines to {host}:{port}/{proto}")
