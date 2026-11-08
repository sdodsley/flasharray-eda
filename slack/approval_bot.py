#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Slack approval bot for flasharray-eda.

Posts every pending approval of the automation orchestrator and of automation
controller's workflow approval nodes to one Slack channel as a card with
Approve and Reject buttons, and carries the click back as the decision, with
the Slack user's name in the decision notes. Slack Socket Mode: the bot opens
an outbound websocket, so it needs no public endpoint and no firewall change.

Environment
-----------
SLACK_BOT_TOKEN      bot token (xoxb-...) with chat:write; the bot must be a
                     member of the channel
SLACK_APP_TOKEN      app-level token (xapp-...) with connections:write;
                     Socket Mode and Interactivity switched on in the app
SLACK_CHANNEL        channel name or id the cards go to
SLACK_APPROVERS      comma-separated Slack user ids allowed to click; empty
                     lets anyone in the channel decide (a lab setting)
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
                     the workflows
AAP_VERIFY_CERTS     true (default) or false

Every *_FILE variable reads the value from a file, so a container can mount
secrets instead of carrying them in its environment. Nothing secret is ever
logged.

Run
---
    pip install slack_sdk
    python slack/approval_bot.py

or as a container beside the other listeners (slack/README.md). The Slack
SDK is imported only by main(), so the card and decision logic can be unit
tested without it.
"""

import json
import logging
import os
import pathlib
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


class Approval:
    """One pending approval, whichever system holds it."""

    def __init__(self, system, ident, title, body, link="", expires="", workflow=""):
        self.system = system  # "orchestrator" or "controller"
        self.id = str(ident)
        self.title = title
        self.body = body
        self.link = link
        self.expires = expires
        self.workflow = workflow

    @property
    def key(self):
        return f"{self.system}:{self.id}"


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
    """Pending approvals of the automation orchestrator and the decision call."""

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

    def pending(self):
        status, body = self.http(
            self.base + "/api/v1/approvals?status=pending&limit=100",
            headers=self._headers(),
            verify=self.verify,
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"orchestrator approvals: HTTP {status}")
        out = []
        for a in body.get("resources", []):
            out.append(
                Approval(
                    "orchestrator",
                    a.get("id"),
                    a.get("node_name") or a.get("name") or "Approval",
                    a.get("prompt") or "",
                    link=f"{self.ui}/approvals",
                    expires=a.get("expires_at") or a.get("decision_deadline") or "",
                    workflow=a.get("workflow_name")
                    or (a.get("execution_id") or "")[:8],
                )
            )
        return out

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
    """Pending workflow approvals of automation controller and the decision call."""

    def __init__(self, base, token, verify=True, http=http_json, ui_url=""):
        self.base = base.rstrip("/")
        self.token = token
        self.verify = verify
        self.http = http
        self.ui_url = (ui_url or base).rstrip("/")

    name = "controller"

    def _headers(self):
        return {"Authorization": "Bearer " + self.token}

    def pending(self):
        status, body = self.http(
            self.base
            + "/api/controller/v2/workflow_approvals/?status=pending&page_size=100",
            headers=self._headers(),
            verify=self.verify,
        )
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"controller approvals: HTTP {status}")
        out = []
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
                )
            )
        return out

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


def build_card(approval):
    """Slack blocks for a pending approval: title, prompt, context and the two buttons."""
    value = json.dumps({"system": approval.system, "id": approval.id})
    context = [f"*{approval.system}*"]
    if approval.workflow:
        context.append(f"workflow: {approval.workflow}")
    if approval.expires:
        context.append(f"expires: {approval.expires}")
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
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": " | ".join(context)
                    + (f" | <{approval.link}|open>" if approval.link else ""),
                }
            ],
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
    """Poll the systems, post cards for new approvals, carry decisions back."""

    def __init__(self, web, channel, systems, approvers=(), poll_seconds=30):
        self.web = web
        self.channel = channel
        self.systems = {s.name: s for s in systems}
        self.approvers = set(approvers)
        self.poll_seconds = poll_seconds
        self.posted = {}  # approval key -> (message ts, Approval)
        self.lock = threading.Lock()

    def poll_once(self):
        seen = set()
        for system in self.systems.values():
            try:
                pending = system.pending()
            except Exception as exc:  # the other system still gets polled
                logger.warning("%s: %s", system.name, exc)
                continue
            for approval in pending:
                seen.add(approval.key)
                with self.lock:
                    if approval.key in self.posted:
                        continue
                    response = self.web.chat_postMessage(
                        channel=self.channel,
                        text=f"Approval needed: {approval.title}",
                        blocks=build_card(approval),
                    )
                    # chat.update wants the channel id, which the post returns
                    self.posted[approval.key] = (
                        response["ts"],
                        approval,
                        response.get("channel") or self.channel,
                    )
                    logger.info("posted %s", approval.key)
            # approvals decided elsewhere (the UI, a timeout) lose their buttons
            with self.lock:
                stale = [
                    k
                    for k in self.posted
                    if k.startswith(system.name + ":") and k not in seen
                ]
                for key in stale:
                    ts, approval, channel = self.posted.pop(key)
                    try:
                        self.web.chat_update(
                            channel=channel,
                            ts=ts,
                            text=f"Decided elsewhere: {approval.title}",
                            blocks=[
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": f"*{approval.title}* ({approval.system}) was decided outside Slack or expired.",
                                    },
                                },
                            ],
                        )
                    except (
                        Exception
                    ) as exc:  # the card keeps its buttons; a click is refused by the system
                        logger.warning("could not update the card for %s: %s", key, exc)

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
            entry = self.posted.get(key)
        approval = entry[1] if entry else Approval(system_name, ident, "approval", "")
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
        self.web.chat_update(
            channel=channel or (entry[2] if entry else self.channel),
            ts=ts or (entry[0] if entry else None),
            text=f"{decision} {approval.title} by {user_name}",
            blocks=decided_card(approval, decision, user_name, "Slack"),
        )
        with self.lock:
            self.posted.pop(key, None)
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
            )
        )
    return systems


def main():
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
        "connected; watching %s; approvers %s",
        ", ".join(bot.systems),
        "anyone in the channel" if not approvers else len(approvers),
    )
    try:
        threading.Event().wait()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
