#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Slack approval bot for flasharray-eda.

Posts every pending approval of the automation orchestrator and of automation
controller's workflow approval nodes to one Slack channel as a card with
Approve and Reject buttons, and carries the click back as the decision, with
the Slack user's name in the decision notes. Slack Socket Mode: the bot opens
an outbound websocket, so it needs no public endpoint and no firewall change.

The card follows the approval to its end. While it waits, the bot reminds the
channel in the card's thread at half and at ninety percent of the decision
window (the second reminder mentions the approvers, or SLACK_ESCALATE, and is
shown in the channel too). When the approval leaves the queue the bot reads
its final status and rewrites the card: approved or rejected by whom (a
decision in a UI as much as a click here), cancelled, or expired. An expired
orchestrator approval fails its execution with nothing done and nobody told,
so it also gets a critical message in the channel and a critical notify
record through the controller's notify template; a timed-out controller
approval sends its workflow down the failure edge, whose own notify node is
the record, so it gets the channel message only. A read of the final status
that fails is retried on the next polls, and an approval still unreadable
once its window has passed is treated as expired rather than forgotten. The
cards are kept under STATE_DIR, so a restart edits the cards it posted
instead of posting them again and still closes them when their approvals
end.

Environment
-----------
SLACK_BOT_TOKEN      bot token (xoxb-...) with chat:write; the bot must be a
                     member of the channel
SLACK_APP_TOKEN      app-level token (xapp-...) with connections:write;
                     Socket Mode and Interactivity switched on in the app
SLACK_CHANNEL        channel name or id the cards go to
SLACK_APPROVERS      comma-separated Slack user ids allowed to click; empty
                     lets anyone in the channel decide (a lab setting)
SLACK_ESCALATE       text put in front of the ninety-percent reminder and the
                     expiry message, for example <!subteam^S0123> or <!here>;
                     empty mentions SLACK_APPROVERS, or nobody
STATE_DIR            directory for cards.json, the record of the cards the bot
                     posted; unset keeps the record in memory only
NOTIFY_TEMPLATE      the controller job template that writes the critical
                     notify record of an expired orchestrator approval
                     (default "FlashArray - notify"; empty = no record; needs
                     AAP_HOST and a token with execute on it)
SLACK_ROUTES         "prefix=channel,prefix=channel": approvals whose workflow
                     name or title starts with a prefix are posted to that
                     channel instead of SLACK_CHANNEL (two projects on one
                     Slack app: Socket Mode hands each click to one open
                     connection, so one bot instance serves both and routes)
NOTIFY_ROUTES        "prefix=template": the notify template for the expiry
                     record of approvals matching the prefix (default
                     NOTIFY_TEMPLATE)
POLL_SECONDS         how often the approval queues are read (default 30)
AO_URL               orchestrator API base, e.g. https://orchestrator:18000;
                     unset = orchestrator approvals are not watched
AO_UI_URL            where the "open" link points (default AO_URL)
AO_VERIFY_CERTS      true (default) or false
AO_CLIENT_ID / AO_CLIENT_SECRET   a service account with the approval:decide
                     permission, or
AO_USERNAME / AO_PASSWORD_FILE    a user login (the password read from a file)
AAP_HOST             platform gateway, e.g. https://aap.example.com; unset =
                     controller approvals are not watched
AAP_UI_URL           where the controller "open" link points (default AAP_HOST)
AAP_TOKEN / AAP_TOKEN_FILE        a controller token with approve rights on
                     the workflows (and execute on NOTIFY_TEMPLATE)
AAP_VERIFY_CERTS     true (default) or false

Every *_FILE variable reads the value from a file, so a container can mount
secrets instead of carrying them in its environment. Nothing secret is ever
logged.

Run
---
    pip install slack_sdk
    python slack/approval_bot.py [--state-dir DIR]

or as a container beside the other listeners (slack/README.md). The Slack
SDK is imported only by main(), so the card and decision logic can be unit
tested without it.
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("approval_bot")

APPROVE = "approve"
REJECT = "reject"
ACTION_PREFIX = "flasharray_eda_"

# states of an approval, whichever system holds it
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
CANCELLED = "cancelled"
UNKNOWN = "unknown"
FINAL = (APPROVED, REJECTED, EXPIRED, CANCELLED)

# fractions of the decision window at which the channel is reminded; the
# last one mentions the approvers and is shown in the channel
REMINDERS = (0.5, 0.9)
ESCALATE_AT = REMINDERS[-1]
# polls a final-status read may fail before the end is judged from the window
CLOSE_TRIES = 3
PAGE_LIMIT = 100
MAX_PAGES = 50
STATE_FILE = "cards.json"
DEFAULT_NOTIFY_TEMPLATE = "FlashArray - notify"
SLACK_NOTE = re.compile(r"via Slack by (.+?) \(")


def parse_routes(text):
    """SLACK_ROUTES and NOTIFY_ROUTES: "prefix=value,prefix=value" as a list of
    (prefix, value) in the order given; items without both parts are
    dropped."""
    routes = []
    for item in (text or "").split(","):
        prefix, sep, value = item.partition("=")
        if sep and prefix.strip() and value.strip():
            routes.append((prefix.strip(), value.strip()))
    return routes


def route_for(approval, routes):
    """The value of the first route whose prefix starts the approval's
    workflow name or title (case-insensitive); None when none does."""
    for prefix, value in routes:
        for text in (approval.workflow, approval.title):
            if text and text.lower().startswith(prefix.lower()):
                return value
    return None


def parse_time(text):
    """Seconds since the epoch for an ISO 8601 time with Z, an offset or a
    fraction; None for anything else."""
    if not text or not isinstance(text, str):
        return None
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if "." in value:
        head, _, tail = value.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        else:
            rest = ""
        value = f"{head}.{(digits + '000000')[:6]}{rest}"
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _human(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 7200:
        return f"{seconds // 3600} h"
    if seconds >= 120:
        return f"{seconds // 60} min"
    return f"{seconds} s"


class Approval:
    """One pending approval, whichever system holds it."""

    def __init__(
        self,
        system,
        ident,
        title,
        body,
        link="",
        expires="",
        workflow="",
        created="",
    ):
        self.system = system  # "orchestrator" or "controller"
        self.id = str(ident)
        self.title = title
        self.body = body
        self.link = link
        self.expires = expires  # ISO text, the end of the decision window
        self.workflow = workflow
        self.created = created  # ISO text, the start of the window

    @property
    def key(self):
        return f"{self.system}:{self.id}"

    def window(self):
        """(start, end) of the decision window as epoch seconds, or None when
        the system gave no usable times."""
        start, end = parse_time(self.created), parse_time(self.expires)
        if start is None or end is None or end <= start:
            return None
        return start, end

    def to_dict(self):
        return {
            "system": self.system,
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "link": self.link,
            "expires": self.expires,
            "workflow": self.workflow,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data.get("system", ""),
            data.get("id", ""),
            data.get("title", ""),
            data.get("body", ""),
            link=data.get("link", ""),
            expires=data.get("expires", ""),
            workflow=data.get("workflow", ""),
            created=data.get("created", ""),
        )


class Card:
    """A card the bot posted: where it is, what it is about, what has been
    said in its thread, and how many times its end could not be read."""

    def __init__(self, ts, channel, approval, posted_at=0.0, reminded=None, closing=0):
        self.ts = ts
        self.channel = channel
        self.approval = approval
        self.posted_at = posted_at
        self.reminded = list(reminded or [])
        self.closing = int(closing or 0)

    def to_dict(self):
        return {
            "ts": self.ts,
            "channel": self.channel,
            "approval": self.approval.to_dict(),
            "posted_at": self.posted_at,
            "reminded": self.reminded,
            "closing": self.closing,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data.get("ts", ""),
            data.get("channel", ""),
            Approval.from_dict(data.get("approval") or {}),
            posted_at=float(data.get("posted_at") or 0.0),
            reminded=data.get("reminded") or [],
            closing=data.get("closing") or 0,
        )


class CardStore:
    """The posted cards as a JSON file, rewritten whole on every change
    (a temporary file renamed over the old one). A missing or unreadable
    file is an empty record."""

    def __init__(self, directory):
        self.path = pathlib.Path(directory) / STATE_FILE

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        cards = {}
        for key, value in data.items():
            try:
                cards[key] = Card.from_dict(value)
            except (AttributeError, TypeError, ValueError):
                logger.warning("state: card %s is unreadable; dropped", key)
        return cards

    def save(self, cards):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: c.to_dict() for k, c in cards.items()}, indent=1))
        os.replace(tmp, self.path)


def _read(value_env, file_env=None):
    """An environment value, or the contents of the file another variable names."""
    value = os.environ.get(value_env, "")
    if not value and file_env and os.environ.get(file_env):
        value = pathlib.Path(os.environ[file_env]).read_text().strip()
    return value


def _truthy(text, default=True):
    if text is None or text == "":
        return default
    return text.strip().lower() not in ("0", "false", "no", "off")


def http_json(
    url, method="GET", body=None, headers=None, verify=True, form=False, timeout=30
):
    """One HTTP call returning (status, decoded JSON or text). Never logs the
    body or the headers."""
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    data = None
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")[:300]


class Orchestrator:
    """Pending approvals of the automation orchestrator, their final status
    and the decision call (API 1.3.1: an approval carries name, prompt,
    execution_id, created_at, timeout_at, status, decided_by and
    decision_notes; the list pages with limit and cursor)."""

    def __init__(
        self,
        base,
        verify=True,
        client_id="",
        client_secret="",
        username="",
        password="",
        ui_url="",
        http=http_json,
    ):
        self.base = base.rstrip("/")
        self.ui = (ui_url or base).rstrip("/")
        self.verify = verify
        self.client_id, self.client_secret = client_id, client_secret
        self.username, self.password = username, password
        self.http = http
        self.token = ""
        self.token_at = 0.0
        self.workflows = {}  # execution id -> workflow name

    name = "orchestrator"

    def _login(self):
        if self.token and time.time() - self.token_at < 600:
            return
        if self.client_id:
            status, body = self.http(
                self.base + "/api/v1/auth/token",
                "POST",
                {
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                verify=self.verify,
                form=True,
            )
        else:
            status, body = self.http(
                self.base + "/api/v1/auth/login",
                "POST",
                {"username": self.username, "password": self.password},
                verify=self.verify,
            )
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise RuntimeError(f"orchestrator login failed: HTTP {status}")
        self.token, self.token_at = body["access_token"], time.time()

    def _headers(self):
        self._login()
        return {"Authorization": "Bearer " + self.token}

    def _workflow_name(self, execution_id):
        """The workflow an execution runs, read once per execution; the
        execution id's first characters when the read fails."""
        if not execution_id:
            return ""
        if execution_id not in self.workflows:
            status, body = self.http(
                f"{self.base}/api/v1/executions/{execution_id}",
                headers=self._headers(),
                verify=self.verify,
            )
            name = body.get("workflow_name") if isinstance(body, dict) else None
            self.workflows[execution_id] = (
                name if status == 200 and name else execution_id[:8]
            )
            if len(self.workflows) > 1000:  # a long-lived bot forgets old executions
                self.workflows.pop(next(iter(self.workflows)))
        return self.workflows[execution_id]

    def pending(self):
        out = []
        cursor = ""
        for _ in range(MAX_PAGES):
            url = f"{self.base}/api/v1/approvals?status=pending&limit={PAGE_LIMIT}"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(cursor)
            status, body = self.http(url, headers=self._headers(), verify=self.verify)
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError(f"orchestrator approvals: HTTP {status}")
            for a in body.get("resources", []):
                out.append(
                    Approval(
                        "orchestrator",
                        a.get("id"),
                        a.get("name") or a.get("node_name") or "Approval",
                        a.get("prompt") or "",
                        link=f"{self.ui}/approvals",
                        expires=a.get("timeout_at") or a.get("expires_at") or "",
                        workflow=a.get("workflow_name")
                        or self._workflow_name(a.get("execution_id") or ""),
                        created=a.get("created_at") or "",
                    )
                )
            cursor = body.get("next_cursor") or ""
            if not cursor:
                break
        return out

    def final(self, ident):
        """How an approval ended: {status, who, note, at}. PENDING when the
        orchestrator still holds it open, UNKNOWN when it answers a state the
        bot does not know; a read that fails raises."""
        status, body = self.http(
            f"{self.base}/api/v1/approvals/{ident}",
            headers=self._headers(),
            verify=self.verify,
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"orchestrator approval {ident}: HTTP {status}")
        state = str(body.get("status") or "").lower()
        if state == "canceled":
            state = CANCELLED
        who = body.get("decided_by")
        who = who.get("name", "") if isinstance(who, dict) else str(who or "")
        return {
            "status": state if state in FINAL or state == PENDING else UNKNOWN,
            "who": who,
            "note": body.get("decision_notes") or "",
            "at": body.get("decided_at") or "",
        }

    def decide(self, ident, approve, note):
        status, body = self.http(
            f"{self.base}/api/v1/approvals/{ident}",
            "PATCH",
            {
                "status": "approved" if approve else "rejected",
                "decision_notes": note[:500],
            },
            headers=self._headers(),
            verify=self.verify,
        )
        if status >= 300:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise RuntimeError(
                f"orchestrator decision refused: HTTP {status} {str(detail)[:120]}"
            )


class Controller:
    """Pending workflow approvals of automation controller, their final
    status, the decision call and the notify launch."""

    def __init__(
        self,
        base,
        token,
        verify=True,
        http=http_json,
        ui_url="",
        notify_template=DEFAULT_NOTIFY_TEMPLATE,
    ):
        self.base = base.rstrip("/")
        self.token = token
        self.verify = verify
        self.http = http
        self.ui_url = (ui_url or base).rstrip("/")
        self.notify_template = notify_template
        self.template_ids = {}  # template name -> id, looked up once

    name = "controller"

    def _headers(self):
        return {"Authorization": "Bearer " + self.token}

    def pending(self):
        out = []
        url = (
            f"{self.base}/api/controller/v2/workflow_approvals/"
            f"?status=pending&page_size={PAGE_LIMIT}"
        )
        for _ in range(MAX_PAGES):
            status, body = self.http(url, headers=self._headers(), verify=self.verify)
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError(f"controller approvals: HTTP {status}")
            for a in body.get("results", []):
                wj = (a.get("summary_fields") or {}).get("workflow_job") or {}
                out.append(
                    Approval(
                        "controller",
                        a.get("id"),
                        a.get("name") or "Workflow approval",
                        a.get("description") or "",
                        link=(
                            f"{self.ui_url}/execution/jobs/workflow/{wj.get('id')}/output"
                            if wj.get("id")
                            else f"{self.ui_url}/execution/jobs"
                        ),
                        expires=a.get("approval_expiration") or "",
                        workflow=wj.get("name") or "",
                        created=a.get("created") or "",
                    )
                )
            nxt = body.get("next")
            if not nxt:
                break
            url = nxt if str(nxt).startswith("http") else self.base + str(nxt)
        return out

    def final(self, ident):
        """How a workflow approval ended: successful is approved, failed is
        expired when it timed out and rejected otherwise, canceled is
        cancelled, pending is still open; a read that fails raises."""
        status, body = self.http(
            f"{self.base}/api/controller/v2/workflow_approvals/{ident}/",
            headers=self._headers(),
            verify=self.verify,
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"controller approval {ident}: HTTP {status}")
        state = str(body.get("status") or "")
        if state == "successful":
            outcome = APPROVED
        elif state == "failed":
            outcome = EXPIRED if body.get("timed_out") else REJECTED
        elif state == "canceled":
            outcome = CANCELLED
        elif state == "pending":
            outcome = PENDING
        else:
            outcome = UNKNOWN
        who = (body.get("summary_fields") or {}).get("approved_or_denied_by") or {}
        return {
            "status": outcome,
            "who": who.get("username", "") if isinstance(who, dict) else "",
            "note": "",
            "at": body.get("finished") or "",
        }

    def decide(self, ident, approve, note):
        verb = "approve" if approve else "deny"
        status, body = self.http(
            f"{self.base}/api/controller/v2/workflow_approvals/{ident}/{verb}/",
            "POST",
            {},
            headers=self._headers(),
            verify=self.verify,
        )
        if status >= 300:
            raise RuntimeError(
                f"controller {verb} refused: HTTP {status} {str(body)[:120]}"
            )

    def notify(
        self, subject, detail, severity="critical", target_array="", template=None
    ):
        """Launch the notify template (NOTIFY_TEMPLATE, or the one given) with
        one record; the job id. Raises RuntimeError when the template is
        unknown or the launch refused."""
        name = template or self.notify_template
        if not name:
            raise RuntimeError("no notify template configured")
        if name not in self.template_ids:
            status, body = self.http(
                self.base
                + "/api/controller/v2/job_templates/?name="
                + urllib.parse.quote(name),
                headers=self._headers(),
                verify=self.verify,
            )
            results = body.get("results") if isinstance(body, dict) else None
            if status != 200 or not results:
                raise RuntimeError(f"notify template {name!r} not found: HTTP {status}")
            self.template_ids[name] = results[0]["id"]
        status, body = self.http(
            f"{self.base}/api/controller/v2/job_templates/{self.template_ids[name]}/launch/",
            "POST",
            {
                "extra_vars": {
                    "severity": severity,
                    "subject": subject[:200],
                    "detail": detail[:2000],
                    "target_array": target_array,
                }
            },
            headers=self._headers(),
            verify=self.verify,
        )
        if status >= 300 or not isinstance(body, dict):
            raise RuntimeError(
                f"notify launch refused: HTTP {status} {str(body)[:120]}"
            )
        return body.get("id")


def expiry_outcome(approval):
    """What an expiry meant, per system: the orchestrator fails the execution
    at the node; the controller marks the node failed and the workflow takes
    its failure edge."""
    if approval.system == Controller.name:
        return "the approval node timed out and the workflow took its failure edge"
    return "the execution failed at this node and nothing was done"


def _context_line(approval):
    context = [f"*{approval.system}*"]
    if approval.workflow:
        context.append(f"workflow: {approval.workflow}")
    if approval.expires:
        context.append(f"expires: {approval.expires}")
    return " | ".join(context) + (f" | <{approval.link}|open>" if approval.link else "")


def build_card(approval):
    """Slack blocks for a pending approval: title, prompt, context and the two buttons."""
    value = json.dumps({"system": approval.system, "id": approval.id})
    body = approval.body[:2900] or "(no prompt)"
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Approval needed: {approval.title}"[:150],
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _context_line(approval)}],
        },
        {
            "type": "actions",
            "block_id": f"{ACTION_PREFIX}{approval.key}",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_PREFIX + APPROVE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_PREFIX + REJECT,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": value,
                },
            ],
        },
    ]
    return blocks


def decided_card(approval, decision, who, note=""):
    """The card after a decision: the same text, no buttons, the outcome."""
    icon = ":white_check_mark:" if decision == APPROVE else ":no_entry:"
    outcome = "approved" if decision == APPROVE else "rejected"
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{outcome.capitalize()}: {approval.title}"[:150],
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": approval.body[:2900] or "(no prompt)"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{icon} {outcome} by {who}"
                    + (f" via {note}" if note else "")
                    + f" | *{approval.system}*",
                }
            ],
        },
    ]


def closed_card(approval, status, who="", note=""):
    """The card after the approval ended without a click here: expired,
    cancelled, or ended in a way the system did not say."""
    if status == EXPIRED:
        head = f"Expired: {approval.title}"
        line = f":rotating_light: nobody decided within the window; {expiry_outcome(approval)}"
    elif status == CANCELLED:
        head = f"Cancelled: {approval.title}"
        line = ":heavy_multiplication_x: the execution was cancelled"
    else:
        head = f"Decided elsewhere: {approval.title}"
        line = "decided outside Slack or expired"
    if who:
        line += f" ({who})"
    if note and status != EXPIRED:
        line += f": {note[:200]}"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": head[:150]}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": approval.body[:2900] or "(no prompt)"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{line} | *{approval.system}*"}],
        },
    ]


def reminder_text(approval, fraction, remaining, mention=""):
    """The thread reply at a fraction of the window."""
    lead = f"{mention} " if mention and fraction >= ESCALATE_AT else ""
    verb = "Still waiting" if fraction < ESCALATE_AT else "Last call"
    text = (
        f"{lead}{verb}: *{approval.title}* ({approval.system}"
        + (f", {approval.workflow}" if approval.workflow else "")
        + f") has {_human(remaining)} left before it expires."
    )
    if approval.link:
        text += f" <{approval.link}|open>"
    return text


def expiry_text(approval, mention=""):
    """The channel message for an approval nobody decided."""
    lead = f"{mention} " if mention else ""
    return (
        f"{lead}:rotating_light: *Approval expired, nobody decided*: {approval.title}"
        f" ({approval.system}"
        + (f", {approval.workflow}" if approval.workflow else "")
        + f"). {expiry_outcome(approval).capitalize()}."
        + (f" <{approval.link}|open>" if approval.link else "")
    )


def parse_action(payload):
    """(system, id, decision, user_id, user_name, channel, message_ts) from a
    Slack block_actions payload, or None when it is not one of our buttons."""
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None
    for action in payload.get("actions", []):
        action_id = action.get("action_id", "")
        if not action_id.startswith(ACTION_PREFIX):
            continue
        decision = action_id[len(ACTION_PREFIX) :]
        if decision not in (APPROVE, REJECT):
            continue
        try:
            value = json.loads(action.get("value") or "{}")
        except ValueError:
            continue
        user = payload.get("user") or {}
        return (
            value.get("system"),
            str(value.get("id", "")),
            decision,
            user.get("id", ""),
            user.get("username") or user.get("name") or user.get("id", ""),
            (payload.get("channel") or {}).get("id", ""),
            (payload.get("message") or {}).get("ts", ""),
        )
    return None


def allowed(user_id, approvers):
    """Whether this Slack user may decide: anyone when the list is empty."""
    return not approvers or user_id in approvers


class Bot:
    """Poll the systems, post cards for new approvals, remind, carry
    decisions back and close the cards when their approvals end."""

    def __init__(
        self,
        web,
        channel,
        systems,
        approvers=(),
        poll_seconds=30,
        store=None,
        escalate="",
        now=time.time,
        routes=(),
        notify_routes=(),
    ):
        self.web = web
        self.channel = channel
        self.routes = list(routes)  # (workflow or title prefix, channel)
        self.notify_routes = list(notify_routes)  # (prefix, notify template)
        self.systems = {s.name: s for s in systems}
        self.approvers = set(approvers)
        self.poll_seconds = poll_seconds
        self.store = store
        self.escalate = escalate
        self.now = now
        self.posted = store.load() if store else {}  # approval key -> Card
        self.store_error = ""
        self.lock = threading.Lock()
        if self.posted:
            logger.info("state: %d card(s) remembered", len(self.posted))

    def _save(self):
        """Write the record; called with the lock held. A failure is said
        once per streak, not on every change."""
        if self.store is None:
            return
        try:
            self.store.save(self.posted)
        except OSError as exc:
            if str(exc) != self.store_error:
                logger.warning("state: could not write %s: %s", self.store.path, exc)
            self.store_error = str(exc)
            return
        if self.store_error:
            logger.info("state: %s is written again", self.store.path)
        self.store_error = ""

    def _mention(self):
        if self.escalate:
            return self.escalate
        return " ".join(f"<@{u}>" for u in sorted(self.approvers))

    def _controller(self):
        return self.systems.get(Controller.name)

    def _channel_for(self, approval):
        return route_for(approval, self.routes) or self.channel

    def poll_once(self):
        for system in self.systems.values():
            try:
                pending = system.pending()
            except Exception as exc:  # the other system still gets polled
                logger.warning("%s: %s", system.name, exc)
                continue
            seen = set()
            for approval in pending:
                seen.add(approval.key)
                with self.lock:
                    card = self.posted.get(approval.key)
                    if card is None:
                        channel = self._channel_for(approval)
                        try:
                            response = self.web.chat_postMessage(
                                channel=channel,
                                text=f"Approval needed: {approval.title}",
                                blocks=build_card(approval),
                            )
                        except (
                            Exception
                        ) as exc:  # a channel the bot is not in: next poll
                            logger.warning(
                                "could not post %s to %s: %s",
                                approval.key,
                                channel,
                                exc,
                            )
                            continue
                        # chat.update wants the channel id, which the post returns
                        card = Card(
                            response["ts"],
                            response.get("channel") or channel,
                            approval,
                            posted_at=self.now(),
                        )
                        self.posted[approval.key] = card
                        self._save()
                        logger.info("posted %s", approval.key)
                    else:
                        # a window the system gave us later (or a restart) is kept
                        if approval.expires and not card.approval.expires:
                            card.approval.expires = approval.expires
                        if approval.created and not card.approval.created:
                            card.approval.created = approval.created
                self._remind(card)
            # approvals that left the queue: read how they ended
            with self.lock:
                gone = [
                    k
                    for k in self.posted
                    if k.startswith(system.name + ":") and k not in seen
                ]
            for key in gone:
                self._close(system, key)

    def _remind(self, card):
        """One thread reply per poll at most: the latest REMINDERS fraction
        that is due and not yet said; every fraction passed is recorded, so a
        card first seen late says one thing, not two."""
        window = card.approval.window()
        if window is None:
            return
        start, end = window
        now = self.now()
        if now >= end:
            return
        elapsed = (now - start) / (end - start)
        due = [f for f in REMINDERS if f not in card.reminded and elapsed >= f]
        if not due:
            return
        fraction = due[-1]
        escalate = fraction >= ESCALATE_AT
        try:
            self.web.chat_postMessage(
                channel=card.channel,
                thread_ts=card.ts,
                reply_broadcast=escalate,
                text=reminder_text(card.approval, fraction, end - now, self._mention()),
            )
        except Exception as exc:
            logger.warning("reminder for %s failed: %s", card.approval.key, exc)
            return
        with self.lock:
            card.reminded.extend(due)
            self._save()
        logger.info(
            "reminded %s at %d%% of its window", card.approval.key, fraction * 100
        )

    def _close(self, system, key):
        """Rewrite the card of an approval that is no longer in the queue
        after reading how it ended. A read that fails leaves the card for the
        next poll, CLOSE_TRIES times; after that an approval past its window
        is treated as expired, any other as unknown. A detail that still says
        pending (a page boundary, a hiccup in the list) keeps the card as it
        is. The card is forgotten only once the outcome is known, and an
        expired one is paged."""
        with self.lock:
            card = self.posted.get(key)
        if card is None:
            return
        approval = card.approval
        try:
            final = system.final(approval.id)
        except Exception as exc:
            card.closing += 1
            logger.warning(
                "final status of %s (try %d of %d): %s",
                key,
                card.closing,
                CLOSE_TRIES,
                exc,
            )
            if card.closing < CLOSE_TRIES:
                with self.lock:
                    self._save()
                return
            window = approval.window()
            status = EXPIRED if window and self.now() >= window[1] else UNKNOWN
            logger.warning("%s: its end cannot be read; treated as %s", key, status)
            final = {"status": status, "who": "", "note": "", "at": ""}
        if final["status"] == PENDING:
            logger.warning(
                "%s is still pending although the list left it out; kept", key
            )
            return
        status = final["status"]
        if status in (APPROVED, REJECTED):
            decision = APPROVE if status == APPROVED else REJECT
            clicked = SLACK_NOTE.match(final["note"] or "")
            if clicked:
                who, note = clicked.group(1), "Slack"
            else:
                who = final["who"] or f"someone in the {approval.system}"
                note = f"the {approval.system}"
            text = f"{status} {approval.title} by {who}"
            blocks = decided_card(approval, decision, who, note)
        else:
            head = status.capitalize() if status != UNKNOWN else "Decided elsewhere"
            text = f"{head}: {approval.title}"
            blocks = closed_card(approval, status, final["who"], final["note"])
        try:
            self.web.chat_update(
                channel=card.channel, ts=card.ts, text=text, blocks=blocks
            )
        except (
            Exception
        ) as exc:  # the card keeps its buttons; a click is refused by the system
            logger.warning("could not update the card for %s: %s", key, exc)
        with self.lock:
            self.posted.pop(key, None)
            self._save()
        logger.info(
            "%s ended: %s%s", key, status, f" by {final['who']}" if final["who"] else ""
        )
        if status == EXPIRED:
            self._page_expiry(card)

    def _page_expiry(self, card):
        """A critical message in the channel for an approval nobody decided,
        and for an orchestrator approval a critical notify record as well
        (a controller workflow's own failure-edge notify is its record)."""
        approval = card.approval
        try:
            self.web.chat_postMessage(
                channel=card.channel,
                text=expiry_text(approval, self._mention()),
                thread_ts=card.ts,
                reply_broadcast=True,
            )
        except Exception as exc:
            logger.warning("expiry message for %s failed: %s", approval.key, exc)
        if approval.system == Controller.name:
            return
        controller = self._controller()
        template = route_for(approval, self.notify_routes) or (
            controller.notify_template if controller else ""
        )
        if controller is None or not template:
            logger.warning(
                "no notify record for the expired %s: no controller or no template",
                approval.key,
            )
            return
        detail = (
            f"{approval.system} approval '{approval.title}'"
            + (f" of workflow {approval.workflow}" if approval.workflow else "")
            + (f" expired at {approval.expires}" if approval.expires else " expired")
            + f": nobody decided within the window, so {expiry_outcome(approval)}."
            + (f" {approval.link}" if approval.link else "")
        )
        try:
            job = controller.notify(
                f"approval expired, nobody decided: {approval.title}",
                detail,
                template=template,
            )
            logger.info("notify record for %s: job %s", approval.key, job)
        except Exception as exc:
            logger.warning("notify record for %s failed: %s", approval.key, exc)

    def handle_action(self, payload):
        parsed = parse_action(payload)
        if parsed is None:
            return "ignored"
        system_name, ident, decision, user_id, user_name, channel, ts = parsed
        system = self.systems.get(system_name)
        key = f"{system_name}:{ident}"
        if system is None:
            return "unknown system"
        if not allowed(user_id, self.approvers):
            self.web.chat_postEphemeral(
                channel=channel or self.channel,
                user=user_id,
                text="You are not on the approver list for FlashArray approvals.",
            )
            logger.warning("refused %s by %s", key, user_id)
            return "refused"
        with self.lock:
            card = self.posted.get(key)
        approval = (
            card.approval if card else Approval(system_name, ident, "approval", "")
        )
        note = f"via Slack by {user_name} ({user_id})"
        try:
            system.decide(ident, decision == APPROVE, note)
        except Exception as exc:
            self.web.chat_postEphemeral(
                channel=channel or self.channel,
                user=user_id,
                text=f"The {system_name} refused the decision: {exc}",
            )
            logger.warning("decision failed for %s: %s", key, exc)
            return "failed"
        # decided: the card is ours to rewrite, so the poller's sweep must
        # not see it any more
        with self.lock:
            self.posted.pop(key, None)
            self._save()
        try:
            self.web.chat_update(
                channel=channel or (card.channel if card else self.channel),
                ts=ts or (card.ts if card else None),
                text=f"{decision} {approval.title} by {user_name}",
                blocks=decided_card(approval, decision, user_name, "Slack"),
            )
        except Exception as exc:  # the decision stands; the card keeps its buttons
            logger.warning("could not update the card for %s: %s", key, exc)
        logger.info("%s %s by %s", decision, key, user_id)
        return decision

    def run_poller(self, stop):
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                logger.warning("poll: %s", exc)
            stop.wait(self.poll_seconds)


def systems_from_env():
    systems = []
    ao_url = os.environ.get("AO_URL", "")
    if ao_url:
        systems.append(
            Orchestrator(
                ao_url,
                verify=_truthy(os.environ.get("AO_VERIFY_CERTS")),
                client_id=os.environ.get("AO_CLIENT_ID", ""),
                client_secret=_read("AO_CLIENT_SECRET", "AO_CLIENT_SECRET_FILE"),
                username=os.environ.get("AO_USERNAME", ""),
                password=_read("AO_PASSWORD", "AO_PASSWORD_FILE"),
                ui_url=os.environ.get("AO_UI_URL", ""),
            )
        )
    aap = os.environ.get("AAP_HOST", "")
    if aap:
        systems.append(
            Controller(
                aap,
                _read("AAP_TOKEN", "AAP_TOKEN_FILE"),
                verify=_truthy(os.environ.get("AAP_VERIFY_CERTS")),
                ui_url=os.environ.get("AAP_UI_URL", ""),
                notify_template=os.environ.get(
                    "NOTIFY_TEMPLATE", DEFAULT_NOTIFY_TEMPLATE
                ),
            )
        )
    return systems


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="approval_bot", description="Slack approval bot for flasharray-eda"
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("STATE_DIR", ""),
        metavar="DIR",
        help="directory for cards.json, the record of the posted cards "
        "(default: STATE_DIR in the environment; unset keeps it in memory)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    bot_token = _read("SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN_FILE")
    app_token = _read("SLACK_APP_TOKEN", "SLACK_APP_TOKEN_FILE")
    channel = os.environ.get("SLACK_CHANNEL", "")
    if not (bot_token and app_token and channel):
        raise SystemExit(
            "SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_CHANNEL are required"
        )
    systems = systems_from_env()
    if not systems:
        raise SystemExit("set AO_URL and/or AAP_HOST; there is nothing to watch")
    approvers = [
        u.strip() for u in os.environ.get("SLACK_APPROVERS", "").split(",") if u.strip()
    ]
    web = WebClient(token=bot_token)
    bot = Bot(
        web,
        channel if channel.startswith(("#", "C")) else "#" + channel,
        systems,
        approvers,
        int(os.environ.get("POLL_SECONDS", "30")),
        store=CardStore(args.state_dir) if args.state_dir else None,
        escalate=os.environ.get("SLACK_ESCALATE", ""),
        routes=parse_routes(os.environ.get("SLACK_ROUTES", "")),
        notify_routes=parse_routes(os.environ.get("NOTIFY_ROUTES", "")),
    )

    def on_request(client, req):
        if req.type == "interactive":
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
            bot.handle_action(req.payload)

    socket = SocketModeClient(app_token=app_token, web_client=web)
    socket.socket_mode_request_listeners.append(on_request)
    stop = threading.Event()
    poller = threading.Thread(target=bot.run_poller, args=(stop,), daemon=True)
    poller.start()
    socket.connect()
    logger.info(
        "connected; watching %s; approvers %s; state %s; routes %s",
        ", ".join(bot.systems),
        "anyone in the channel" if not approvers else len(approvers),
        args.state_dir or "in memory",
        ", ".join(f"{p} -> {c}" for p, c in bot.routes) or "none",
    )
    try:
        threading.Event().wait()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
