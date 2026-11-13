"""tools/activation_audit.py renders the extra vars the seed would, compares
them with every FlashArray activation, flags placeholders and disabled
activations, and recreates only what drifted. Every name here is an example
(no lab host or person)."""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "activation_audit.py"


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location("activation_audit", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXAMPLE = {
    "purefa_arrays": [{"name": "array-a"}],
    "config_arrays": [{"name": "array-a"}],
    "alerts_interval": 30,
    "capacity_risk_pct": 85,
    "orchestrator_enabled": False,
    "orchestrator_url": "https://orchestrator.example.com",
    "baseline_dns_domain": "example.com",
}
SITE = {
    "purefa_arrays": [{"name": "a"}, {"name": "b"}],
    "alerts_interval": 60,
    "orchestrator_enabled": True,
    "orchestrator_url": "https://ao.site:18000",
    "baseline_dns_domain": "site.example",
}


def test_expected_vars_follow_the_seed_recipe(audit):
    got = audit.expected_extra_vars(
        EXAMPLE, SITE, "FlashArray", orchestrator_attached=False
    )
    assert "purefa_arrays" not in got and "config_arrays" not in got
    assert got["alerts_interval"] == 60  # the site file wins
    assert got["capacity_risk_pct"] == 85  # a key the site lacks keeps its default
    assert got["orchestrator_url"] == "https://ao.site:18000"
    assert got["aap_organization"] == "FlashArray"
    attached = audit.expected_extra_vars(
        EXAMPLE, SITE, "FlashArray", orchestrator_attached=True
    )
    assert "orchestrator_url" not in attached  # the credential injects it


def test_placeholders_count_only_where_they_would_act(audit):
    live = dict(EXAMPLE, orchestrator_enabled=True)
    found = dict(audit.placeholders(live))
    assert "orchestrator_url" in found and "baseline_dns_domain" in found
    # the orchestrator URL is harmless while the route is off
    assert "orchestrator_url" not in dict(audit.placeholders(EXAMPLE))
    # a site that means example.com says so
    assert "baseline_dns_domain" not in dict(
        audit.placeholders(live, allow=["baseline_dns_domain"])
    )
    assert audit.placeholders({"dns": "192.0.2.53,192.0.2.54"}) == [
        ("dns", "192.0.2.53,192.0.2.54")
    ]
    assert audit.placeholders({"url": "https://ao.site:18000", "n": 5}) == []


def test_audit_names_every_difference_and_drift_is_vars_only(audit):
    expected = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    live = dict(expected)
    live["alerts_interval"] = 30
    live["orchestrator_url"] = "https://orchestrator.example.com"
    del live["capacity_risk_pct"]
    live["stale_key"] = 1
    detail = {
        "extra_var": yaml.safe_dump(live),
        "is_enabled": False,
        "status": "stopped",
    }
    problems = audit.audit_activation(detail, expected)
    joined = "\n".join(problems)
    assert "missing capacity_risk_pct" in joined
    assert "extra stale_key" in joined
    assert "alerts_interval is 30, expected 60" in joined
    assert "orchestrator_url is" in joined and "placeholder orchestrator_url" in joined
    assert "disabled" in problems and "status stopped" in problems
    assert audit.has_drift(detail, expected) is True
    # disabled or stopped alone is reported but is not drift, so --recreate leaves it
    quiet = {
        "extra_var": yaml.safe_dump(expected),
        "is_enabled": False,
        "status": "stopped",
    }
    assert audit.audit_activation(quiet, expected) == ["disabled", "status stopped"]
    assert audit.has_drift(quiet, expected) is False
    clean = {
        "extra_var": yaml.safe_dump(expected),
        "is_enabled": True,
        "status": "running",
    }
    assert audit.audit_activation(clean, expected) == []


# --- a tiny EDA controller -------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_truncated(self, code, head=b'{"id": 10'):
        # the connection drops after a few body bytes
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "500")
        self.end_headers()
        self.wfile.write(head)
        self.close_connection = True

    def _send_raw(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        store = self.server.store
        store.calls.append(("GET", self.path))
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send(401, {"detail": "no"})
        if self.path.startswith("/api/eda/v1/activations/?"):
            if store.list_503:
                store.list_503 -= 1
                return self._send(503, {"detail": "gateway"})
            return self._send(
                200,
                {
                    "results": [
                        {"id": a["id"], "name": a["name"]} for a in store.activations
                    ],
                    "next": None,
                },
            )
        for a in store.activations:
            if self.path == f"/api/eda/v1/activations/{a['id']}/":
                if store.detail_503:
                    store.detail_503 -= 1
                    return self._send(503, {"detail": "gateway"})
                if a["id"] in store.detail_empty_ids:
                    store.detail_empty_ids.discard(a["id"])
                    return self._send_empty(200)
                return self._send(200, a)
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        store = self.server.store
        store.calls.append(("POST", self.path))
        body = self._body()
        if self.path == "/api/eda/v1/activations/":
            if store.refuse_vars:
                return self._send(
                    400,
                    {
                        "extra_var": [
                            "Key: orchestrator_url already exists in extra vars."
                        ]
                    },
                )
            if store.truncate_400:
                store.truncate_400 -= 1
                return self._send_truncated(400, b'{"name": ["activation')
            if store.answer_503:
                store.answer_503 -= 1
                return self._send(503, {"detail": "gateway"})
            if store.answer_empty:
                store.answer_empty -= 1
                return self._send_empty(201)
            taken = any(a["name"] == body.get("name") for a in store.activations)
            if store.name_held or taken:
                store.name_held = max(0, store.name_held - 1)
                return self._send(
                    400, {"name": ["activation with this name already exists."]}
                )
            store.next_id += 1
            created = dict(body, id=store.next_id, status="running")
            # the controller hands extra_var back re-serialised, keys sorted
            created["extra_var"] = yaml.safe_dump(yaml.safe_load(body["extra_var"]))
            created["eda_credentials"] = [
                {"id": i, "name": f"cred-{i}"} for i in body.get("eda_credentials", [])
            ]
            for key in ("project", "rulebook", "decision_environment", "organization"):
                created[key] = {"id": body.get(key + "_id")}
            store.activations.append(created)
            store.created.append(body)
            if store.ghost_create:
                # the controller carried the create out; the gateway lost the answer
                store.ghost_create -= 1
                return self._send(503, {"detail": "gateway"})
            if store.ghost_empty:
                store.ghost_empty -= 1
                return self._send_empty(201)
            if store.ghost_truncate:
                store.ghost_truncate -= 1
                return self._send_truncated(201)
            if store.ghost_html:
                store.ghost_html -= 1
                return self._send_raw(
                    201, b"<html>Service restarting</html>", "text/html"
                )
            if store.detail_503_after_create:
                store.detail_503 = store.detail_503_after_create
                store.detail_503_after_create = 0
            return self._send(201, created)
        if self.path.endswith("/disable/"):
            return self._send(204, {})
        self._send(404, {"detail": "not found"})

    def do_DELETE(self):
        store = self.server.store
        store.calls.append(("DELETE", self.path))
        before = len(store.activations)
        store.activations = [
            a
            for a in store.activations
            if self.path != f"/api/eda/v1/activations/{a['id']}/"
        ]
        if store.list_503_after_delete and len(store.activations) < before:
            store.list_503 = store.list_503_after_delete
            store.list_503_after_delete = 0
        if store.delete_503 and len(store.activations) < before:
            # carried out, but the gateway lost the answer
            store.delete_503 -= 1
            return self._send(503, {"detail": "gateway"})
        self._send(204 if len(store.activations) < before else 404, {})


class Store:
    def __init__(self):
        self.calls = []
        self.activations = []
        self.created = []
        self.next_id = 100
        self.name_held = 0  # creates refused with 'already exists' before one succeeds
        self.ghost_create = 0  # creates carried out but answered 503
        self.answer_503 = 0  # creates answered 503 and not carried out
        self.answer_empty = 0  # creates answered 201 with no body, not carried out
        self.ghost_empty = 0  # creates carried out but answered 201 with no body
        self.ghost_truncate = 0  # creates carried out but the answer cut short
        self.refuse_vars = False  # every create refused 400 over the extra vars
        self.delete_503 = 0  # deletes carried out but answered 503
        self.truncate_400 = 0  # creates refused 400 with the body cut short
        self.ghost_html = 0  # creates carried out but answered with an HTML page
        self.list_503 = 0  # activations lists answered 503
        self.list_503_after_delete = 0  # ... armed by the next delete
        self.detail_503 = 0  # activation details answered 503
        self.detail_503_after_create = 0  # ... armed by the next create
        self.detail_empty_ids = set()  # details answered 200 with no body, once
        self.url = None


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


def _activation(aid, name, variables, enabled=True, status="running", credentials=()):
    return {
        "id": aid,
        "name": name,
        "description": "d",
        "extra_var": yaml.safe_dump(variables),
        "is_enabled": enabled,
        "status": status,
        "restart_policy": "on-failure",
        "log_level": "error",
        "eda_credentials": [{"id": i, "name": n} for i, n in credentials],
        "project": {"id": 1},
        "rulebook": {"id": 2},
        "decision_environment": {"id": 3},
        "organization": {"id": 4},
    }


@pytest.fixture
def files(tmp_path):
    example = tmp_path / "example.yml"
    site = tmp_path / "site.yml"
    example.write_text(yaml.safe_dump(EXAMPLE))
    site.write_text(yaml.safe_dump(SITE))
    return ["--example", str(example), "--vars", str(site)]


def test_cli_reports_drift_and_exits_one(audit, store, files, capsys, monkeypatch):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    stale = dict(good, orchestrator_url="https://orchestrator.example.com")
    store.activations = [
        _activation(7, "FlashArray rulebook - alerts", good),
        _activation(8, "FlashArray rulebook - capacity", stale),
        _activation(9, "Something else", {}),
    ]
    monkeypatch.setenv("AAP_TOKEN", "tok")
    code = audit.main(["--url", store.url, *files])
    out = capsys.readouterr()
    assert code == 1
    lines = out.out.splitlines()
    assert lines[0] == "FlashArray rulebook - alerts: ok (running)"
    assert lines[1].startswith("FlashArray rulebook - capacity (id 8): ")
    assert "placeholder orchestrator_url" in lines[1]
    assert "Something else" not in out.out
    assert "2 activation(s), 1 wrong" in out.err
    assert "tok" not in out.out + out.err
    # a clean fleet exits 0 and the JSON form carries the same report
    store.activations[1] = _activation(8, "FlashArray rulebook - capacity", good)
    code = audit.main(["--url", store.url, *files, "--json"])
    out = capsys.readouterr()
    assert code == 0
    assert [e["problems"] for e in json.loads(out.out)] == [[], []]


def test_recreate_touches_only_the_drifted_and_keeps_the_enabled_flag(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    stale = dict(good, alerts_interval=30)
    store.activations = [
        _activation(7, "FlashArray rulebook - alerts", good),
        _activation(
            8,
            "FlashArray rulebook - capacity",
            stale,
            enabled=False,
            credentials=[(5, "fleet")],
        ),
        _activation(9, "FlashArray rulebook - health", good, status="stopped"),
    ]
    store.name_held = 1  # the controller still holds the name right after the delete
    monkeypatch.setenv("AAP_TOKEN", "tok")
    monkeypatch.setattr(audit.time, "sleep", lambda s: None)
    code = audit.main(["--url", store.url, *files, "--recreate"])
    out = capsys.readouterr()
    # only the capacity activation drifted; the stopped one is reported, not recreated
    deletes = [c for c in store.calls if c[0] == "DELETE"]
    assert deletes == [("DELETE", "/api/eda/v1/activations/8/")]
    assert len(store.created) == 1
    created = store.created[0]
    assert created["name"] == "FlashArray rulebook - capacity"
    assert created["is_enabled"] is False  # the site's choice survives
    assert created["eda_credentials"] == [5]
    assert created["project_id"] == 1 and created["rulebook_id"] == 2
    assert yaml.safe_load(created["extra_var"]) == good
    assert "deleting FlashArray rulebook - capacity (id 8)" in out.out
    assert "created FlashArray rulebook - capacity as id 101" in out.out
    assert "still holds the name FlashArray rulebook - capacity" in out.out
    # the stopped health activation still counts as wrong
    assert code == 1
    assert "3 activation(s), 1 wrong" in out.err


def _run_recreate(audit, store, files, monkeypatch):
    # a clock that only sleeps move, so every deadline in the tool is reached
    # in one pass and a loop that should have stopped fails fast
    clock = {"now": 1000.0}
    monkeypatch.setenv("AAP_TOKEN", "tok")
    monkeypatch.setattr(
        audit.time, "sleep", lambda s: clock.update(now=clock["now"] + s)
    )
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock["now"])
    return audit.main(["--url", store.url, *files, "--recreate"])


def _posts(store):
    return [c for c in store.calls if c == ("POST", "/api/eda/v1/activations/")]


def test_recreate_keeps_a_create_that_went_through_behind_a_gateway_error(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(7, "FlashArray rulebook - alerts", good),
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.ghost_create = 1  # the controller creates it, the gateway answers 503
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    # one create reached the controller; the retry's "already exists" was it,
    # recognised although the controller hands the vars back in another key order
    assert len(store.created) == 1
    names = [a["name"] for a in store.activations]
    assert names.count("FlashArray rulebook - capacity") == 1
    assert (
        "the create of FlashArray rulebook - capacity had gone through as id 101 "
        "although the gateway answered an error; keeping it"
    ) in out.out
    assert code == 0


def test_recreate_finds_the_create_the_gateway_kept_answering_errors_to(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    # the client's first four attempts are answered 503 and do nothing; the
    # fifth is carried out and answered 503 as well, so the client gives up
    store.answer_503 = 4
    store.ghost_create = 1
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert len(_posts(store)) == 5  # looked up by name, not posted again
    assert len(store.created) == 1
    assert "had gone through as id 101" in out.out
    assert code == 0


def test_recreate_keeps_a_create_answered_with_an_empty_body(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.ghost_empty = 1
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert len(_posts(store)) == 1
    assert len(store.created) == 1
    assert "had gone through as id 101" in out.out
    assert code == 0


def test_recreate_keeps_a_create_whose_answer_was_cut_short(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.ghost_truncate = 1
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    # a truncated answer is an audit error, not a traceback, and the lookup follows
    assert len(_posts(store)) == 1
    assert "had gone through as id 101" in out.out
    assert code == 0


def test_recreate_looks_an_empty_answer_up_and_tries_again(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.answer_empty = 1  # nothing was created behind that answer
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert len(_posts(store)) == 2
    assert len(store.created) == 1
    assert "answered 201 with no activation in the body); trying again" in out.out
    assert "created FlashArray rulebook - capacity as id 101" in out.out
    assert code == 0


def test_recreate_stops_at_once_when_the_controller_refuses_the_vars(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.refuse_vars = True
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert len(_posts(store)) == 1  # "already exists in extra vars" is not the name
    assert store.created == []
    assert "the controller refused the create" in out.out + out.err
    assert "already exists in extra vars" in out.out + out.err
    assert "run aap/seed.yml" in out.out + out.err
    assert code == 1


def test_recreate_survives_a_delete_whose_answer_the_gateway_lost(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.delete_503 = 1  # the retry of the delete then meets a 404
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert "the delete of FlashArray rulebook - capacity was answered" in out.out
    assert "HTTP 404" in out.out
    assert "created FlashArray rulebook - capacity as id 101" in out.out
    assert code == 0


def test_recreate_keeps_a_create_answered_with_an_html_page(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.ghost_html = 1
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    # the non-JSON answer became an audit error (not a traceback) and the
    # lookup that follows found the activation
    assert len(_posts(store)) == 1
    assert "had gone through as id 101" in out.out
    assert code == 0


def test_recreate_treats_a_cut_short_refusal_as_unknown_and_tries_again(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.truncate_400 = 1  # a 400 whose body (the name conflict?) never arrived
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert "HTTP 400 (body unreadable)); trying again" in out.out
    assert len(_posts(store)) == 2
    assert len(store.created) == 1
    assert code == 0


def test_recreate_waits_out_a_list_error_after_the_delete(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
        _activation(9, "FlashArray rulebook - health", dict(good, alerts_interval=31)),
    ]
    store.list_503_after_delete = 5  # the client's five attempts all fail once
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert "could not read the activations list" in out.out
    assert "created FlashArray rulebook - capacity as id 101" in out.out
    assert "created FlashArray rulebook - health as id 102" in out.out
    assert code == 0


def test_recreate_says_so_when_the_list_stays_unreadable_after_the_delete(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.list_503_after_delete = 1000  # outlasts the delete wait
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert store.created == []
    assert "the activations list could not be read for 120 s" in out.err
    assert "still listed" not in out.err
    assert "check whether id 8 is gone, then run aap/seed.yml" in out.err
    assert code == 1


def test_audit_reports_a_bodiless_detail_answer_and_keeps_the_ids_done(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
        _activation(9, "FlashArray rulebook - health", dict(good, alerts_interval=31)),
    ]
    store.detail_empty_ids = {9}
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert "GET /api/eda/v1/activations/9/: the answer is not an object" in out.err
    # the first recreation is still reported, beside the error
    assert "recreated as id 101 (running); was id 8" in out.err
    assert code == 1


def test_recreate_waits_out_a_detail_error_after_the_create(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
        _activation(9, "FlashArray rulebook - health", dict(good, alerts_interval=31)),
    ]
    store.detail_503_after_create = 5
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert "could not read FlashArray rulebook - capacity (id 101)" in out.out
    assert "recreated as id 101 (running)" in out.out
    assert "recreated as id 102 (running)" in out.out
    assert code == 0


def test_recreate_gives_up_when_the_controller_keeps_the_name(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.name_held = 50
    monkeypatch.setattr(audit, "NAME_WAIT_SECONDS", 0)
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert store.created == []
    assert "no create went through in 0 s" in out.out + out.err
    assert "already exists" in out.out + out.err  # the last answer is quoted
    assert "run aap/seed.yml" in out.out + out.err
    assert code == 1


def test_recreate_keeps_waiting_when_the_lookup_itself_fails(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
    ]
    store.name_held = 1
    real_list_all = audit.Client.list_all
    calls = {"n": 0}

    def flaky_list_all(self, path):
        # the list answers 5xx once, right after the create was refused
        calls["n"] += 1
        if calls["n"] == 3:
            raise audit.AuditError(f"GET {path}: gave up after 5 attempts")
        return real_list_all(self, path)

    monkeypatch.setattr(audit.Client, "list_all", flaky_list_all)
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert "could not look FlashArray rulebook - capacity up" in out.out
    assert "created FlashArray rulebook - capacity as id 101" in out.out
    assert code == 0


def test_recreate_leaves_a_same_named_activation_with_other_vars_alone(
    audit, store, files, capsys, monkeypatch
):
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [
        _activation(
            8, "FlashArray rulebook - capacity", dict(good, alerts_interval=30)
        ),
        _activation(
            9, "FlashArray rulebook - capacity", dict(good, alerts_interval=99)
        ),
    ]
    code = _run_recreate(audit, store, files, monkeypatch)
    out = capsys.readouterr()
    assert store.created == []
    assert [a["id"] for a in store.activations] == [9]
    assert (
        "appeared as id 9 with other extra vars; not touching it" in out.out + out.err
    )
    assert code == 1


def test_recreate_refuses_while_the_expected_vars_carry_a_placeholder(
    audit, store, tmp_path, capsys, monkeypatch
):
    broken_site = dict(SITE)
    del broken_site[
        "orchestrator_url"
    ]  # the site file lost the key: the example URL comes through
    example = tmp_path / "example.yml"
    site = tmp_path / "site.yml"
    example.write_text(yaml.safe_dump(EXAMPLE))
    site.write_text(yaml.safe_dump(broken_site))
    expected = audit.expected_extra_vars(EXAMPLE, broken_site, "FlashArray", False)
    live = dict(expected, alerts_interval=30)
    store.activations = [_activation(8, "FlashArray rulebook - capacity", live)]
    monkeypatch.setenv("AAP_TOKEN", "tok")
    monkeypatch.setattr(audit.time, "sleep", lambda s: None)
    code = audit.main(
        [
            "--url",
            store.url,
            "--example",
            str(example),
            "--vars",
            str(site),
            "--recreate",
        ]
    )
    out = capsys.readouterr()
    assert code == 1
    assert [c for c in store.calls if c[0] in ("DELETE", "POST")] == []
    assert "expected vars still carry placeholder orchestrator_url" in out.out


def test_cli_needs_the_token_in_the_environment(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "AAP_TOKEN"}
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(TOOL),
            "--url",
            "http://127.0.0.1:9",
            "--vars",
            str(tmp_path / "x.yml"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 2
    assert "AAP_TOKEN" in result.stderr


def test_cli_takes_the_gateway_and_tls_from_the_credentials_environment(
    audit, store, files, capsys, monkeypatch
):
    """In a platform job the "Red Hat Ansible Automation Platform" credential
    injects AAP_HOSTNAME (possibly with an /api/... path) and
    AAP_VALIDATE_CERTS; --url and --insecure default to them."""
    good = audit.expected_extra_vars(EXAMPLE, SITE, "FlashArray", False)
    store.activations = [_activation(7, "FlashArray rulebook - alerts", good)]
    monkeypatch.setenv("AAP_TOKEN", "tok")
    monkeypatch.setenv("AAP_HOSTNAME", store.url + "/api/controller/")
    monkeypatch.setenv("AAP_VALIDATE_CERTS", "False")
    code = audit.main([*files])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.splitlines()[0] == "FlashArray rulebook - alerts: ok (running)"


def test_env_defaults_for_the_gateway_and_tls(audit, monkeypatch):
    for name in ("AAP_HOSTNAME", "CONTROLLER_HOST", "AAP_VALIDATE_CERTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CONTROLLER_VERIFY_SSL", raising=False)
    assert audit.env_url() == ""
    assert audit.env_insecure() is False
    monkeypatch.setenv("CONTROLLER_HOST", "https://gw.example.com/api/controller/")
    assert audit.env_url() == "https://gw.example.com"
    monkeypatch.setenv("AAP_HOSTNAME", "https://aap.example.com/")
    assert audit.env_url() == "https://aap.example.com"  # AAP_HOSTNAME first
    monkeypatch.setenv("CONTROLLER_VERIFY_SSL", "False")
    assert audit.env_insecure() is True
    monkeypatch.setenv("AAP_VALIDATE_CERTS", "True")
    assert audit.env_insecure() is False  # AAP_VALIDATE_CERTS first


def test_cli_needs_a_gateway(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("AAP_HOSTNAME", "CONTROLLER_HOST")
    }
    env["AAP_TOKEN"] = "tok"
    result = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--vars", str(tmp_path / "x.yml")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 2
    assert "AAP_HOSTNAME" in result.stderr
