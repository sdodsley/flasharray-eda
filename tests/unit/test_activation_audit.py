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

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        store = self.server.store
        store.calls.append(("GET", self.path))
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send(401, {"detail": "no"})
        if self.path.startswith("/api/eda/v1/activations/?"):
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
                return self._send(200, a)
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        store = self.server.store
        store.calls.append(("POST", self.path))
        body = self._body()
        if self.path == "/api/eda/v1/activations/":
            if store.name_held:
                store.name_held -= 1
                return self._send(
                    400, {"name": ["activation with this name already exists."]}
                )
            store.next_id += 1
            created = dict(body, id=store.next_id, status="running")
            created["eda_credentials"] = [
                {"id": i, "name": f"cred-{i}"} for i in body.get("eda_credentials", [])
            ]
            for key in ("project", "rulebook", "decision_environment", "organization"):
                created[key] = {"id": body.get(key + "_id")}
            store.activations.append(created)
            store.created.append(body)
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
        self._send(204 if len(store.activations) < before else 404, {})


class Store:
    def __init__(self):
        self.calls = []
        self.activations = []
        self.created = []
        self.next_id = 100
        self.name_held = 0  # creates refused with 'already exists' before one succeeds
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
