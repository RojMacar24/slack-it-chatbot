# Slack + Jira IT Help Desk Bot

[![tests](https://github.com/Roj-D0nut/slack-it-chatbot/actions/workflows/tests.yml/badge.svg)](https://github.com/Roj-D0nut/slack-it-chatbot/actions/workflows/tests.yml)

**A Slack bot that turns IT requests into triaged Jira tickets, answers with first-line troubleshooting, and keeps
each ticket up to date with the Slack conversation until the problem is solved.**

## Highlights

- **End-to-end automation.** A Slack post becomes a typed, prioritised, labelled Jira ticket within seconds. Thread
  replies become Jira comments, and buttons in Slack resolve or escalate the ticket in Jira.
- **AI with guardrails.** OpenAI returns structured triage that the bot validates, with a keyword-rule fallback, so
  tickets still flow when there's no API key or the API fails. The AI steps back as soon as someone from IT joins.
- **Built for real conversations.** Greetings get asked for details, split messages merge into one ticket, replies
  sent while a ticket is being created aren't lost, and two quick replies get one answer.
- **Secure by default.** Passwords and tokens are masked before anything reaches Jira or OpenAI, AI output is escaped
  so it can't ping a whole channel, and the Slack app asks only for the permissions it uses.
- **Simple to run, thoroughly tested.** No database (Jira is the source of truth) and no public URL (Slack Socket
  Mode). Over 100 automated tests with fake Slack, Jira and OpenAI clients run on every pull request.

**Tech:** Python 3.12 · Slack Bolt (Socket Mode, Block Kit) · Jira REST API (Cloud and Data Center) · OpenAI API ·
APScheduler · pytest · GitHub Actions

## What it does

A self-contained lab project that automates first-line IT support between Slack and Jira:

- New posts in your IT channel become **Jira tickets**, each typed, categorised, prioritised and labelled. A greeting
  gets asked for details first, and quick follow-up posts join the same ticket.
- The bot replies in the Slack thread with the ticket link and, if OpenAI is configured, **first troubleshooting steps**.
- Replies in the thread are **copied into Jira as comments**, so the ticket holds the whole conversation.
- The requester can press **✅ That fixed it**, which closes the Jira ticket, or **🆘 Escalate to IT**, which labels it and pings your IT group.
- The AI stops replying as soon as someone from IT joins the thread, the ticket is escalated or closed, or it has run out of attempts.
- A **weekly report**, built from Jira data, is posted to the channel. You can also get one any time with `@IT Help Desk report`.

It runs over Slack **Socket Mode**, so it needs no public URL, web server or database. You can run it on a laptop.

```text
 Slack #it-help                      bot.py                          Jira
 ──────────────                      ──────                          ────
 "VPN keeps dropping"  ──event──▶  triage (AI or keywords)  ──────▶  create IT-42
                                                                     (labels, priority)
 thread: 🎫 IT-42 + steps  ◀──────  post ticket + first reply  ────▶  comment: AI reply
 "still failing"       ──event──▶  AI follow-up             ──────▶  comment: both messages
 [✅ That fixed it]     ──button─▶  transition to Done       ──────▶  IT-42 → Done
 [🆘 Escalate to IT]    ──button─▶  label "escalated"        ──────▶  IT-42 + label, comment
```

## Project layout

| File | What it does |
|---|---|
| `bot.py` | Entry point. Slack event and button handlers, ticket lifecycle, weekly schedule |
| `jira_client.py` | Small Jira REST client that works with both Jira Cloud and Data Center |
| `assistant.py` | Triage and troubleshooting with OpenAI, falling back to keyword rules |
| `tickets.py` | Slack message layout for tickets, and how the bot recognises its threads |
| `reports.py` | Weekly summary built from Jira search results |
| `text_utils.py` | Converts Slack and Markdown text, and redacts secrets |
| `config.py` | Reads and validates settings from the environment or `.env` |
| `it_environment.md` | Describes your lab's tools. Sent to the AI so its advice fits |
| `slack-app-manifest.yml` | Creates the Slack app with the right scopes in one step |
| `tests/` | Offline tests with fake Slack, Jira and OpenAI clients |

## Setup

You need Python 3.11+ (3.12 recommended), a Slack workspace where you can install apps, and a Jira site.
Free tiers of both are fine.

### 1. Jira

1. Create a free Jira Cloud site at <https://www.atlassian.com/software/jira/free> if you don't have one.
2. Create a project for IT tickets. Note its **key**, for example `IT`. A company-managed software project works
   out of the box. For a Jira Service Management project, set `JIRA_ISSUE_TYPE` and `JIRA_REQUEST_ISSUE_TYPE`
   to its issue type names.
3. Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens>.

On **Jira Data Center**, create a personal access token instead and leave `JIRA_EMAIL` empty.

### 2. Slack

1. Go to <https://api.slack.com/apps>, choose **Create New App**, then **From a manifest**. Pick your workspace and
   paste in `slack-app-manifest.yml`.
2. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-…`) from *OAuth & Permissions*.
3. Under *Basic Information*, then *App-Level Tokens*, create a token with the `connections:write` scope and copy it (`xapp-…`).
4. Create your IT channel (for example `#it-help`) and invite the bot: `/invite @IT Help Desk`.

### 3. OpenAI (optional)

Create an API key at <https://platform.openai.com/api-keys>. Without one, the bot still opens, labels, comments on,
closes and escalates tickets. It just uses keyword rules for triage and doesn't post troubleshooting replies.

### 4. Configure

```bash
cp .env.example .env
```

Fill in `.env`. Everything is explained in `.env.example`. Then edit `it_environment.md` to describe your lab's tools.

### 5. Run

With [uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -r requirements.txt
uv run python bot.py
```

Or with plain pip:

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

On startup the bot checks the Jira credentials and project, looks up the channel, and warns you if it hasn't been
invited yet. Then post something like *"My VPN keeps disconnecting"* in the channel.

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | yes | | `xoxb-…` |
| `SLACK_APP_TOKEN` | yes | | `xapp-…`, needs `connections:write` |
| `IT_CHANNEL` | yes | | Channel name, or ID for private channels |
| `JIRA_BASE_URL` | yes | | e.g. `https://your-site.atlassian.net` |
| `JIRA_EMAIL` | Cloud only | | Leave empty for Data Center |
| `JIRA_API_TOKEN` | yes | | Cloud API token or Data Center PAT |
| `JIRA_PROJECT_KEY` | yes | | e.g. `IT` |
| `JIRA_ISSUE_TYPE` | | `Task` | Issue type for incidents |
| `JIRA_REQUEST_ISSUE_TYPE` | | same as above | Issue type for access and change requests |
| `JIRA_LABEL` | | `slack-it-bot` | Added to every ticket, and used by the report |
| `JIRA_SET_PRIORITY` | | `true` | The bot retries without a priority if Jira rejects it |
| `JIRA_DONE_TRANSITION` | | | Exact transition name for "That fixed it", if the automatic choice is wrong |
| `OPENAI_API_KEY` | | | Turns on AI triage and replies |
| `OPENAI_MODEL` | | `gpt-4o-mini` | Any chat model that supports JSON mode |
| `IT_ENVIRONMENT_FILE` | | `it_environment.md` | Context for the AI |
| `ESCALATION_MENTION` | | "The IT team" | `<@U…>` or `<!subteam^S…>` to ping on escalation |
| `MAX_AI_FOLLOW_UPS` | | `3` | AI replies per ticket after the first answer |
| `MERGE_WINDOW_SECONDS` | | `120` | Extra posts from the same person within this time join their last ticket. `0` turns it off |
| `REPORT_ENABLED` / `REPORT_DAY` / `REPORT_HOUR` / `REPORT_TIMEZONE` | | `true` / `mon` / `9` / `UTC` | Weekly report schedule |

## How it works

**Triage.** With OpenAI configured, one model call returns the ticket type (incident, access request or change
request), category, priority, a short ticket title, and the first reply, all as JSON. Invalid values fall back to the
keyword rules, and so does any API failure. Access and change requests get an acknowledgement instead of troubleshooting.

**Jira labels.** Each ticket gets `slack-it-bot` (or your `JIRA_LABEL`), its type (`incident`, `access-request` or
`change-request`) and `category-<name>`. Escalated tickets also get `escalated`. This makes JQL filters and Jira
automation rules easy, for example `labels = escalated AND statusCategory != Done`.

**Greetings and split posts.** People rarely put a whole problem in one message:

- A post with no details yet, like "Hi team" or "quick question", gets a reply asking what's going on. When that
  person answers in the thread, the ticket opens there.
- If the same person posts again within `MERGE_WINDOW_SECONDS` (2 minutes by default), the new post joins their
  last ticket instead of opening another. It's added to Jira as a comment, the new post gets a link back to the
  ticket thread, and the AI answers there with the new detail in mind. Replies under the extra post are copied to
  the ticket too.

**Timing.** A reply sent while its ticket is still being created waits for it instead of being dropped. Replies in the
same thread are handled one at a time. If someone sends two messages in quick succession, the AI answers once,
covering both.

**No database.** The bot recognises its tickets from its own thread message, which starts with
"Ticket IT-42 created". The requester is whoever started the thread. Jira is the source of truth for status and
labels, so closing or labelling a ticket directly in Jira also stops the AI. The only things kept in memory are
short-lived: tickets still being created, and each person's latest ticket for the merge window. A restart forgets
those and nothing else.

**When the AI stays quiet.** It only replies to the person who opened the ticket, and only on incidents. It stops when:

- anyone else (such as IT staff) replies in the thread
- the ticket is escalated or closed
- it has used up `MAX_AI_FOLLOW_UPS`, after which it posts one final suggestion to escalate

Everyone's replies are still copied to Jira.

**Buttons.** Only the requester can use them. Anyone else gets a private note saying so. To close a ticket, the bot
picks a transition into a Done-category status, preferring names like Done, Resolve or Close. It never uses
cancel-style transitions ("Cancel", "Won't do", "Duplicate"). If the transition asks for a resolution, it fills in
Done or Fixed. Set `JIRA_DONE_TRANSITION` to override the choice.

## Tests

```bash
uv pip install -r requirements-dev.txt
uv run pytest
```

The tests use in-memory fakes for Slack, Jira and OpenAI, so they need no accounts or network. One test sends real
Slack event and button payloads through Bolt's router to check the wiring. GitHub Actions runs the same tests and a
`pyflakes` lint on every push to `main` and on every pull request (`.github/workflows/tests.yml`).

## Deploying

Socket Mode only needs a long-running process that can make outbound connections. It needs no inbound port. The
`Procfile` (`worker: python bot.py`) works on hosts such as Railway, Render or Heroku. Set the same environment
variables there instead of using a `.env` file.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot never reacts | It isn't in the channel (`/invite`), or the app is missing the `message.channels` event |
| `Couldn't reach Jira project` at startup | Wrong URL, email or token, or the account can't see the project |
| `issuetype: … invalid` | `JIRA_ISSUE_TYPE` doesn't exist in the project. Check the names under *Project settings*, then *Issue types* |
| `labels` error when creating | The Labels field isn't on the project's create screen |
| "couldn't find a way to close" | The workflow has no non-cancel transition to a Done status from the current one. Set `JIRA_DONE_TRANSITION` to the transition's exact name |
| Buttons do nothing | Interactivity is off in the Slack app settings (the manifest turns it on) |
| Private channel not found | Use the channel ID in `IT_CHANNEL`, add `groups:history` and `groups:read`, and subscribe to `message.groups` |

## Ideas for extending the lab

- **Jira to Slack updates:** post in the thread when an agent changes status or comments. This needs a Jira webhook,
  or an automation rule that calls a small HTTP endpoint, or polling with JQL.
- **Reporter mapping:** look up the Slack user's email (`users:read.email`) and set them as the Jira reporter.
- **Knowledge base:** search Confluence or a docs folder and include the matching articles in the AI prompt.
- **Jira Service Management:** map incident and request types to JSM request types and SLAs.
- **Another AI provider:** everything model-specific is in `assistant.py`.

See [SECURITY.md](SECURITY.md) for how data and credentials are handled.

## Author and copyright

Built by Rojie M. ([@Roj-D0nut](https://github.com/Roj-D0nut)).

© 2026 Rojie M. All rights reserved. No license is granted to copy, modify or redistribute this code. You're welcome to
read it; please ask before reusing any of it.
