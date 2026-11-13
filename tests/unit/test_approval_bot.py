# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The Slack approval bot's card, decision, reminder, final-status and state
logic, driven with a fake HTTP function, a fake Slack client and a fake
clock; nothing here needs slack_sdk or a network."""

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
    """Records calls and answers from a script keyed by (method, path
    prefix); the first key that matches wins, so a detail route goes before
    the list route it extends."""

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
        return {"ts": f"{len(self.posted)}.000", "channel": "C1"}

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


T0 = "2026-11-13T10:00:00Z"
T1 = "2026-11-13T10:16:40Z"  # 1000 s later

PENDING_ONE = {
    "resources": [
        {
            "id": "ap-1",
            "name": "Approve",
            "prompt": "x",
            "created_at": T0,
            "timeout_at": T1,
        }
    ]
}


def _orchestrator(http):
    return None  # placeholder replaced below by the fixture-aware helper


def test_parse_time(bot):
    base = bot.parse_time(T0)
    assert base is not None
    assert bot.parse_time(T1) - base == 1000
    assert bot.parse_time("2026-11-13T10:00:00.738623Z") - base == pytest.approx(
        0.738623
    )
    assert bot.parse_time("2026-11-13T10:00:00.1234567890Z") - base == pytest.approx(
        0.123456
    )
    assert bot.parse_time("2026-11-13T11:00:00+01:00") == base
    assert bot.parse_time("2026-11-13T05:00:00-05:00") == base
    assert bot.parse_time("2026-11-13T10:00:00") == base  # naive is UTC
    assert bot.parse_time("") is None and bot.parse_time(None) is None
    assert bot.parse_time("yesterday") is None
    a = bot.Approval("orchestrator", "1", "t", "b", created=T0, expires=T1)
    assert a.window() == (base, base + 1000)
    assert bot.Approval("orchestrator", "1", "t", "b", expires=T1).window() is None
    assert (
        bot.Approval("orchestrator", "1", "t", "b", created=T1, expires=T0).window()
        is None
    )


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
                            "name": "Approve eradicating",
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
    # the execution read failed (no route), so the workflow is the id's head
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
    # a second call within the token's life does not log in again, and the
    # execution is not read again either
    o.pending()
    assert sum(1 for c in http.calls if "/auth/token" in c[1]) == 1
    assert sum(1 for c in http.calls if "/api/v1/executions/" in c[1]) == 1


def test_orchestrator_reads_the_window_and_the_workflow_name(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (
                200,
                {
                    "resources": [
                        {
                            "id": "ap-7",
                            "execution_id": "64ea199c-3346",
                            "name": "Approve the failback",
                            "prompt": "Fail back?",
                            "created_at": T0,
                            "timeout_at": T1,
                            "status": "pending",
                        }
                    ]
                },
            ),
            ("GET", "/api/v1/executions/64ea199c-3346"): (
                200,
                {"id": "64ea199c-3346", "workflow_name": "FlashArray - DR failover"},
            ),
        }
    )
    o = bot.Orchestrator("https://ao", username="a", password="p", http=http)
    (a,) = o.pending()
    assert (a.created, a.expires, a.workflow) == (T0, T1, "FlashArray - DR failover")
    assert a.window() == (bot.parse_time(T0), bot.parse_time(T1))
    assert "expires: " + T1 in bot.build_card(a)[2]["elements"][0]["text"]


def test_pending_lists_follow_the_pages(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "cursor=c2"): (
                200,
                {"resources": [{"id": "ap-2", "name": "two", "prompt": "p"}]},
            ),
            ("GET", "/api/v1/approvals"): (
                200,
                {
                    "resources": [{"id": "ap-1", "name": "one", "prompt": "p"}],
                    "next_cursor": "c2",
                },
            ),
            ("GET", "page=2"): (
                200,
                {
                    "results": [
                        {"id": 2, "name": "b", "description": "", "summary_fields": {}}
                    ]
                },
            ),
            ("GET", "/api/controller/v2/workflow_approvals/"): (
                200,
                {
                    "results": [
                        {"id": 1, "name": "a", "description": "", "summary_fields": {}}
                    ],
                    "next": "/api/controller/v2/workflow_approvals/?page=2&status=pending",
                },
            ),
        }
    )
    o = bot.Orchestrator("https://ao", username="a", password="p", http=http)
    assert [a.id for a in o.pending()] == ["ap-1", "ap-2"]
    c = bot.Controller("https://aap", "tok", http=http)
    assert [a.id for a in c.pending()] == ["1", "2"]
    second = [x for x in http.calls if "page=2" in x[1]][0]
    assert second[1].startswith(
        "https://aap/api/controller/v2/workflow_approvals/?page=2"
    )


def test_final_status_of_both_systems(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals/ap-x"): (
                200,
                {
                    "id": "ap-x",
                    "status": "expired",
                    "decided_by": {"id": "b", "name": "worker.ao.svc"},
                    "decision_notes": "Approval decision window expired",
                    "decided_at": T1,
                },
            ),
            ("GET", "/api/v1/approvals/ap-a"): (
                200,
                {
                    "status": "approved",
                    "decided_by": {"name": "Administrator"},
                    "decision_notes": "fine",
                },
            ),
            ("GET", "/api/v1/approvals/ap-c"): (200, {"status": "cancelled"}),
            ("GET", "/api/v1/approvals/ap-z"): (200, {"status": "pending"}),
            ("GET", "/api/v1/approvals/ap-odd"): (200, {"status": "frozen"}),
            ("GET", "/api/controller/v2/workflow_approvals/1/"): (
                200,
                {
                    "status": "successful",
                    "timed_out": False,
                    "finished": T1,
                    "summary_fields": {"approved_or_denied_by": {"username": "admin"}},
                },
            ),
            ("GET", "/api/controller/v2/workflow_approvals/2/"): (
                200,
                {"status": "failed", "timed_out": True, "summary_fields": {}},
            ),
            ("GET", "/api/controller/v2/workflow_approvals/3/"): (
                200,
                {
                    "status": "failed",
                    "timed_out": False,
                    "summary_fields": {"approved_or_denied_by": {"username": "jo"}},
                },
            ),
            ("GET", "/api/controller/v2/workflow_approvals/4/"): (
                200,
                {"status": "canceled", "summary_fields": {}},
            ),
            ("GET", "/api/controller/v2/workflow_approvals/5/"): (
                200,
                {"status": "pending", "summary_fields": {}},
            ),
        }
    )
    o = bot.Orchestrator("https://ao", username="a", password="p", http=http)
    assert o.final("ap-x") == {
        "status": bot.EXPIRED,
        "who": "worker.ao.svc",
        "note": "Approval decision window expired",
        "at": T1,
    }
    assert o.final("ap-a")["status"] == bot.APPROVED
    assert o.final("ap-a")["who"] == "Administrator"
    assert o.final("ap-c")["status"] == bot.CANCELLED
    assert o.final("ap-z")["status"] == bot.PENDING  # still open: not an end
    assert o.final("ap-odd")["status"] == bot.UNKNOWN
    with pytest.raises(RuntimeError, match="404"):
        o.final("ap-missing")  # a failed read is not an answer
    c = bot.Controller("https://aap", "tok", http=http)
    assert c.final("1") == {
        "status": bot.APPROVED,
        "who": "admin",
        "note": "",
        "at": T1,
    }
    assert c.final("2")["status"] == bot.EXPIRED
    assert c.final("3") == {"status": bot.REJECTED, "who": "jo", "note": "", "at": ""}
    assert c.final("4")["status"] == bot.CANCELLED
    assert c.final("5")["status"] == bot.PENDING
    with pytest.raises(RuntimeError, match="404"):
        c.final("9")


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


def test_controller_lists_decides_and_notifies(bot):
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
                            "approval_expiration": T1,
                            "created": T0,
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
            ("GET", "/api/controller/v2/job_templates/?name="): (
                200,
                {"results": [{"id": 10, "name": "FlashArray - notify"}]},
            ),
            ("POST", "/api/controller/v2/job_templates/10/launch/"): (
                201,
                {"id": 77, "job": 77},
            ),
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
    assert (a.created, a.expires) == (T0, T1)
    assert a.link == "https://aap.example.com/execution/jobs/workflow/12/output"
    # a UI address people can resolve, when the gateway name is not one
    (b,) = bot.Controller(
        "https://aap.example.com", "tok", http=http, ui_url="https://192.0.2.10/"
    ).pending()
    assert b.link == "https://192.0.2.10/execution/jobs/workflow/12/output"
    c.decide("14", False, "via Slack by jane (U1)")
    assert http.calls[-1][0:2] == (
        "POST",
        "https://aap.example.com/api/controller/v2/workflow_approvals/14/deny/",
    )
    assert http.calls[-1][3]["Authorization"] == "Bearer tok"
    # the notify launch: the template looked up by name once, then launched
    assert c.notify("approval expired: x", "nobody decided") == 77
    assert c.notify("again", "d") == 77
    lookups = [x for x in http.calls if "job_templates/?name=" in x[1]]
    assert len(lookups) == 1 and "FlashArray%20-%20notify" in lookups[0][1]
    launch = [x for x in http.calls if x[1].endswith("/launch/")][-1]
    assert launch[2]["extra_vars"] == {
        "severity": "critical",
        "subject": "again",
        "detail": "d",
        "target_array": "",
    }
    with pytest.raises(RuntimeError, match="no notify template"):
        bot.Controller("https://aap", "tok", http=http, notify_template="").notify(
            "s", "d"
        )


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
    expired = bot.closed_card(a, bot.EXPIRED, "worker.ao.svc")
    assert expired[0]["text"]["text"].startswith("Expired: ")
    assert "nobody decided" in expired[-1]["elements"][0]["text"]
    assert "execution failed at this node" in expired[-1]["elements"][0]["text"]
    assert all(b["type"] != "actions" for b in expired)
    cancelled = bot.closed_card(a, bot.CANCELLED)
    assert cancelled[0]["text"]["text"].startswith("Cancelled: ")
    unknown = bot.closed_card(a, bot.UNKNOWN)
    assert "decided outside Slack or expired" in unknown[-1]["elements"][0]["text"]
    assert "<@U1>" in bot.expiry_text(a, "<@U1>")
    assert ":rotating_light:" in bot.expiry_text(a)
    assert "execution failed at this node" in bot.expiry_text(a)
    # the controller's timed-out node sends its workflow down the failure edge
    c = bot.Approval("controller", "4", "t", "b")
    assert "workflow took its failure edge" in bot.expiry_text(c)
    assert "failure edge" in bot.closed_card(c, bot.EXPIRED)[-1]["elements"][0]["text"]
    assert "Still waiting" in bot.reminder_text(a, 0.5, 500, "<@U1>")
    assert "<@U1>" not in bot.reminder_text(a, 0.5, 500, "<@U1>")
    last = bot.reminder_text(a, 0.9, 100, "<@U1>")
    assert last.startswith("<@U1> Last call") and "100 s left" in last


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
                        {"id": "ap-1", "prompt": "Eradicate?", "name": "Approve"}
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
    assert b.posted["orchestrator:ap-1"].ts == "1.000"
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
    # an approval that disappears from the queue is closed: here the detail
    # read answers with the list (no status), so the end is unknown
    http.answers[("GET", "/api/v1/approvals")] = (
        200,
        {"resources": [{"id": "ap-2", "prompt": "x", "name": "n"}]},
    )
    b.poll_once()
    assert "orchestrator:ap-2" in b.posted
    http.answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    b.poll_once()
    assert "orchestrator:ap-2" not in b.posted
    assert web.updated[-1]["text"] == "Decided elsewhere: n"
    assert (
        "decided outside Slack or expired"
        in web.updated[-1]["blocks"][-1]["elements"][0]["text"]
    )
    # the update names the channel id the post returned, not the configured name
    assert web.updated[-1]["channel"] == "C1"


def test_cards_survive_a_restart_and_close_with_the_final_status(bot, tmp_path):
    answers = {
        ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
        ("GET", "/api/v1/approvals/ap-1"): (
            200,
            {
                "status": "approved",
                "decided_by": {"name": "Administrator"},
                "decision_notes": "looks right",
            },
        ),
        ("GET", "/api/v1/approvals"): (200, PENDING_ONE),
    }
    http = FakeHttp(answers)
    store = bot.CardStore(tmp_path / "deeper" / "state")  # a directory not there yet
    web1 = FakeWeb()
    first = bot.Bot(
        web1,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        store=store,
        now=Clock(bot.parse_time(T0) + 10),
    )
    first.poll_once()
    assert len(web1.posted) == 1
    saved = json.loads(store.path.read_text())
    assert saved["orchestrator:ap-1"]["ts"] == "1.000"
    assert saved["orchestrator:ap-1"]["approval"]["expires"] == T1
    # a restart: the card is remembered, nothing is posted again
    web2 = FakeWeb()
    second = bot.Bot(
        web2,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        store=store,
        now=Clock(bot.parse_time(T0) + 20),
    )
    assert second.posted["orchestrator:ap-1"].ts == "1.000"
    second.poll_once()
    assert web2.posted == []
    # the approval is decided in the UI: the remembered card is rewritten
    # with who decided, and the record forgets it
    answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    second.poll_once()
    assert web2.updated[-1]["ts"] == "1.000" and web2.updated[-1]["channel"] == "C1"
    assert web2.updated[-1]["text"] == "approved Approve by Administrator"
    context = web2.updated[-1]["blocks"][-1]["elements"][0]["text"]
    assert "approved by Administrator via the orchestrator" in context
    assert json.loads(store.path.read_text()) == {}
    # an unreadable state file is an empty record, not a crash
    store.path.write_text("{not json")
    assert bot.CardStore(store.path.parent).load() == {}


def test_an_expired_approval_pages_and_records(bot):
    answers = {
        ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
        ("GET", "/api/v1/approvals/ap-1"): (
            200,
            {
                "status": "expired",
                "decided_by": {"name": "worker.ao.svc"},
                "decision_notes": "Approval decision window expired",
            },
        ),
        ("GET", "/api/v1/approvals"): (
            200,
            {
                "resources": [
                    {
                        "id": "ap-1",
                        "name": "Approve the snapshot",
                        "prompt": "Replicate now?",
                        "execution_id": "ex-1",
                        "created_at": T0,
                        "timeout_at": T1,
                    }
                ]
            },
        ),
        ("GET", "/api/v1/executions/ex-1"): (
            200,
            {"workflow_name": "FlashArray - replicate now with approval"},
        ),
        ("GET", "/api/controller/v2/workflow_approvals/"): (200, {"results": []}),
        ("GET", "/api/controller/v2/job_templates/?name="): (
            200,
            {"results": [{"id": 10}]},
        ),
        ("POST", "/api/controller/v2/job_templates/10/launch/"): (201, {"id": 501}),
    }
    http = FakeHttp(answers)
    web = FakeWeb()
    b = bot.Bot(
        web,
        "#test",
        [
            bot.Orchestrator("https://ao", username="a", password="p", http=http),
            bot.Controller("https://aap", "tok", http=http),
        ],
        approvers=["U2", "U1"],
        now=Clock(bot.parse_time(T0) + 10),
    )
    b.poll_once()
    assert len(web.posted) == 1
    answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    b.poll_once()
    # the card: expired, no buttons
    assert web.updated[-1]["text"] == "Expired: Approve the snapshot"
    assert (
        web.updated[-1]["blocks"][0]["text"]["text"] == "Expired: Approve the snapshot"
    )
    assert "worker.ao.svc" in web.updated[-1]["blocks"][-1]["elements"][0]["text"]
    # the page: in the card's thread and the channel, mentioning the approvers
    page = web.posted[-1]
    assert page["thread_ts"] == "1.000" and page["reply_broadcast"] is True
    assert page["text"].startswith("<@U1> <@U2> :rotating_light: *Approval expired")
    assert "FlashArray - replicate now with approval" in page["text"]
    # the record: the notify template launched at critical
    launch = next(c for c in http.calls if c[1].endswith("/launch/"))
    extra = launch[2]["extra_vars"]
    assert extra["severity"] == "critical"
    assert extra["subject"] == "approval expired, nobody decided: Approve the snapshot"
    assert "nobody decided within the window" in extra["detail"]
    assert "FlashArray - replicate now with approval" in extra["detail"]
    assert "orchestrator:ap-1" not in b.posted


def test_a_timed_out_controller_approval_is_paged_but_not_recorded(bot):
    answers = {
        ("GET", "/api/controller/v2/workflow_approvals/14/"): (
            200,
            {"status": "failed", "timed_out": True, "summary_fields": {}},
        ),
        ("GET", "/api/controller/v2/workflow_approvals/"): (
            200,
            {
                "results": [
                    {
                        "id": 14,
                        "name": "Approve eradication",
                        "description": "d",
                        "summary_fields": {
                            "workflow_job": {"id": 3, "name": "capacity reclaim"}
                        },
                    }
                ]
            },
        ),
        ("GET", "/api/controller/v2/job_templates/?name="): (
            200,
            {"results": [{"id": 10}]},
        ),
        ("POST", "/api/controller/v2/job_templates/10/launch/"): (201, {"id": 9}),
    }
    http = FakeHttp(answers)
    web = FakeWeb()
    b = bot.Bot(web, "#test", [bot.Controller("https://aap", "tok", http=http)])
    b.poll_once()
    answers[("GET", "/api/controller/v2/workflow_approvals/")] = (200, {"results": []})
    b.poll_once()
    assert web.updated[-1]["text"] == "Expired: Approve eradication"
    assert "failure edge" in web.updated[-1]["blocks"][-1]["elements"][0]["text"]
    page = web.posted[-1]
    assert page["reply_broadcast"] is True and "failure edge" in page["text"]
    # the workflow's own notify node is the record: no launch from the bot
    assert not any(c[1].endswith("/launch/") for c in http.calls)


def test_a_cancelled_or_unknown_end_does_not_page(bot):
    answers = {
        ("GET", "/api/controller/v2/workflow_approvals/4/"): (
            200,
            {"status": "canceled", "summary_fields": {}},
        ),
        ("GET", "/api/controller/v2/workflow_approvals/"): (
            200,
            {
                "results": [
                    {
                        "id": 4,
                        "name": "Approve",
                        "description": "",
                        "summary_fields": {},
                    }
                ]
            },
        ),
        ("GET", "/api/controller/v2/job_templates/?name="): (
            200,
            {"results": [{"id": 10}]},
        ),
    }
    http = FakeHttp(answers)
    web = FakeWeb()
    b = bot.Bot(web, "#test", [bot.Controller("https://aap", "tok", http=http)])
    b.poll_once()
    answers[("GET", "/api/controller/v2/workflow_approvals/")] = (200, {"results": []})
    b.poll_once()
    assert web.updated[-1]["text"] == "Cancelled: Approve"
    assert len(web.posted) == 1  # no page
    assert not any(c[1].endswith("/launch/") for c in http.calls)


def test_a_failed_final_read_keeps_the_card_and_an_old_one_is_paged(bot, tmp_path):
    answers = {
        ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
        ("GET", "/api/v1/approvals/ap-1"): (503, "busy"),
        ("GET", "/api/v1/approvals"): (200, PENDING_ONE),
        ("GET", "/api/controller/v2/workflow_approvals/"): (200, {"results": []}),
        ("GET", "/api/controller/v2/job_templates/?name="): (
            200,
            {"results": [{"id": 10}]},
        ),
        ("POST", "/api/controller/v2/job_templates/10/launch/"): (201, {"id": 9}),
    }
    http = FakeHttp(answers)
    web = FakeWeb()
    clock = Clock(bot.parse_time(T0) + 10)
    store = bot.CardStore(tmp_path)
    b = bot.Bot(
        web,
        "#test",
        [
            bot.Orchestrator("https://ao", username="a", password="p", http=http),
            bot.Controller("https://aap", "tok", http=http),
        ],
        store=store,
        now=clock,
    )
    b.poll_once()
    answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    # two polls with the detail unreadable: the card stays, the tries are
    # counted and saved
    b.poll_once()
    b.poll_once()
    assert (
        "orchestrator:ap-1" in b.posted and b.posted["orchestrator:ap-1"].closing == 2
    )
    saved = json.loads((tmp_path / "cards.json").read_text())
    assert saved["orchestrator:ap-1"]["closing"] == 2
    assert web.updated == []
    # the third failure after the window has passed: treated as expired and paged
    clock.at = bot.parse_time(T1) + 5
    b.poll_once()
    assert "orchestrator:ap-1" not in b.posted
    assert web.updated[-1]["text"] == "Expired: Approve"
    assert web.posted[-1]["reply_broadcast"] is True
    assert "expired" in web.posted[-1]["text"]
    assert any(c[1].endswith("/launch/") for c in http.calls)


def test_a_failed_final_read_before_the_window_end_becomes_unknown(bot):
    answers = {
        ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
        ("GET", "/api/v1/approvals/ap-1"): (500, "boom"),
        ("GET", "/api/v1/approvals"): (200, PENDING_ONE),
    }
    http = FakeHttp(answers)
    web = FakeWeb()
    b = bot.Bot(
        web,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        now=Clock(bot.parse_time(T0) + 10),
    )
    b.poll_once()
    answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    for _ in range(bot.CLOSE_TRIES):
        b.poll_once()
    assert "orchestrator:ap-1" not in b.posted
    assert web.updated[-1]["text"] == "Decided elsewhere: Approve"
    assert len(web.posted) == 1  # no page: the window had not passed


def test_a_detail_that_still_says_pending_keeps_the_card(bot):
    one = {
        "results": [{"id": 14, "name": "n", "description": "d", "summary_fields": {}}]
    }
    answers = {
        ("GET", "/api/controller/v2/workflow_approvals/14/"): (
            200,
            {"status": "pending", "summary_fields": {}},
        ),
        ("GET", "/api/controller/v2/workflow_approvals/"): (200, one),
    }
    http = FakeHttp(answers)
    web = FakeWeb()
    b = bot.Bot(web, "#test", [bot.Controller("https://aap", "tok", http=http)])
    b.poll_once()
    answers[("GET", "/api/controller/v2/workflow_approvals/")] = (200, {"results": []})
    b.poll_once()
    b.poll_once()
    assert "controller:14" in b.posted and web.updated == [] and len(web.posted) == 1
    # back in the list: nothing is posted again
    answers[("GET", "/api/controller/v2/workflow_approvals/")] = (200, one)
    b.poll_once()
    assert len(web.posted) == 1


def test_reminders_at_half_and_ninety_percent_of_the_window(bot, tmp_path):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (200, PENDING_ONE),
        }
    )
    base = bot.parse_time(T0)
    clock = Clock(base + 100)
    web = FakeWeb()
    store = bot.CardStore(tmp_path)
    b = bot.Bot(
        web,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        approvers=["U1"],
        store=store,
        now=clock,
    )
    b.poll_once()
    assert len(web.posted) == 1  # the card, no reminder at 10 %
    clock.at = base + 500
    b.poll_once()
    assert len(web.posted) == 2
    half = web.posted[1]
    assert half["thread_ts"] == "1.000" and half["reply_broadcast"] is False
    assert half["text"].startswith("Still waiting") and "8 min left" in half["text"]
    assert "<@U1>" not in half["text"]
    clock.at = base + 600
    b.poll_once()
    assert len(web.posted) == 2  # each reminder once
    clock.at = base + 950
    b.poll_once()
    assert len(web.posted) == 3
    last = web.posted[2]
    assert last["reply_broadcast"] is True and last["text"].startswith(
        "<@U1> Last call"
    )
    assert "50 s left" in last["text"]
    clock.at = base + 990
    b.poll_once()
    assert len(web.posted) == 3
    saved = json.loads((tmp_path / "cards.json").read_text())
    assert saved["orchestrator:ap-1"]["reminded"] == [0.5, 0.9]
    # after the window has passed nothing more is said in the thread
    clock.at = base + 2000
    b.poll_once()
    assert len(web.posted) == 3
    # SLACK_ESCALATE replaces the approver mentions
    other = bot.Bot(FakeWeb(), "#test", [], approvers=["U1"], escalate="<!subteam^S1>")
    assert other._mention() == "<!subteam^S1>"
    assert b._mention() == "<@U1>"


def test_a_card_first_seen_late_gets_one_reminder(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (200, PENDING_ONE),
        }
    )
    web = FakeWeb()
    b = bot.Bot(
        web,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        approvers=["U1"],
        now=Clock(bot.parse_time(T0) + 950),
    )
    b.poll_once()
    assert len(web.posted) == 2  # the card and one reminder, the last call
    assert web.posted[1]["text"].startswith("<@U1> Last call")
    assert b.posted["orchestrator:ap-1"].reminded == [0.5, 0.9]
    b.poll_once()
    assert len(web.posted) == 2


def test_a_failed_reminder_post_is_said_again_next_poll(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (200, PENDING_ONE),
        }
    )

    class FlakyWeb(FakeWeb):
        fail_next = False

        def chat_postMessage(self, **kw):
            if self.fail_next and kw.get("thread_ts"):
                self.fail_next = False
                raise RuntimeError("ratelimited")
            return super().chat_postMessage(**kw)

    web = FlakyWeb()
    clock = Clock(bot.parse_time(T0) + 10)
    b = bot.Bot(
        web,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        now=clock,
    )
    b.poll_once()
    clock.at = bot.parse_time(T0) + 500
    web.fail_next = True
    b.poll_once()
    assert len(web.posted) == 1 and b.posted["orchestrator:ap-1"].reminded == []
    b.poll_once()
    assert len(web.posted) == 2 and b.posted["orchestrator:ap-1"].reminded == [0.5]


def test_no_window_means_no_reminders(bot):
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
                            "approval_expiration": None,
                            "created": T0,
                            "summary_fields": {},
                        }
                    ]
                },
            ),
        }
    )
    web = FakeWeb()
    b = bot.Bot(
        web,
        "#test",
        [bot.Controller("https://aap", "tok", http=http)],
        now=Clock(bot.parse_time(T0) + 10**7),
    )
    b.poll_once()
    b.poll_once()
    assert len(web.posted) == 1


def test_a_click_is_forgotten_before_the_card_is_rewritten_and_the_sweep_sees_the_user(
    bot,
):
    answers = {
        ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
        ("GET", "/api/v1/approvals/ap-1"): (
            200,
            {
                "status": "approved",
                "decided_by": {"name": "slack-approver"},
                "decision_notes": "via Slack by jane (U1)",
            },
        ),
        ("GET", "/api/v1/approvals"): (
            200,
            {"resources": [{"id": "ap-1", "prompt": "x", "name": "Approve"}]},
        ),
        ("PATCH", "/api/v1/approvals/ap-1"): (200, {"status": "approved"}),
    }
    http = FakeHttp(answers)
    holder = {}

    class SweepingWeb(FakeWeb):
        """chat.update for the click runs the poller's sweep first, as the
        other thread would, with the approval already gone from the list."""

        def chat_update(self, **kw):
            if not getattr(self, "swept", False):
                self.swept = True
                answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
                holder["bot"].poll_once()
            return super().chat_update(**kw)

    web = SweepingWeb()
    owner = bot.Bot(
        web,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        approvers=["U1"],
    )
    holder["bot"] = owner
    owner.poll_once()
    assert owner.handle_action(_click("approve")) == "approve"
    # the sweep found nothing to close (the click handler had forgotten the
    # card), so the only rewrite is the click handler's
    assert len(web.updated) == 1
    assert (
        "approved by jane via Slack"
        in web.updated[0]["blocks"][-1]["elements"][0]["text"]
    )
    # a card closed by the sweep for a click made through another bot copy
    # names the Slack user, not the bot's identity
    plain = FakeWeb()
    other = bot.Bot(
        plain,
        "#test",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
    )
    answers[("GET", "/api/v1/approvals")] = (
        200,
        {"resources": [{"id": "ap-1", "prompt": "x", "name": "Approve"}]},
    )
    other.poll_once()
    answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    other.poll_once()
    assert plain.updated[-1]["text"] == "approved Approve by jane"
    assert (
        "approved by jane via Slack"
        in plain.updated[-1]["blocks"][-1]["elements"][0]["text"]
    )


class FailingUpdateWeb(FakeWeb):
    def chat_update(self, **kw):
        raise RuntimeError("channel_not_found")


def test_a_failed_card_update_does_not_stop_the_poll(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (
                200,
                {"resources": [{"id": "ap-1", "prompt": "x", "name": "n"}]},
            ),
            ("GET", "/api/controller/v2/workflow_approvals/"): (200, {"results": []}),
        }
    )
    web = FailingUpdateWeb()
    b = bot.Bot(
        web,
        "#test",
        [
            bot.Orchestrator("https://ao", username="a", password="p", http=http),
            bot.Controller("https://aap", "tok", http=http),
        ],
    )
    b.poll_once()
    http.answers[("GET", "/api/v1/approvals")] = (200, {"resources": []})
    b.poll_once()  # the orchestrator card is stale and its update fails
    assert "orchestrator:ap-1" not in b.posted
    assert sum(1 for c in http.calls if "workflow_approvals" in c[1]) == 2


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


def test_systems_from_env_carry_the_notify_template(bot, monkeypatch):
    monkeypatch.delenv("AO_URL", raising=False)
    monkeypatch.setenv("AAP_HOST", "https://aap.example.com")
    monkeypatch.setenv("AAP_TOKEN", "t")
    monkeypatch.delenv("NOTIFY_TEMPLATE", raising=False)
    (c,) = bot.systems_from_env()
    assert c.notify_template == "FlashArray - notify"
    monkeypatch.setenv("NOTIFY_TEMPLATE", "")
    (c,) = bot.systems_from_env()
    assert c.notify_template == ""


def test_module_has_no_slack_sdk_import_at_module_level():
    text = BOT.read_text()
    head = text.split("def main(")[0]
    assert "slack_sdk" not in head.replace("pip install slack_sdk", "")


def test_routes_send_another_projects_cards_to_its_channel_and_template(bot):
    assert bot.parse_routes("Other=#other-eda, FlashArray = #fa-eda,,bad,=x,y=") == [
        ("Other", "#other-eda"),
        ("FlashArray", "#fa-eda"),
    ]
    assert bot.parse_routes("") == [] and bot.parse_routes(None) == []
    other = bot.Approval(
        "orchestrator", "1", "Approve the grow", "x", workflow="Other - grow"
    )
    fa = bot.Approval("orchestrator", "2", "Approve", "x", workflow="FlashArray - DR")
    by_title = bot.Approval(
        "controller", "3", "Other - capacity reclaim (controller tier)", "x"
    )
    routes = [("other", "#other")]
    assert (
        bot.route_for(other, routes) == "#other"
        and bot.route_for(by_title, routes) == "#other"
    )
    assert bot.route_for(fa, routes) is None
    answers = {
        ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
        ("GET", "/api/v1/approvals/ap-other"): (
            200,
            {
                "status": "expired",
                "decided_by": {"name": "worker"},
                "decision_notes": "expired",
            },
        ),
        ("GET", "/api/v1/approvals"): (
            200,
            {
                "resources": [
                    {
                        "id": "ap-other",
                        "name": "Approve the grow",
                        "prompt": "x",
                        "execution_id": "e1",
                    },
                    {
                        "id": "ap-fa",
                        "name": "Approve",
                        "prompt": "x",
                        "execution_id": "e2",
                    },
                ]
            },
        ),
        ("GET", "/api/v1/executions/e1"): (
            200,
            {"workflow_name": "Other - grow"},
        ),
        ("GET", "/api/v1/executions/e2"): (200, {"workflow_name": "FlashArray - x"}),
        ("GET", "/api/controller/v2/workflow_approvals/"): (200, {"results": []}),
        ("GET", "/api/controller/v2/job_templates/?name=Other"): (
            200,
            {"results": [{"id": 20}]},
        ),
        ("GET", "/api/controller/v2/job_templates/?name="): (
            200,
            {"results": [{"id": 10}]},
        ),
        ("POST", "/api/controller/v2/job_templates/20/launch/"): (201, {"id": 90}),
    }
    http = FakeHttp(answers)

    class ChannelWeb(FakeWeb):
        def chat_postMessage(self, **kw):
            self.posted.append(kw)
            return {"ts": f"{len(self.posted)}.000", "channel": kw["channel"].upper()}

    web = ChannelWeb()
    b = bot.Bot(
        web,
        "#fa",
        [
            bot.Orchestrator("https://ao", username="a", password="p", http=http),
            bot.Controller("https://aap", "tok", http=http),
        ],
        routes=[("Other", "#other")],
        notify_routes=[("Other", "Other - notify")],
    )
    b.poll_once()
    assert [p["channel"] for p in web.posted] == ["#other", "#fa"]
    assert b.posted["orchestrator:ap-other"].channel == "#OTHER"
    # the other project's approval expires: the page goes to its card's
    # channel and the record through that project's template
    answers[("GET", "/api/v1/approvals")] = (
        200,
        {
            "resources": [
                {"id": "ap-fa", "name": "Approve", "prompt": "x", "execution_id": "e2"}
            ]
        },
    )
    b.poll_once()
    assert web.updated[-1]["channel"] == "#OTHER"
    assert web.posted[-1]["channel"] == "#OTHER" and "expired" in web.posted[-1]["text"]
    launch = next(c for c in http.calls if c[1].endswith("/launch/"))
    assert "/job_templates/20/launch/" in launch[1]
    lookup = next(c for c in http.calls if "job_templates/?name=" in c[1])
    assert "Other%20-%20notify" in lookup[1]


def test_a_channel_the_bot_cannot_post_to_does_not_block_the_other_cards(bot):
    http = FakeHttp(
        {
            ("POST", "/api/v1/auth/login"): (200, {"access_token": "u-tok"}),
            ("GET", "/api/v1/approvals"): (
                200,
                {
                    "resources": [
                        {
                            "id": "ap-other",
                            "name": "Approve",
                            "prompt": "x",
                            "execution_id": "e1",
                        },
                        {
                            "id": "ap-fa",
                            "name": "Approve",
                            "prompt": "x",
                            "execution_id": "e2",
                        },
                    ]
                },
            ),
            ("GET", "/api/v1/executions/e1"): (
                200,
                {"workflow_name": "Other - grow"},
            ),
            ("GET", "/api/v1/executions/e2"): (
                200,
                {"workflow_name": "FlashArray - x"},
            ),
        }
    )

    class NotInChannelWeb(FakeWeb):
        def chat_postMessage(self, **kw):
            if kw["channel"] == "#other":
                raise RuntimeError("not_in_channel")
            return super().chat_postMessage(**kw)

    web = NotInChannelWeb()
    b = bot.Bot(
        web,
        "#fa",
        [bot.Orchestrator("https://ao", username="a", password="p", http=http)],
        routes=[("Other", "#other")],
    )
    b.poll_once()
    assert [p["channel"] for p in web.posted] == ["#fa"]
    assert "orchestrator:ap-fa" in b.posted and "orchestrator:ap-other" not in b.posted
    b.poll_once()  # tried again, still refused, still no card and no crash
    assert len(web.posted) == 1
