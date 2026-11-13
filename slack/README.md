# Slack approval bot

`approval_bot.py` posts every pending approval of the automation
orchestrator and of automation controller's workflow approval nodes into
one Slack channel as a card with Approve and Reject buttons, and carries
the click back as the decision, with the Slack user's name in the decision
notes. It uses Slack Socket Mode: an outbound websocket from wherever it
runs, so it needs no public endpoint and no firewall change. The cards
complement the notifications `playbooks/notify.yml` posts through the
incoming webhook and the platform's own Slack notification template
(README.md, "Notifications"); neither of those can take a decision.

A card follows its approval to the end. While the approval waits, the bot
reminds the channel in the card's thread at half and at ninety percent of
the decision window (the second reminder mentions the approvers, or
`SLACK_ESCALATE`, and is shown in the channel). When the approval leaves
the queue the bot reads how it ended and rewrites the card: approved or
rejected and by whom, whether from here or in a UI; cancelled; or expired.
An expired orchestrator approval fails its execution with nothing done and
nobody told (orchestrator/README.md), so the bot also posts a critical
message in the channel, with the mentions, and launches the controller's
notify template for a critical record in the alert log, the way every
other critical finding is recorded; a timed-out controller approval sends
its workflow down the failure edge, whose own notify node is the record,
so it gets the channel message only. A read of the final status that fails
is retried on the next polls, and an approval still unreadable once its
window has passed is treated as expired rather than forgotten; one the
system still reports pending (left out of a list page) keeps its card. The
posted cards are kept in `STATE_DIR`, so a restart edits the cards it
posted instead of posting them again and still closes them when their
approvals end.

## The Slack app

One app in the workspace, created at api.slack.com/apps:

- **Bot token** (`xoxb-...`): OAuth & Permissions, bot token scope
  `chat:write`; install the app and invite the bot to the channel
  (`/invite @<bot name>`), because a bot cannot post into a channel it is
  not a member of.
- **App-level token** (`xapp-...`): Settings, Socket Mode, enabled, with
  the `connections:write` scope.
- Interactivity & Shortcuts switched on (no request URL with Socket Mode).

## Identities

- **Orchestrator:** a user login (`AO_USERNAME` with `AO_PASSWORD_FILE`)
  for a user who is in a group the approval nodes name (`approvers` in the
  shipped definitions) and holds the `project-user` role on the project. A
  service account (`AO_CLIENT_ID` and `AO_CLIENT_SECRET`) can list the
  approvals but cannot decide them: the orchestrator accepts users and
  groups as approvers, not service accounts. The decision is recorded as
  that identity with `via Slack by <user> (<id>)` in the decision notes.
- **Controller:** a token (`AAP_TOKEN` or `AAP_TOKEN_FILE`) of a user with
  the approve permission on the workflows and, for the record of an
  expired approval, the execute permission on `NOTIFY_TEMPLATE`
  (`FlashArray - notify`). Controller approvals carry no notes field; the
  platform's activity stream records the token's user, the bot's log
  records the Slack user.

Who may click: `SLACK_APPROVERS`, a comma-separated list of Slack user
ids. Empty means anyone in the channel, which is a lab setting; set it
anywhere that matters. A refused click gets an ephemeral reply and nothing
is decided.

## Run

Standalone:

    pip install slack_sdk
    set -a; . ~/.config/slack/flasharray-eda.env; set +a
    AO_URL=https://orchestrator.example.com AO_USERNAME=admin AO_PASSWORD_FILE=/path/to/password \
    AAP_HOST=https://aap.example.com AAP_TOKEN_FILE=/path/to/token \
    python slack/approval_bot.py

As a container beside the other listeners, with the two passwords handed
over as podman secrets rather than carried in the environment (every
`*_FILE` variable reads its value from a file; a secret is mounted
readable under `/run/secrets/<name>`, where a 600-mode file bind-mounted
from the host would be unreadable to the image's non-root user under
rootless uid mapping):

    podman secret create ao-admin /path/to/ao-admin-password
    podman secret create aap-token /path/to/aap-token
    mkdir -p ~/.config/slack/bot-state
    podman run -d --name eda-approval-bot --restart=unless-stopped --network host \
      --env-file ~/.config/slack/flasharray-eda.env \
      -e AO_URL=https://orchestrator.example.com -e AO_USERNAME=admin -e AO_PASSWORD_FILE=/run/secrets/ao-admin \
      -e AAP_HOST=https://aap.example.com -e AAP_TOKEN_FILE=/run/secrets/aap-token \
      -e STATE_DIR=/state -v ~/.config/slack/bot-state:/state:Z,U \
      --secret ao-admin --secret aap-token \
      -v $PWD/slack/approval_bot.py:/app/approval_bot.py:ro,Z \
      registry.access.redhat.com/ubi9/python-312 \
      sh -c 'pip install -q slack_sdk && python /app/approval_bot.py'

`POLL_SECONDS` (default 30) is how often the two approval queues are read.
A card is posted once per approval. The cards the bot posted are written
to `cards.json` under `STATE_DIR` (`--state-dir` on the command line)
after every change, so a restart picks them up (the `U` on the volume
makes the directory writable by the image's non-root user under rootless
podman, after which the host user reads it with `podman unshare cat`;
without it every write fails and the bot says so once and runs on in
memory): an approval still pending
keeps its card, one that ended while the bot was down gets its card
rewritten with the outcome on the first poll. Without a state directory
the record lives in memory and a restart posts a fresh card for every
approval still pending, while the earlier card keeps its buttons (a click
on either decides the same approval). The decision window comes from the
approval itself (`timeout_at` in the orchestrator, `approval_expiration`
in the controller; a controller node with no timeout has no window and no
reminders). Nothing secret is logged.

Verified against a workspace: the bot connects over Socket Mode, posts the
card for a pending orchestrator approval within one poll, and a click on
Approve decides it (the orchestrator records the approval under the bot's
identity with the Slack user in the decision notes) and the execution
resumes. A pending controller approval is posted and decided the same way
(the controller-tier workflow launched by hand, approved from the channel,
recorded as the bot's platform user). Deployed, the bot holds a service
account with the project-user role and a platform user with the Approve
role on the workflow, nothing wider; a second click on an approval already
decided is refused by the platform and reported back as an ephemeral
message.
`SLACK_CHANNEL` may be a name: Slack accepts it for the post and the bot
keeps the channel id the post returns for the card's later updates.

Two projects on one Slack app share one bot. Socket Mode hands each button
click to one of the app's open connections, so a second bot instance on
the same app would lose the clicks meant for the other; instead the one
instance polls both systems (its identities need the approve rights on
both projects' workflows and execute on both notify templates) and
`SLACK_ROUTES` sends each project's cards to its own channel by the prefix
of the workflow name, for example `FlashBlade=#flashblade-eda`, with
`NOTIFY_ROUTES` naming that project's notify template for the expiry
record. A second Slack app, with its own tokens and bot instance, is the
other way to keep the projects apart.
The reminders, the expiry handling and the state were exercised against
the lab orchestrator with a throwaway four-minute approval nobody decided:
the card, the two reminders in its thread, the rewrite as expired, the
critical message and the critical notify record, with the bot restarted
mid-window and the card kept; and against the controller with an approval
denied by API, which the bot read and rewrote as rejected by that user
([docs/VERIFICATION.md](../docs/VERIFICATION.md)).

## Variables

| Variable | Meaning |
| --- | --- |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL` | the app's tokens and the channel (name or id); each token also as `*_FILE` |
| `SLACK_APPROVERS` | Slack user ids allowed to decide; empty = anyone in the channel; also who the last reminder and the expiry message mention |
| `SLACK_ESCALATE` | text put in front of the last reminder and the expiry message instead of the approver mentions, for example `<!subteam^S0123>` or `<!here>` |
| `STATE_DIR` | directory for `cards.json`, the record of the posted cards; unset keeps it in memory |
| `NOTIFY_TEMPLATE` | the controller template that writes the critical record of an expired orchestrator approval (default `FlashArray - notify`; empty = no record) |
| `SLACK_ROUTES` | `prefix=channel,prefix=channel`: an approval whose workflow name or title starts with a prefix is posted to that channel instead of `SLACK_CHANNEL`, and its reminders and expiry message follow it |
| `NOTIFY_ROUTES` | `prefix=template`: the notify template for the expiry record of approvals matching the prefix (default `NOTIFY_TEMPLATE`) |
| `AO_URL`, `AO_UI_URL`, `AO_VERIFY_CERTS` | orchestrator API base, the UI the card links to (default the API base), TLS verification (default true); unset `AO_URL` = not watched |
| `AO_CLIENT_ID` + `AO_CLIENT_SECRET`, or `AO_USERNAME` + `AO_PASSWORD_FILE` | the orchestrator identity |
| `AAP_HOST`, `AAP_TOKEN` or `AAP_TOKEN_FILE`, `AAP_VERIFY_CERTS` | the platform gateway and a controller token; unset `AAP_HOST` = not watched |
| `AAP_UI_URL` | the platform address the card links to (default `AAP_HOST`); the platform's own notification links come from its `TOWER_URL_BASE` setting, which the seed sets from `aap_url_base` |
| `POLL_SECONDS` | poll interval, default 30 |

`tests/unit/test_approval_bot.py` drives the cards, the decision routing,
the approver list, both systems' API calls and final-status reads, the
reminders against a fake clock, the state file across a restart and the
expiry page and record, with a fake HTTP function and a fake Slack client;
`slack_sdk` is imported only by `main()`.
