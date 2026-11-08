# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The Slack approval bot's card, decision and system logic, driven with a
fake HTTP function and a fake Slack client; nothing here needs slack_sdk or a
network."""

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOT = ROOT / "slack" / "approval_bot.py"


@pytest.fixture(scope="module")
def bot():
    spec = importlib.util.spec_from_file_location("approval_bot", BOT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHttp:
    """Records calls and answers from a script keyed by (method, path prefix)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(
        self,
        url,
        method="GET",
        body=None,
        headers=None,
        verify=True,
        form=False,
        timeout=30,
    ):
        self.calls.append((method, url, body, headers or {}, form))
        for (m, prefix), answer in self.answers.items():
            if m == method and prefix in url:
                return answer
        return 404, {"detail": "no route"}


class FakeWeb:
    def __init__(self):
        self.posted = []
        self.updated = []
        self.ephemeral = []

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ts": f"{len(self.posted)}.000"}

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}


def test_orchestrator_logs_in_lists_and_decides(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/token"): (
                200,
                {"access_token": "t0k", "expires_in": 900},
            ),
            ("GET", "/api/v1/approvals"): (
                200,
                {
                    "resources": [
                        {
                            "id": "ap-1",
                            "execution_id": "ex-12345678",
                            "prompt": "Eradicate 6 objects?",
                            "node_name": "Approve eradicating",
                        }
                    ]
                },
            ),
            ("PATCH", "/api/v1/approvals/ap-1"): (
                200,
                {"id": "ap-1", "status": "approved"},
            ),
        }
    )
    o = bot.Orchestrator(
        "https://ao.example.com:18000/",
        client_id="nx_sa_x",
        client_secret="s3cret",
        verify=False,
        http=http,
    )
    pending = o.pending()
    assert [(a.system, a.id, a.title, a.workflow) for a in pending] == [
        ("orchestrator", "ap-1", "Approve eradicating", "ex-12345")
    ]
    assert pending[0].link == "https://ao.example.com:18000/approvals"
    # the token call is a form post; the listing carries the bearer
    assert (
        http.calls[0][4] is True
        and http.calls[0][2]["grant_type"] == "client_credentials"
    )
    assert http.calls[1][3]["Authorization"] == "Bearer t0k"
    o.decide("ap-1", True, "via Slack by jane (U1)")
    method, url, body, headers, _ = http.calls[-1]
    assert (method, body) == (
        "PATCH",
        {"status": "approved", "decision_notes": "via Slack by jane (U1)"},
    )
    # a second call within the token's life does not log in again
    o.pending()
    assert sum(1 for c in http.calls if "/auth/token" in c[1]) == 1


def test_orchestrator_user_login_and_refusal(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (200, {"resources": []}),
            ("PATCH", "/api/v1/approvals/ap-9"): (403, {"detail": "not an approver"}),
        }
    )
    o = bot.Orchestrator("https://ao", username="admin", password="pw", http=http)
    assert o.pending() == []
    assert (
        http.calls[0][2] == {"username": "admin", "password": "pw"}
        and http.calls[0][4] is False
    )
    with pytest.raises(RuntimeError, match="403"):
        o.decide("ap-9", False, "note")


def test_controller_lists_and_decides(bot):
    http = FakeHttp(
        {
            ("GET", "/api/controller/v2/workflow_approvals/"): (
                200,
                {
                    "results": [
                        {
                            "id": 14,
                            "name": "Approve eradication",
                            "description": "6 candidates",
                            "approval_expiration": None,
                            "summary_fields": {
                                "workflow_job": {
                                    "id": 12,
                                    "name": "capacity reclaim (controller tier)",
                                }
                            },
                        }
                    ]
                },
            ),
            ("POST", "/api/controller/v2/workflow_approvals/14/deny/"): (204, None),
        }
    )
    c = bot.Controller("https://aap.example.com", "tok", verify=False, http=http)
    (a,) = c.pending()
    assert (a.system, a.id, a.title, a.body, a.workflow) == (
        "controller",
        "14",
        "Approve eradication",
        "6 candidates",
        "capacity reclaim (controller tier)",
    )
    assert a.link == "https://aap.example.com/execution/jobs/workflow/12/output"
    c.decide("14", False, "via Slack by jane (U1)")
    assert http.calls[-1][0:2] == (
        "POST",
        "https://aap.example.com/api/controller/v2/workflow_approvals/14/deny/",
    )
    assert http.calls[-1][3]["Authorization"] == "Bearer tok"


def test_card_carries_both_buttons_and_the_identity(bot):
    a = bot.Approval(
        "orchestrator",
        "ap-1",
        "Approve eradicating",
        "Eradicate the 6 objects on array-a?",
        link="https://ao/approvals",
        expires="2026-11-08T10:00:00Z",
        workflow="capacity reclaim",
    )
    blocks = bot.build_card(a)
    assert (
        blocks[0]["type"] == "header"
        and "Approve eradicating" in blocks[0]["text"]["text"]
    )
    assert blocks[1]["text"]["text"] == "Eradicate the 6 objects on array-a?"
    actions = blocks[-1]
    assert (
        actions["type"] == "actions"
        and actions["block_id"] == "flasharray_eda_orchestrator:ap-1"
    )
    ids = [e["action_id"] for e in actions["elements"]]
    assert ids == ["flasharray_eda_approve", "flasharray_eda_reject"]
    for element in actions["elements"]:
        assert json.loads(element["value"]) == {"system": "orchestrator", "id": "ap-1"}
    context = blocks[2]["elements"][0]["text"]
    assert (
        "workflow: capacity reclaim" in context
        and "expires: 2026-11-08T10:00:00Z" in context
        and "<https://ao/approvals|open>" in context
    )
    done = bot.decided_card(a, bot.APPROVE, "jane", "Slack")
    assert all(b["type"] != "actions" for b in done)
    assert "approved by jane via Slack" in done[-1]["elements"][0]["text"]


def _click(decision, user="U1", system="orchestrator", ident="ap-1"):
    return {
        "type": "block_actions",
        "user": {"id": user, "username": "jane"},
        "channel": {"id": "C1"},
        "message": {"ts": "1.000"},
        "actions": [
            {
                "action_id": "flasharray_eda_" + decision,
                "value": json.dumps({"system": system, "id": ident}),
            }
        ],
    }


def test_parse_action(bot):
    assert bot.parse_action(_click("approve")) == (
        "orchestrator",
        "ap-1",
        "approve",
        "U1",
        "jane",
        "C1",
        "1.000",
    )
    assert bot.parse_action({"type": "view_submission"}) is None
    other = _click("approve")
    other["actions"][0]["action_id"] = "someone_elses_button"
    assert bot.parse_action(other) is None
    assert (
        bot.allowed("U1", set())
        and bot.allowed("U1", {"U1", "U2"})
        and not bot.allowed("U3", {"U1"})
    )


def test_bot_posts_new_approvals_once_and_carries_a_click_back(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (
                200,
                {
                    "resources": [
                        {"id": "ap-1", "prompt": "Eradicate?", "node_name": "Approve"}
                    ]
                },
            ),
            ("PATCH", "/api/v1/approvals/ap-1"): (200, {"status": "approved"}),
        }
    )
    web = FakeWeb()
    b = bot.Bot(
        web,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        approvers=["U1"],
        poll_seconds=1,
    )
    b.poll_once()
    b.poll_once()
    assert len(web.posted) == 1 and web.posted[0]["channel"] == "#test"
    assert b.posted["orchestrator:ap-1"][0] == "1.000"
    # a stranger's click is refused with an ephemeral message and no decision
    assert b.handle_action(_click("approve", user="U9")) == "refused"
    assert web.ephemeral and not any(c[0] == "PATCH" for c in http.calls)
    # the approver's click decides and rewrites the card without buttons
    assert b.handle_action(_click("approve")) == "approve"
    patch = next(c for c in http.calls if c[0] == "PATCH")
    assert patch[2] == {
        "status": "approved",
        "decision_notes": "via Slack by jane (U1)",
    }
    assert web.updated[-1]["ts"] == "1.000" and all(
        x["type"] != "actions" for x in web.updated[-1]["blocks"]
    )
    assert "orchestrator:ap-1" not in b.posted
    # an approval that disappears from the queue (decided in the UI) loses its buttons
    http.answers[("GET", "/api/v1/approvals")] = (
        200,
        {"resources": [{"id": "ap-2", "prompt": "x", "node_name": "n"}]},
    )
    b.poll_once()
    assert "orchestrator:ap-2" in b.posted
    http.answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    b.poll_once()
    assert (
        "orchestrator:ap-2" not in b.posted
        and "decided outside Slack" in web.updated[-1]["blocks"][0]["text"]["text"]
    )


def test_bot_reports_a_refused_decision(bot):
    http = FakeHttp(
        {
            ("GET", "/api/controller/v2/workflow_approvals/"): (
                200,
                {
                    "results": [
                        {
                            "id": 14,
                            "name": "n",
                            "description": "d",
                            "summary_fields": {},
                        }
                    ]
                },
            ),
            ("POST", "/api/controller/v2/workflow_approvals/14/approve/"): (
                403,
                {"detail": "no"},
            ),
        }
    )
    web = FakeWeb()
    b = bot.Bot(web, "#test", [bot.Controller("https://aap", "tok", http=http)])
    b.poll_once()
    assert (
        b.handle_action(_click("approve", system="controller", ident="14")) == "failed"
    )
    assert (
        "refused the decision" in web.ephemeral[-1]["text"]
        and "controller:14" in b.posted
    )


def test_secrets_can_come_from_files(bot, tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("file-token\n")
    monkeypatch.delenv("AAP_TOKEN", raising=False)
    monkeypatch.setenv("AAP_TOKEN_FILE", str(secret))
    assert bot._read("AAP_TOKEN", "AAP_TOKEN_FILE") == "file-token"
    monkeypatch.setenv("AAP_TOKEN", "env-token")
    assert bot._read("AAP_TOKEN", "AAP_TOKEN_FILE") == "env-token"
    assert (
        bot._truthy("false") is False
        and bot._truthy("") is True
        and bot._truthy("0") is False
    )


def test_module_has_no_slack_sdk_import_at_module_level():
    text = BOT.read_text()
    head = text.split("def main():")[0]
    assert "slack_sdk" not in head.replace("pip install slack_sdk", "")
