# Slack + Jira IT Help Desk Bot

[![tests](https://github.com/RojMacar24/slack-it-chatbot/actions/workflows/tests.yml/badge.svg)](https://github.com/RojMacar24/slack-it-chatbot/actions/workflows/tests.yml)

**A Slack bot that turns IT requests into triaged Jira tickets, answers with first-line troubleshooting, and keeps
each ticket up to date with the Slack conversation until the problem is solved.**

<p align="center">
  <img src="docs/demo.gif" width="732" alt="Demo: a Slack post becomes Jira ticket IT-9 with AI troubleshooting steps. The requester replies, gets a follow-up, presses That fixed it, and the ticket shows as Done in Jira with the conversation copied in as comments.">
</p>
<p align="center"><em>A real run in a lab Slack workspace and Jira Cloud site, with the waiting time sped up.</em></p>

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

## Run it yourself

The bot runs on a laptop with free Slack and Jira accounts; OpenAI is optional. **[SETUP.md](SETUP.md)** walks
through it step by step in about 45 minutes, and lists every setting, how to deploy it and how to troubleshoot it.

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
| `SETUP.md` | Step-by-step setup, every setting, deploying and troubleshooting |

## How it works

**Triage.** With OpenAI configured, one model call returns the ticket type (incident, access request or change
request), category, priority, a short ticket title, and the first reply, all as JSON. Invalid values fall back to the
keyword rules, and so does any API failure. Access and change requests get an acknowledgement instead of troubleshooting.

**Jira labels.** Each ticket gets `slack-it-bot` (or your `JIRA_LABEL`), its type (`incident`, `access-request` or
`change-request`) and `category-<name>`. Escalated tickets also get `escalated`. This makes JQL filters and Jira
automation rules easy, for example `labels = escalated AND statusCategory != Done`.

**Greetings and split posts.** People rarely put a whole problem in one message:

- A post with no details yet, like "Hi team", "quick question" or "I have a problem", gets a reply asking what's
  going on. When that person answers in the thread, the ticket opens there.
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
Done or Fixed. Set `JIRA_DONE_TRANSITION` to override the choice. Once a ticket is closed or escalated, the buttons
come off every message in its thread.

## Tests

```bash
uv pip install -r requirements-dev.txt
uv run pytest
```

The tests use in-memory fakes for Slack, Jira and OpenAI, so they need no accounts or network. One test sends real
Slack event and button payloads through Bolt's router to check the wiring. GitHub Actions runs the same tests and a
`pyflakes` lint on every push to `main` and on every pull request (`.github/workflows/tests.yml`).

## Ideas for extending the lab

- **Jira to Slack updates:** post in the thread when an agent changes status or comments. This needs a Jira webhook,
  or an automation rule that calls a small HTTP endpoint, or polling with JQL.
- **Reporter mapping:** look up the Slack user's email (`users:read.email`) and set them as the Jira reporter.
- **Knowledge base:** search Confluence or a docs folder and include the matching articles in the AI prompt.
- **Jira Service Management:** map incident and request types to JSM request types and SLAs.
- **Another AI provider:** everything model-specific is in `assistant.py`.

See [SECURITY.md](SECURITY.md) for how data and credentials are handled.

## Author and copyright

Built by Rojie M. ([@RojMacar24](https://github.com/RojMacar24)).

© 2026 Rojie M. All rights reserved. No license is granted to copy, modify or redistribute this code. You're welcome to
read it; please ask before reusing any of it.
