#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Mock of the two automation orchestrator endpoints the EDA bridge uses.

Standard library only. It stands in for Red Hat's automation orchestrator
(upstream: Syntara) so playbooks/orchestrator_post.yml can be exercised
without an instance:

  POST /api/v1/auth/token            form fields grant_type=client_credentials,
                                     client_id, client_secret -> 200
                                     {access_token, token_type, expires_in}
                                     or 401
  POST /api/v1/webhooks/eda/<path>   Authorization: Bearer <token> (401 when
                                     missing, unknown or expired); <path> must
                                     be one of --paths (403); body over 1 MiB
                                     -> 413; invalid JSON -> 400; otherwise
                                     the body is appended to --record as one
                                     JSON line and the answer is 202
                                     {execution_id}
  GET  /healthz                      200 {status: ok}

Run it as a script from the repository root (tests/ is not a package, and
`python -m tests.mock_orchestrator` can be shadowed by a `tests` package in
site-packages):

  python tests/mock_orchestrator.py --port 8800 --client-id nx_sa_demo \\
      --client-secret demo --paths fa_capacity --record tests/out/orchestrator.jsonl

or import make_server() from a test and serve it in a thread. The secret
is compared in constant time; tokens are random and expire after
--token-ttl seconds (default 900, the orchestrator's default lifetime).
"""

import argparse
import hmac
import json
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

MAX_BODY_BYTES = 1024 * 1024
DEFAULT_TOKEN_TTL = 900
TOKEN_PATH = "/api/v1/auth/token"
EDA_PREFIX = "/api/v1/webhooks/eda/"
# Never read more than this from a client, whatever Content-Length claims.
READ_CAP_BYTES = 8 * MAX_BODY_BYTES


class MockOrchestrator(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        client_id,
        client_secret,
        paths,
        record,
        token_ttl=DEFAULT_TOKEN_TTL,
        quiet=True,
    ):
        super().__init__(address, Handler)
        self.client_id = client_id
        self.client_secret = client_secret
        self.paths = set(paths)
        self.record = record
        self.token_ttl = token_ttl
        self.quiet = quiet
        self.lock = threading.Lock()
        self.tokens = {}  # token -> expiry (monotonic seconds)
        self.issued_tokens = []  # every token ever issued, for tests
        self.webhook_calls = []  # (path, status) for every POST under EDA_PREFIX
        self.execution_ids = []
        self.executions = (
            []
        )  # the executions the posted events started, as the API lists them

    @property
    def url(self):
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def check_client(self, client_id, client_secret):
        return hmac.compare_digest(
            client_id or "", self.client_id
        ) and hmac.compare_digest(client_secret or "", self.client_secret)

    def issue_token(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = time.monotonic() + self.token_ttl
            self.issued_tokens.append(token)
        return token

    def token_valid(self, token):
        with self.lock:
            expiry = self.tokens.get(token)
            if expiry is None:
                return False
            if expiry < time.monotonic():
                del self.tokens[token]
                return False
            return True

    def record_event(self, path, body):
        execution_id = str(uuid.uuid4())
        with self.lock:
            self.execution_ids.append(execution_id)
            self.executions.append(
                {
                    "id": execution_id,
                    "status": "paused",
                    "approval_pending": True,
                    "workflow_id": "mock-" + path,
                    "input_data": body,
                }
            )
            if self.record:
                with open(self.record, "a", encoding="ascii") as handle:
                    handle.write(json.dumps(body) + "\n")
        return execution_id

    def note_call(self, path, status):
        with self.lock:
            self.webhook_calls.append((path, status))


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-orchestrator/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.server.quiet:
            super().log_message(fmt, *args)

    def _send(self, status, payload):
        body = json.dumps(payload).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _problem(self, status, title, detail):
        # RFC 9457 problem details, the orchestrator's error shape
        self._send(status, {"title": title, "status": status, "detail": detail})

    def _read_body(self):
        """Return (body_bytes, declared_length). Reads at most READ_CAP_BYTES
        so a lying client cannot make the mock buffer without bound."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b"", 0
        data = self.rfile.read(min(length, READ_CAP_BYTES))
        return data, length

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        elif self.path.startswith("/api/v1/executions"):
            self._executions()
        else:
            self._problem(404, "Not Found", f"no route for GET {self.path}")

    def _executions(self):
        """The executions list the bridge reads before posting: bearer token
        required, ?status= filters, {"resources": [...]} like the product."""
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not self.server.token_valid(token.strip()):
            return self._problem(401, "Unauthorized", "missing or invalid bearer token")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        wanted = query.get("status", [None])[0]
        limit = int(query.get("limit", ["100"])[0])
        offset = int(query.get("cursor", ["0"])[0] or 0)
        with self.server.lock:
            rows = [
                dict(e) for e in self.server.executions if wanted in (None, e["status"])
            ]
        page = rows[offset : offset + limit]
        body = {"resources": page, "total": len(rows)}
        if offset + limit < len(rows):
            body["next_cursor"] = str(offset + limit)
        self._send(200, body)

    def do_POST(self):
        if self.path == TOKEN_PATH:
            self._token()
        elif self.path.startswith(EDA_PREFIX):
            self._event(self.path[len(EDA_PREFIX) :])
        else:
            self._read_body()
            self._problem(404, "Not Found", f"no route for POST {self.path}")

    def _token(self):
        data, _ = self._read_body()
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            self._problem(400, "Bad Request", "token requests are form-encoded")
            return
        form = parse_qs(data.decode("ascii", "replace"))
        grant_type = (form.get("grant_type") or [""])[0]
        if grant_type != "client_credentials":
            self._problem(400, "Bad Request", "grant_type must be client_credentials")
            return
        client_id = (form.get("client_id") or [""])[0]
        client_secret = (form.get("client_secret") or [""])[0]
        if not self.server.check_client(client_id, client_secret):
            self._problem(401, "Unauthorized", "invalid client credentials")
            return
        self._send(
            200,
            {
                "access_token": self.server.issue_token(),
                "token_type": "bearer",
                "expires_in": self.server.token_ttl,
            },
        )

    def _event(self, path):
        data, declared = self._read_body()
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        # decide first, note the call, then answer: a test that reads the
        # call list as soon as it has the answer must find the call there
        if scheme.lower() != "bearer" or not self.server.token_valid(token.strip()):
            status, answer = 401, ("Unauthorized", "missing or invalid bearer token")
        elif path not in self.server.paths:
            status, answer = 403, (
                "Forbidden",
                "service account is not authorized for this trigger",
            )
        elif declared > MAX_BODY_BYTES or len(data) > MAX_BODY_BYTES:
            status, answer = 413, (
                "Payload Too Large",
                f"payload of {declared} bytes exceeds {MAX_BODY_BYTES} bytes",
            )
        else:
            try:
                body = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                status, answer = 400, ("Bad Request", "body is not valid JSON")
            else:
                status = 202
                answer = {"execution_id": self.server.record_event(path, body)}
        self.server.note_call(path, status)
        if status == 202:
            self._send(status, answer)
        else:
            self._problem(status, *answer)


def make_server(
    host,
    port,
    client_id,
    client_secret,
    paths,
    record=None,
    token_ttl=DEFAULT_TOKEN_TTL,
    quiet=True,
):
    """Bind the mock; port 0 picks a free one (read server.server_address)."""
    return MockOrchestrator(
        (host, port), client_id, client_secret, paths, record, token_ttl, quiet
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument(
        "--paths",
        required=True,
        help="comma-separated webhook paths the service account is bound to",
    )
    parser.add_argument(
        "--record", help="file that receives one JSON line per accepted event"
    )
    parser.add_argument("--token-ttl", type=int, default=DEFAULT_TOKEN_TTL)
    parser.add_argument("--quiet", action="store_true", help="no request log on stderr")
    args = parser.parse_args(argv)
    paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    server = make_server(
        args.host,
        args.port,
        args.client_id,
        args.client_secret,
        paths,
        args.record,
        args.token_ttl,
        args.quiet,
    )
    print(
        f"mock orchestrator on {server.url} paths={sorted(paths)} record={args.record}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
