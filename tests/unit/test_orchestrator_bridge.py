# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The EDA -> orchestrator bridge against the mock orchestrator.

tests/mock_orchestrator.py is served in a thread on a free port; the mock's
own behaviour is checked with urllib, then playbooks/orchestrator_post.yml
is run with ansible-playbook (skipped when it is not on the PATH) against a
temporary alert log: 202 with an execution id and the envelope in the
record file, 401 on a wrong secret, 403 on an unbound path, status -1 when
nothing answers at all (a closed port stands in for the orchestrator being
down), and an oversize event refused locally without a request. The secret
and every issued token must stay out of the playbook output."""

import importlib.util
import json
import os
import pathlib
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "playbooks" / "orchestrator_post.yml"
INVENTORY = ROOT / "inventory" / "example.yml"
CLIENT_ID = "nx_sa_unit_test"
CLIENT_SECRET = "unit-test-secret-not-a-real-one"
BOUND_PATH = "fa_capacity"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")

needs_ansible = pytest.mark.skipif(
    ANSIBLE_PLAYBOOK is None, reason="ansible-playbook is not on PATH"
)


def _load_mock():
    spec = importlib.util.spec_from_file_location(
        "mock_orchestrator", ROOT / "tests" / "mock_orchestrator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mock(tmp_path):
    module = _load_mock()
    record = tmp_path / "record.jsonl"
    server = module.make_server(
        "127.0.0.1", 0, CLIENT_ID, CLIENT_SECRET, [BOUND_PATH], str(record)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(server=server, url=server.url, record=record)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http(method, url, body=None, headers=None):
    """Return (status, parsed JSON body) without raising on 4xx."""
    request = urllib.request.Request(
        url, data=body, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def _token(mock, secret=CLIENT_SECRET):
    form = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": secret,
        }
    ).encode()
    return _http(
        "POST",
        f"{mock.url}/api/v1/auth/token",
        form,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _capacity_event(**overrides):
    data = {
        "array": "array-a",
        "used_pct": 91.2,
        "used_bytes": 45600000000000,
        "capacity_bytes": 50000000000000,
        "days_to_full": 12,
        "days_to_full_known": True,
        "growth_bytes_per_day": 366666666666,
        "top_growers": [{"volume": "vol-db-01", "growth_bytes": 120000000000}],
        "pending_eradication_volumes": 1,
        "pending_eradication_snapshots": 3,
        "others": [{"array": "array-b", "used_pct": 40.5}],
    }
    data.update(overrides)
    return {
        "purefa_capacity": data,
        "meta": {
            "source": {"name": "purefa_events", "type": "purefa_events"},
            "received_at": "2026-11-05T10:00:00.000000Z",
            "uuid": "00000000-0000-4000-8000-000000000001",
        },
    }


# --- the mock itself -----------------------------------------------------


def test_mock_health_token_and_refusals(mock):
    assert _http("GET", f"{mock.url}/healthz")[0] == 200

    status, body = _token(mock, "wrong")
    assert status == 401

    status, body = _token(mock)
    assert status == 200
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    token = body["access_token"]
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    status, _ = _http(
        "POST", f"{mock.url}/api/v1/webhooks/eda/{BOUND_PATH}", b'{"event": {}}'
    )
    assert status == 401, "no bearer token must be 401"

    status, _ = _http(
        "POST", f"{mock.url}/api/v1/webhooks/eda/not_bound", b'{"event": {}}', auth
    )
    assert status == 403, "a path the service account is not bound to must be 403"

    big = json.dumps({"event": {"blob": "x" * (1024 * 1024)}}).encode()
    status, _ = _http("POST", f"{mock.url}/api/v1/webhooks/eda/{BOUND_PATH}", big, auth)
    assert status == 413

    status, body = _http(
        "POST",
        f"{mock.url}/api/v1/webhooks/eda/{BOUND_PATH}",
        b'{"event": {"a": 1}}',
        auth,
    )
    assert status == 202 and body["execution_id"]
    assert _records(mock.record) == [{"event": {"a": 1}}]
    assert mock.server.webhook_calls == [
        (BOUND_PATH, 401),
        ("not_bound", 403),
        (BOUND_PATH, 413),
        (BOUND_PATH, 202),
    ]


# --- the playbook --------------------------------------------------------


def _run_playbook(
    tmp_path,
    mock,
    event,
    path=BOUND_PATH,
    secret=CLIENT_SECRET,
    env_extra=None,
    url=None,
):
    """Run orchestrator_post.yml once against the mock, or against `url`;
    return (rc, alert-log records, output)."""
    alert_log = tmp_path / "alerts.log"
    extra = tmp_path / "extra_vars.json"
    extra.write_text(
        json.dumps(
            {
                "orchestrator_url": mock.url if url is None else url,
                "orchestrator_webhook_path": path,
                "event": event,
                "array": event["purefa_capacity"]["array"],
                "alert_log": str(alert_log),
            }
        )
    )
    env = dict(os.environ)
    env.pop("ANSIBLE_CONFIG", None)
    env.update(
        {
            "AO_CLIENT_ID": CLIENT_ID,
            "AO_CLIENT_SECRET": secret,
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_VERBOSITY": "1",
            "PYTHONWARNINGS": "ignore",
        }
    )
    env.update(env_extra or {})
    proc = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", str(INVENTORY), "-e", f"@{extra}", str(PLAYBOOK)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    output = proc.stdout + proc.stderr
    return proc.returncode, _records(alert_log), output


def _assert_no_secret_leaked(mock, output):
    assert (
        CLIENT_SECRET not in output
    ), "the client secret appeared in the playbook output"
    for token in mock.server.issued_tokens:
        assert token not in output, "a bearer token appeared in the playbook output"


@needs_ansible
def test_accepted_event_is_recorded_with_its_execution_id(tmp_path, mock):
    rc, records, output = _run_playbook(tmp_path, mock, _capacity_event())
    assert rc == 0, output
    _assert_no_secret_leaked(mock, output)

    assert len(records) == 1, records
    record = records[0]
    assert record["kind"] == "orchestrator_post"
    assert record["severity"] == "info"
    assert record["array"] == "array-a"
    assert record["status"] == 202
    assert record["stage"] == "post"
    assert record["webhook_path"] == BOUND_PATH
    assert record["execution_id"] in mock.server.execution_ids
    assert record["reason"] == ""
    for key in ("time", "kind", "severity", "array"):
        assert key in record

    landed = _records(mock.record)
    assert len(landed) == 1, landed
    envelope = landed[0]["event"]
    assert envelope["schema"] == "flasharray-eda/1"
    assert envelope["event_type"] == "purefa_capacity"
    assert envelope["array"] == "array-a"
    assert envelope["maintenance_active"] is False
    assert envelope["observed_at"].endswith("Z")
    assert envelope["data"]["used_pct"] == 91.2
    assert envelope["data"]["top_growers"][0]["volume"] == "vol-db-01"
    assert "meta" not in envelope["data"]
    assert sorted(landed[0]) == ["event"], "the body is {event: ...} and nothing else"


@needs_ansible
def test_wrong_secret_records_a_401_warning_and_sends_nothing(tmp_path, mock):
    rc, records, output = _run_playbook(
        tmp_path, mock, _capacity_event(), secret="not-it"
    )
    assert rc == 0, output
    assert "not-it" not in output
    assert len(records) == 1, records
    record = records[0]
    assert record["severity"] == "warning"
    assert record["stage"] == "token"
    assert record["status"] == 401
    assert record["execution_id"] == ""
    assert record["reason"].startswith("token request failed: ")
    assert _records(mock.record) == []
    assert mock.server.webhook_calls == []


@needs_ansible
def test_unreachable_orchestrator_records_a_minus_one_warning(tmp_path):
    """No HTTP answer at all (connection refused here; a timeout or a TLS
    failure look the same to the playbook): the uri module reports status
    -1, the record says so at the token stage and the run still succeeds."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    rc, records, output = _run_playbook(
        tmp_path, None, _capacity_event(), url=f"http://127.0.0.1:{port}"
    )
    assert rc == 0, output
    assert CLIENT_SECRET not in output
    assert len(records) == 1, records
    record = records[0]
    assert record["severity"] == "warning"
    assert record["stage"] == "token"
    assert record["status"] == -1
    assert record["execution_id"] == ""
    assert record["reason"].startswith("token request failed: ")
    assert record["body_bytes"] > 0


@needs_ansible
def test_unbound_path_records_a_403_warning(tmp_path, mock):
    rc, records, output = _run_playbook(
        tmp_path, mock, _capacity_event(), path="fa_other"
    )
    assert rc == 0, output
    _assert_no_secret_leaked(mock, output)
    assert len(records) == 1, records
    record = records[0]
    assert record["severity"] == "warning"
    assert record["stage"] == "post"
    assert record["status"] == 403
    assert record["webhook_path"] == "fa_other"
    assert record["execution_id"] == ""
    assert record["reason"]
    assert _records(mock.record) == []
    assert mock.server.webhook_calls == [("fa_other", 403)]


@needs_ansible
def test_oversize_event_is_refused_locally_without_a_request(tmp_path, mock):
    event = _capacity_event(padding="x" * 1_100_000)
    rc, records, output = _run_playbook(tmp_path, mock, event)
    assert rc == 0, output
    assert len(records) == 1, records
    record = records[0]
    assert record["severity"] == "warning"
    assert record["stage"] == "size_check"
    assert record["status"] is None
    assert record["body_bytes"] > 1_000_000
    assert "not sent" in record["reason"]
    assert (
        mock.server.issued_tokens == []
    ), "no token must be requested for a refused body"
    assert mock.server.webhook_calls == []
    assert _records(mock.record) == []


@needs_ansible
def test_missing_credentials_fail_before_any_request(tmp_path, mock):
    rc, records, output = _run_playbook(
        tmp_path, mock, _capacity_event(), env_extra={"AO_CLIENT_SECRET": ""}
    )
    assert rc != 0
    assert "AO_CLIENT_ID and AO_CLIENT_SECRET" in output
    assert records == []
    assert mock.server.issued_tokens == []
    assert mock.server.webhook_calls == []
