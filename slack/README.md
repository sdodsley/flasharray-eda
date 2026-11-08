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

- **Orchestrator:** a service account with the `approval:decide`
  permission on the project (`AO_CLIENT_ID` and `AO_CLIENT_SECRET`), or a
  user login (`AO_USERNAME` with `AO_PASSWORD_FILE`). The decision is
  recorded as that identity with `via Slack by <user> (<id>)` in the
  decision notes.
- **Controller:** a token (`AAP_TOKEN` or `AAP_TOKEN_FILE`) of a user with
  the approve permission on the workflows. Controller approvals carry no
  notes field; the platform's activity stream records the token's user,
  the bot's log records the Slack user.

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

As a container beside the other listeners, with the secrets mounted as
files rather than carried in the environment (every `*_FILE` variable reads
its value from a file):

    podman run -d --name eda-approval-bot --restart=unless-stopped --network host \
      --env-file ~/.config/slack/flasharray-eda.env \
      -e AO_URL=https://orchestrator.example.com -e AO_USERNAME=admin -e AO_PASSWORD_FILE=/run/secrets/ao-admin \
      -e AAP_HOST=https://aap.example.com -e AAP_TOKEN_FILE=/run/secrets/aap-token \
      -v /path/to/ao-admin-password:/run/secrets/ao-admin:ro,Z \
      -v /path/to/aap-token:/run/secrets/aap-token:ro,Z \
      -v $PWD/slack/approval_bot.py:/app/approval_bot.py:ro,Z \
      registry.access.redhat.com/ubi9/python-312 \
      sh -c 'pip install -q slack_sdk && python /app/approval_bot.py'

`POLL_SECONDS` (default 30) is how often the two approval queues are read.
A card is posted once per approval; when the approval is decided in a UI
or expires, the card loses its buttons on the next poll. Nothing secret is
logged.

## Variables

| Variable | Meaning |
| --- | --- |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL` | the app's tokens and the channel (name or id); each token also as `*_FILE` |
| `SLACK_APPROVERS` | Slack user ids allowed to decide; empty = anyone in the channel |
| `AO_URL`, `AO_UI_URL`, `AO_VERIFY_CERTS` | orchestrator API base, the UI the card links to (default the API base), TLS verification (default true); unset `AO_URL` = not watched |
| `AO_CLIENT_ID` + `AO_CLIENT_SECRET`, or `AO_USERNAME` + `AO_PASSWORD_FILE` | the orchestrator identity |
| `AAP_HOST`, `AAP_TOKEN` or `AAP_TOKEN_FILE`, `AAP_VERIFY_CERTS` | the platform gateway and a controller token; unset `AAP_HOST` = not watched |
| `POLL_SECONDS` | poll interval, default 30 |

`tests/unit/test_approval_bot.py` drives the cards, the decision routing,
the approver list and both systems' API calls with a fake HTTP function and
a fake Slack client; `slack_sdk` is imported only by `main()`.
