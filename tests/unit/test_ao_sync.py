# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for tools/ao_sync.py against an in-process mock of the automation
orchestrator's API (1.3.1): token, paginated lists, validate, create,
export, update, publish, RFC 9457 problem details. The mock keeps an
in-memory store and records every call, so each test can say what was and
was not written. The shipped definitions are the input; every name here is
an example (no lab host or person)."""

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "ao_sync.py"
DEFINITIONS = sorted((ROOT / "orchestrator").glob("*.json"))
CLIENT_ID = "nx_sa_sync"
CLIENT_SECRET = "example-secret-for-the-mock-only"
CREDENTIAL = "AAP controller"
INTEGRATION = "aap-example"
SERVICE_ACCOUNT = "eda-bridge"
CAPACITY = "capacity_reclaim_approval.json"
DRILL = "dr_drill_schedule.json"
DRILL_NAME = "FlashArray - nightly DR drill"
PLACEHOLDER = "REPLACE_AT_SYNC"


class Problem(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Store:
    """What the mock orchestrator holds, and a record of every call."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = set()
        self.calls = []  # (method, path) in order, the token call included
        self.projects = [
            {"id": "proj-1", "name": "default"},
            {"id": "proj-2", "name": "other"},
        ]
        self.credentials = [{"id": "cred-1", "name": CREDENTIAL}]
        self.integrations = [{"id": "int-1", "name": INTEGRATION}]
        self.service_accounts = [{"id": "sa-1", "name": SERVICE_ACCOUNT}]
        self.workflows = {}  # id -> record with a versions dict
        self.invalid_names = set()  # validate says these are invalid
        self.refuse_create = None  # (status, detail) for POST /workflows
        self.url = None

    def add_workflow(self, name, project_id, definition, published=True):
        wid = f"wf-{len(self.workflows) + 1}"
        self.workflows[wid] = {
            "id": wid,
            "name": name,
            "project_id": project_id,
            "current_version": 1,
            "published_version_number": 1 if published else None,
            "versions": {1: definition},
        }
        return wid

    @staticmethod
    def public(record):
        return {key: value for key, value in record.items() if key != "versions"}

    def count(self, method, pattern):
        return sum(
            1 for m, path in self.calls if m == method and re.fullmatch(pattern, path)
        )

    def writes(self):
        """Every call that would change a workflow."""
        return [
            (m, path)
            for m, path in self.calls
            if m in ("POST", "PATCH", "PUT", "DELETE")
            and path.startswith("/api/v1/workflows")
            and not path.endswith("/validate")
        ]

    def by_name(self, name):
        return [r for r in self.workflows.values() if r["name"] == name]


class Handler(BaseHTTPRequestHandler):
    LISTS = {
        "projects": "projects",
        "credentials": "credentials",
        "integrations": "integrations",
        "service-accounts": "service_accounts",
        "workflows": "workflows",
    }

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        store = self.server.store
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with store.lock:
            store.calls.append((method, parts.path))
            try:
                if parts.path == "/api/v1/auth/token":
                    status, body = self._token(store, method, raw)
                else:
                    auth = self.headers.get("Authorization", "")
                    if not auth.startswith("Bearer ") or auth[7:] not in store.tokens:
                        raise Problem(401, "missing or invalid bearer token")
                    status, body = self._route(store, method, parts.path, query, raw)
            except Problem as exc:
                self._send(
                    exc.status,
                    {
                        "type": "about:blank",
                        "title": "error",
                        "status": exc.status,
                        "detail": exc.detail,
                    },
                    "application/problem+json",
                )
                return
        self._send(status, body)

    def _send(self, status, body, content_type="application/json"):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _json(raw):
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise Problem(400, "the body is not JSON") from None
        if not isinstance(body, dict):
            raise Problem(400, "the body is not an object")
        return body

    @staticmethod
    def _token(store, method, raw):
        if method != "POST":
            raise Problem(405, "method not allowed")
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
        if (
            form.get("grant_type") != "client_credentials"
            or form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
        ):
            raise Problem(401, "invalid client credentials")
        token = f"tok-{len(store.tokens) + 1}"
        store.tokens.add(token)
        return 200, {"access_token": token, "token_type": "Bearer", "expires_in": 900}

    @staticmethod
    def _page(items, query):
        limit = min(int(query.get("limit", ["100"])[0]), 100)
        start = int(query.get("cursor", ["0"])[0])
        end = start + limit
        return 200, {
            "resources": items[start:end],
            "next_cursor": str(end) if end < len(items) else None,
        }

    def _route(self, store, method, path, query, raw):
        segments = path.strip("/").split("/")
        if segments[:2] != ["api", "v1"]:
            raise Problem(404, f"no such path {path}")
        rest = segments[2:]
        if method == "GET" and len(rest) == 1 and rest[0] in self.LISTS:
            items = getattr(store, self.LISTS[rest[0]])
            if rest[0] == "workflows":
                items = [store.public(record) for record in items.values()]
            return self._page(items, query)
        if rest == ["workflows", "validate"] and method == "POST":
            definition = self._json(raw).get("workflow_definition")
            if not isinstance(definition, dict):
                raise Problem(422, "workflow_definition is required")
            if definition.get("name") in store.invalid_names:
                return 200, {
                    "is_valid": False,
                    "error_count": 1,
                    "warning_count": 0,
                    "findings": [
                        {
                            "severity": "error",
                            "node_id": "dr_drill",
                            "message": "unknown parameter 'credentials'",
                        }
                    ],
                }
            return 200, {
                "is_valid": True,
                "error_count": 0,
                "warning_count": 0,
                "findings": [],
            }
        if rest == ["workflows"] and method == "POST":
            if store.refuse_create:
                raise Problem(*store.refuse_create)
            body = self._json(raw)
            for key in ("name", "project_id", "workflow_definition"):
                if key not in body:
                    raise Problem(422, f"{key} is required")
            if body.get("is_import") is not True:
                raise Problem(422, "is_import must be true")
            wid = store.add_workflow(
                body["name"],
                body["project_id"],
                body["workflow_definition"],
                published=False,
            )
            return 201, store.public(store.workflows[wid])
        if len(rest) >= 4 and rest[0] == "workflows" and rest[2] == "versions":
            record = store.workflows.get(rest[1])
            if record is None:
                raise Problem(404, f"no workflow {rest[1]}")
            try:
                version = int(rest[3])
            except ValueError:
                raise Problem(404, f"no version {rest[3]}") from None
            if version not in record["versions"]:
                raise Problem(404, f"no version {version}")
            tail = rest[4:]
            if tail == ["export"] and method == "GET":
                return 200, record["versions"][version]
            if tail == [] and method == "PATCH":
                # the live contract: a version's PATCH changes name and
                # change_description only; a definition here is ignored
                self._json(raw)
                return 200, store.public(record)
            if tail == ["publish"] and method == "POST":
                body = self._json(raw) if raw else {}
                definition = (
                    body.get("workflow_definition") if isinstance(body, dict) else None
                )
                if isinstance(definition, dict):
                    # a publish that carries a definition stores it as the
                    # next version and publishes that one
                    version = max(record["versions"]) + 1
                    record["versions"][version] = definition
                    record["current_version"] = version
                record["published_version_number"] = version
                return 200, store.public(record)
        raise Problem(404, f"no route for {method} {path}")


@pytest.fixture(scope="module")
def ao_sync():
    spec = importlib.util.spec_from_file_location("ao_sync", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store():
    store = Store()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.store = store
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    store.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield store
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AO_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AO_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.chdir(ROOT)


def shown(name):
    """How the tool prints a default-pattern definition."""
    return os.path.join("orchestrator", name)


def run(ao_sync, capsys, store, *args):
    """Run the tool with every lookup name; (exit code, stdout lines, stderr)."""
    argv = [
        "--url",
        store.url,
        "--aap-credential",
        CREDENTIAL,
        "--aap-integration",
        INTEGRATION,
        "--service-account",
        SERVICE_ACCOUNT,
        *args,
    ]
    code = ao_sync.main(argv)
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err


def definition_lines(lines):
    return [line for line in lines if line.startswith("orchestrator")]


def test_token_error_is_reported_and_nothing_written(
    ao_sync, store, env, monkeypatch, capsys
):
    monkeypatch.setenv("AO_CLIENT_SECRET", "wrong")
    code, lines, err = run(ao_sync, capsys, store)
    assert code == 1
    assert lines == []
    assert "HTTP 401 on POST /api/v1/auth/token: invalid client credentials" in err
    assert "nothing was written" in err
    assert "wrong" not in err and CLIENT_SECRET not in err
    assert store.calls == [("POST", "/api/v1/auth/token")]
    assert store.writes() == []


def test_missing_credentials_in_the_environment_is_a_usage_error(
    ao_sync, store, monkeypatch, capsys
):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("AO_CLIENT_ID", raising=False)
    monkeypatch.setenv("AO_CLIENT_SECRET", CLIENT_SECRET)
    with pytest.raises(SystemExit) as exc:
        run(ao_sync, capsys, store)
    assert exc.value.code == 2
    assert "AO_CLIENT_ID and AO_CLIENT_SECRET" in capsys.readouterr().err
    assert store.calls == []


def test_unknown_credential_name_fails_before_any_write(ao_sync, store, env, capsys):
    code, lines, err = run(
        ao_sync, capsys, store, "--aap-credential", "no-such-credential"
    )
    assert code == 1
    assert lines == []
    assert "no credential named 'no-such-credential'; nothing was written" in err
    assert store.writes() == []
    assert store.count("POST", r"/api/v1/workflows/validate") == 0
    assert store.workflows == {}


def test_unknown_project_integration_and_service_account_fail_the_same_way(
    ao_sync, store, env, capsys
):
    for option, value, message in (
        ("--project", "nowhere", "no project named 'nowhere'"),
        ("--aap-integration", "none", "no integration named 'none'"),
        ("--service-account", "nobody", "no service account named 'nobody'"),
    ):
        code, lines, err = run(ao_sync, capsys, store, option, value)
        assert code == 1, option
        assert lines == [], option
        assert message in err, option
    assert store.writes() == []


def test_create_and_publish_on_an_empty_project(ao_sync, store, env, capsys):
    code, lines, err = run(ao_sync, capsys, store)
    assert code == 0, err
    assert lines == [
        f"{shown(path.name)}: created, published v1" for path in DEFINITIONS
    ]
    assert f"ao_sync: {len(DEFINITIONS)} ok, 0 failed, 0 extra" in err
    assert len(store.workflows) == len(DEFINITIONS)
    for record in store.workflows.values():
        assert record["project_id"] == "proj-1"
        assert record["published_version_number"] == 1
        definition = record["versions"][1]
        assert PLACEHOLDER not in json.dumps(definition)
        for node in definition["nodes"]:
            if node["type"] == "aap_job_template":
                assert node["parameters"]["credential_id"] == "cred-1"
                assert node["parameters"]["integration_id"] == "int-1"
            else:
                assert "integration_id" not in node["parameters"]
    (capacity,) = store.by_name("FlashArray - capacity reclaim with approval")
    trigger = capacity["versions"][1]["triggers"][0]
    assert trigger["parameters"]["authorized_service_account_ids"] == ["sa-1"]
    # one validate, one create and one publish per definition, nothing else
    n = len(DEFINITIONS)
    assert store.count("POST", r"/api/v1/workflows/validate") == n
    assert store.count("POST", r"/api/v1/workflows") == n
    assert store.count("POST", r"/api/v1/workflows/wf-\d+/versions/1/publish") == n
    assert store.count("PATCH", r".*") == 0
    assert store.count("DELETE", r".*") == 0


def test_second_run_is_unchanged_with_no_patch_or_publish(ao_sync, store, env, capsys):
    assert run(ao_sync, capsys, store)[0] == 0
    before = len(store.calls)
    code, lines, err = run(ao_sync, capsys, store)
    assert code == 0, err
    assert lines == [
        f"{shown(path.name)}: unchanged, published v1" for path in DEFINITIONS
    ]
    later = store.calls[before:]
    assert [c for c in later if c[0] == "PATCH"] == []
    assert [c for c in later if c[1].endswith("/publish")] == []
    assert [c for c in later if c == ("POST", "/api/v1/workflows")] == []
    # the comparison read every current version
    exports = [c for c in later if c[0] == "GET" and c[1].endswith("/export")]
    assert len(exports) == len(DEFINITIONS)


def test_seeded_equal_workflow_is_unchanged_whatever_the_key_order(
    ao_sync, store, env, capsys
):
    """The orchestrator's export may order keys differently; equality is
    judged after normalisation. A draft equal to ours that was never
    published is published, and only published."""
    doc = json.loads((ROOT / "orchestrator" / DRILL).read_text())
    same = ao_sync.substitute(doc, "cred-1", "int-1", "sa-1")
    reordered = json.loads(json.dumps(same, sort_keys=True))
    assert list(reordered) != list(same)
    wid = store.add_workflow(DRILL_NAME, "proj-1", reordered, published=False)
    code, lines, err = run(ao_sync, capsys, store, shown(DRILL))
    assert code == 0, err
    assert lines == [f"{shown(DRILL)}: unchanged, published v1"]
    assert store.count("PATCH", r".*") == 0
    publish = rf"/api/v1/workflows/{wid}/versions/1/publish"
    assert store.count("POST", publish) == 1
    assert store.workflows[wid]["published_version_number"] == 1
    # now published: the next run neither patches nor publishes
    code, lines, err = run(ao_sync, capsys, store, shown(DRILL))
    assert code == 0, err
    assert lines == [f"{shown(DRILL)}: unchanged, published v1"]
    assert store.count("PATCH", r".*") == 0
    assert store.count("POST", publish) == 1


def test_changed_definition_is_updated_and_republished(
    ao_sync, store, env, capsys, tmp_path
):
    assert run(ao_sync, capsys, store)[0] == 0
    copies = tmp_path / "orchestrator"
    copies.mkdir()
    for path in DEFINITIONS:
        shutil.copy(path, copies / path.name)
    changed = copies / DRILL
    doc = json.loads(changed.read_text())
    doc["description"] = "Every night at 03:00 instead."
    changed.write_text(json.dumps(doc, indent=2) + "\n")
    before = len(store.calls)
    code, lines, err = run(ao_sync, capsys, store, str(copies / "*.json"))
    assert code == 0, err
    expected = {
        str(copies / path.name): "unchanged, published v1" for path in DEFINITIONS
    }
    expected[str(changed)] = "updated, published v2"
    assert lines == [f"{path}: {text}" for path, text in expected.items()]
    later = store.calls[before:]
    (record,) = store.by_name(DRILL_NAME)
    wid = record["id"]
    # no PATCH: a changed definition travels in the publish call and lands as v2
    assert [c for c in later if c[0] == "PATCH"] == []
    assert [c for c in later if c[1].endswith("/publish")] == [
        ("POST", f"/api/v1/workflows/{wid}/versions/1/publish")
    ]
    assert record["current_version"] == 2 and record["published_version_number"] == 2
    assert record["versions"][2]["description"] == "Every night at 03:00 instead."
    assert record["versions"][1]["description"] != "Every night at 03:00 instead."
    assert record["versions"][2]["nodes"][0]["parameters"]["credential_id"] == "cred-1"
    assert len(store.workflows) == len(DEFINITIONS)


def test_validation_failure_refuses_the_import(ao_sync, store, env, capsys):
    store.invalid_names.add(DRILL_NAME)
    code, lines, err = run(ao_sync, capsys, store)
    assert code == 1
    index = lines.index(
        f"{shown(DRILL)}: error: the orchestrator refuses the definition: "
        "1 error(s), 0 warning(s)"
    )
    assert lines[index + 1] == "    error: dr_drill: unknown parameter 'credentials'"
    assert store.by_name(DRILL_NAME) == []
    assert len(store.workflows) == len(DEFINITIONS) - 1
    created = [line for line in lines if line.endswith("created, published v1")]
    assert len(created) == len(DEFINITIONS) - 1
    assert f"ao_sync: {len(DEFINITIONS) - 1} ok, 1 failed, 0 extra" in err


def test_dry_run_writes_nothing(ao_sync, store, env, capsys, tmp_path):
    code, lines, err = run(ao_sync, capsys, store, "--dry-run")
    assert code == 0, err
    assert lines == [
        f"{shown(path.name)}: would create and publish" for path in DEFINITIONS
    ]
    assert store.writes() == []
    assert store.workflows == {}
    assert store.count("POST", r"/api/v1/workflows/validate") == len(DEFINITIONS)
    assert "(dry run, nothing written)" in err

    assert run(ao_sync, capsys, store)[0] == 0
    changed = tmp_path / DRILL
    doc = json.loads((ROOT / "orchestrator" / DRILL).read_text())
    doc["description"] = "changed"
    changed.write_text(json.dumps(doc))
    writes = len(store.writes())
    code, lines, err = run(ao_sync, capsys, store, "--dry-run", str(changed))
    assert code == 0, err
    assert lines[0] == f"{changed}: would update and publish v2"
    assert len(store.writes()) == writes
    (record,) = store.by_name(DRILL_NAME)
    assert record["versions"][1]["description"] != "changed"


def test_extra_workflows_are_reported_not_removed(ao_sync, store, env, capsys):
    store.add_workflow("Legacy cleanup", "proj-1", {"name": "Legacy cleanup"})
    store.add_workflow("Elsewhere", "proj-2", {"name": "Elsewhere"})
    code, lines, err = run(ao_sync, capsys, store)
    assert code == 0, err
    extras = [line for line in lines if line.startswith("extra: ")]
    assert extras == [
        "extra: Legacy cleanup (in the project, no definition file; left alone)"
    ]
    assert lines.index(extras[0]) > len(DEFINITIONS) - 1
    assert store.by_name("Legacy cleanup") and store.by_name("Elsewhere")
    assert store.count("DELETE", r".*") == 0
    assert "1 extra" in err


def test_a_leftover_placeholder_fails_its_definition_only(
    ao_sync, store, env, capsys, tmp_path
):
    """An approval node's credential_id is not one of the two substitutions,
    so a placeholder there stays and the definition is refused before it
    reaches the orchestrator; the other definition is still imported."""
    doc = json.loads((ROOT / "orchestrator" / CAPACITY).read_text())
    gate = next(n for n in doc["nodes"] if n["type"] == "approval")
    gate["parameters"]["credential_id"] = PLACEHOLDER
    bad = tmp_path / "capacity.json"
    bad.write_text(json.dumps(doc))
    code, lines, err = run(ao_sync, capsys, store, str(bad), shown(DRILL))
    assert code == 1
    assert lines == [
        f"{bad}: error: {PLACEHOLDER} left in "
        f"nodes[{gate['id']}].parameters.credential_id",
        f"{shown(DRILL)}: created, published v1",
    ]
    assert store.by_name(doc["name"]) == []
    assert store.count("POST", r"/api/v1/workflows/validate") == 1


def test_a_placeholder_without_its_option_is_refused(ao_sync, store, env, capsys):
    """The capacity definition needs all three names; given only the
    credential, it is refused and nothing of it reaches the orchestrator,
    while the drill (credential and integration only) is still imported."""
    code = ao_sync.main(
        [
            "--url",
            store.url,
            "--aap-credential",
            CREDENTIAL,
            "--aap-integration",
            INTEGRATION,
            shown(CAPACITY),
            shown(DRILL),
        ]
    )
    out = capsys.readouterr().out.splitlines()
    assert code == 1
    assert out == [
        f"{shown(CAPACITY)}: error: {PLACEHOLDER} needs --service-account",
        f"{shown(DRILL)}: created, published v1",
    ]
    code = ao_sync.main(
        ["--url", store.url, "--aap-credential", CREDENTIAL, shown(CAPACITY)]
    )
    out = capsys.readouterr().out.splitlines()
    assert code == 1
    # the drill, imported above, is an extra of this narrower run
    assert out == [
        f"{shown(CAPACITY)}: error: {PLACEHOLDER} needs "
        "--aap-integration and --service-account",
        f"extra: {DRILL_NAME} (in the project, no definition file; left alone)",
    ]
    assert store.by_name("FlashArray - capacity reclaim with approval") == []
    assert store.count("POST", r"/api/v1/workflows/validate") == 1


def test_only_narrows_what_is_written(ao_sync, store, env, capsys):
    code, lines, err = run(ao_sync, capsys, store, "--only", DRILL)
    assert code == 0, err
    assert lines == [f"{shown(DRILL)}: created, published v1"]
    assert len(store.workflows) == 1
    # the other definitions are not written, and nothing is an extra
    assert store.count("POST", r"/api/v1/workflows/validate") == 1
    with pytest.raises(SystemExit) as exc:
        run(ao_sync, capsys, store, "--only", "no_such_file.json")
    assert exc.value.code == 2


def test_api_error_detail_is_printed(ao_sync, store, env, capsys):
    store.refuse_create = (422, "the project's workflow quota is used up")
    code, lines, err = run(ao_sync, capsys, store, shown(DRILL))
    assert code == 1
    assert lines == [
        f"{shown(DRILL)}: error: HTTP 422 on POST /api/v1/workflows: "
        "the project's workflow quota is used up"
    ]
    assert store.count("POST", r".*/publish") == 0


def test_two_workflows_of_one_name_are_ambiguous(ao_sync, store, env, capsys):
    store.add_workflow(DRILL_NAME, "proj-1", {"name": DRILL_NAME})
    store.add_workflow(DRILL_NAME, "proj-1", {"name": DRILL_NAME})
    code, lines, err = run(ao_sync, capsys, store, shown(DRILL))
    assert code == 1
    assert lines == [
        f"{shown(DRILL)}: error: 2 workflows named {DRILL_NAME!r} in the project; "
        "which one to update is ambiguous"
    ]
    assert store.writes() == []


def test_lists_are_paginated(ao_sync, store, env, capsys):
    """The credential is the 150th of 150, so the lookup needs two pages."""
    store.credentials = [
        {"id": f"cred-{i}", "name": f"credential {i}"} for i in range(149)
    ]
    store.credentials.append({"id": "cred-last", "name": CREDENTIAL})
    code, lines, err = run(ao_sync, capsys, store, shown(DRILL))
    assert code == 0, err
    assert store.count("GET", r"/api/v1/credentials") == 2
    (record,) = store.by_name(DRILL_NAME)
    for node in record["versions"][1]["nodes"]:
        if node["type"] == "aap_job_template":
            assert node["parameters"]["credential_id"] == "cred-last"


def test_substitute_touches_only_the_placeholders(ao_sync):
    doc = {
        "name": "t",
        "triggers": [
            {
                "id": "eda",
                "type": "eda_trigger",
                "parameters": {
                    "webhook_path": "p",
                    "authorized_service_account_ids": [PLACEHOLDER, "sa-other"],
                },
            }
        ],
        "nodes": [
            {
                "id": "job",
                "type": "aap_job_template",
                "parameters": {"credential_id": PLACEHOLDER, "extra_vars": {}},
            },
            {
                "id": "wf",
                "type": "aap_workflow_job_template",
                "parameters": {"credential_id": "fixed-id"},
            },
            {
                "id": "gate",
                "type": "approval",
                "parameters": {"credential_id": PLACEHOLDER},
            },
        ],
    }
    assert ao_sync.placeholder_needs(doc) == {
        "--aap-credential",
        "--aap-integration",
        "--service-account",
    }
    out = ao_sync.substitute(doc, "c", "i", "s")
    assert doc["nodes"][0]["parameters"]["credential_id"] == PLACEHOLDER  # a copy
    assert out["nodes"][0]["parameters"] == {
        "credential_id": "c",
        "integration_id": "i",
        "extra_vars": {},
    }
    assert out["nodes"][1]["parameters"] == {"credential_id": "fixed-id"}
    assert out["triggers"][0]["parameters"]["authorized_service_account_ids"] == [
        "s",
        "sa-other",
    ]
    assert ao_sync.leftover_placeholders(out) == [
        "nodes[gate].parameters.credential_id"
    ]
    # nothing given: nothing replaced
    assert ao_sync.substitute(doc) == doc
    assert ao_sync.normalise({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def _cli(*args, env):
    return subprocess.run(
        [sys.executable, "-I", "tools/ao_sync.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_cli_exit_codes(store):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("AO_")}
    result = _cli("--url", store.url, env=clean)
    assert result.returncode == 2
    assert "AO_CLIENT_ID and AO_CLIENT_SECRET" in result.stderr
    assert store.calls == []

    env = dict(clean, AO_CLIENT_ID=CLIENT_ID, AO_CLIENT_SECRET=CLIENT_SECRET)
    args = (
        "--url",
        store.url,
        "--aap-credential",
        CREDENTIAL,
        "--aap-integration",
        INTEGRATION,
        "--service-account",
        SERVICE_ACCOUNT,
    )
    result = _cli(*args, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"{shown(path.name)}: created, published v1" for path in DEFINITIONS
    ]
    assert CLIENT_SECRET not in result.stdout + result.stderr
    result = _cli(*args, "--aap-credential", "missing", env=env)
    assert result.returncode == 1
    assert "no credential named 'missing'" in result.stderr
    result = _cli(*args, "orchestrator/does_not_exist_*.json", env=env)
    assert result.returncode == 2


def test_tool_is_standard_library_only():
    text = TOOL.read_text()
    imported = set(re.findall(r"^(?:import|from)\s+([a-z_]+)", text, re.MULTILINE))
    assert imported <= {
        "argparse",
        "glob",
        "json",
        "os",
        "pathlib",
        "ssl",
        "sys",
        "urllib",
    }
    assert "SPDX-License-Identifier: Apache-2.0" in text
    # the secret is read from the environment only
    assert "AO_CLIENT_SECRET" in text
    assert "--client-secret" not in text
