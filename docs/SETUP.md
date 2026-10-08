# Setup guide

How to run the Slack + Jira IT Help Desk Bot in your own lab. For what the bot does and how it works, see the
[README](../README.md).

**You'll need** about 45 minutes, Python 3.11 or newer (3.12 recommended), and free accounts for Slack and Jira. An
OpenAI API key is optional.

> 🔒 Your tokens and keys go only in the `.env` file, which git ignores. Never commit them or paste them into chat,
> tickets or screenshots.

## 1. Slack

1. **Create a workspace** to practise in at <https://slack.com/get-started>, then make a channel for IT requests,
   for example `#it-help`.
2. **Create the app.** Go to <https://api.slack.com/apps>, click **Create New App**, then **From a manifest**, and
   pick your workspace. Paste the contents of [`slack-app-manifest.yml`](slack-app-manifest.yml) into the **YAML**
   tab, then click **Next** and **Create**.
   - If every line is underlined in red, the first line (`display_information:`) probably didn't get copied. Add it
     back, with the lines under it indented by two spaces, or paste the same settings into the **JSON** tab instead.
   - A yellow note saying Socket Mode needs more setup is expected. Step 4 below covers it.
3. **Install it.** Under **Install App** (or **OAuth & Permissions**), click **Install to Workspace**, then **Allow**,
   and copy the **Bot User OAuth Token** (`xoxb-…`). Leave the **Opt In** buttons for token rotation and PKCE alone:
   with token rotation on, the token would expire.
4. **Create the app-level token.** Under **Basic Information**, then **App-Level Tokens**, click **Generate Token and
   Scopes**. Name it `socket`, add the `connections:write` scope, click **Generate** and copy the token (`xapp-…`).
5. **Invite the bot** into your IT channel: `/invite @IT Help Desk`.

## 2. Jira

1. **Create a site.** Sign up for free at <https://www.atlassian.com/software/jira/free>. Your site address looks
   like `https://your-site.atlassian.net`.
2. **Create a project.** Go to **Projects**, then **Create project**, choose the **Kanban** template and pick
   **Company-managed**. Name it, for example, *IT Help Desk*, with the key **`IT`**.
   - For a Jira Service Management project, set `JIRA_ISSUE_TYPE` and `JIRA_REQUEST_ISSUE_TYPE` to its issue type
     names.
3. **Create an API token** at <https://id.atlassian.com/manage-profile/security/api-tokens>. Click **Create API
   token**, the plain option, not "with scopes", and copy it.

On **Jira Data Center**, create a personal access token instead and leave `JIRA_EMAIL` empty.

## 3. OpenAI (optional)

Create an API key at <https://platform.openai.com/api-keys> and add a few dollars of credit. Each ticket costs well
under a cent with the default model. Without a key, the bot still opens, labels, comments on, closes and escalates
tickets. It just uses keyword rules for triage and doesn't post troubleshooting replies.

## 4. Configure

Copy the template, then fill it in. Every setting is explained in [`.env.example`](../.env.example).

```powershell
Copy-Item .env.example .env      # macOS/Linux: cp .env.example .env
notepad .env                     # or any text editor
```

The values go straight after the `=`, with no spaces or quote marks. At minimum, set:

```dotenv
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
IT_CHANNEL=it-help
JIRA_BASE_URL=https://your-site.atlassian.net
JIRA_EMAIL=the email you use for Atlassian
JIRA_API_TOKEN=…
JIRA_PROJECT_KEY=IT
```

Then edit [`docs/it_environment.md`](it_environment.md) to describe your lab's tools, so the AI's advice fits them.

## 5. Run

With [uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -r requirements.txt
uv run python -m helpdesk
```

Or with plain pip:

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m helpdesk
```

On startup the bot checks the Jira credentials and project, finds the channel, and warns you if it hasn't been
invited yet. A setting that's missing or wrong stops it with a one-line explanation.

## 6. Try it

In your IT channel:

| Try this | You should see |
|---|---|
| Post *"My VPN keeps disconnecting"* | A thread reply with a ticket link, troubleshooting steps and two buttons. The Jira ticket has labels and a priority |
| Reply *"Still failing"* in that thread | The reply copied into Jira as a comment, and a follow-up from the AI |
| Post *"Hi team"*, then reply with the details in its thread | A request for details first, then the ticket opening in that same thread |
| Post twice, about 10 seconds apart | One ticket. The second post gets "Added to ticket…" with a link back |
| Press **✅ That fixed it** | The Jira ticket moves to Done and the buttons disappear |
| Press **🆘 Escalate to IT** on another ticket | The ticket gets the `escalated` label and the AI stops replying |
| In Jira, move an open ticket to *In Progress*, then *Done* | Within a minute, the Slack thread says who moved it, and the buttons disappear once it's done |
| Post `@IT Help Desk report` | A summary of the last 7 days |

## Settings

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | yes | | `xoxb-…` |
| `SLACK_APP_TOKEN` | yes | | `xapp-…`, needs `connections:write` |
| `IT_CHANNEL` | yes | | Channel name, or ID for private channels |
| `JIRA_BASE_URL` | yes | | e.g. `https://your-site.atlassian.net`. Must be `https://`, except for `localhost` |
| `JIRA_EMAIL` | Cloud only | | Leave empty for Data Center |
| `JIRA_API_TOKEN` | yes | | Cloud API token or Data Center PAT |
| `JIRA_PROJECT_KEY` | yes | | e.g. `IT` |
| `JIRA_ISSUE_TYPE` | | `Task` | Issue type for incidents |
| `JIRA_REQUEST_ISSUE_TYPE` | | same as above | Issue type for access and change requests |
| `JIRA_LABEL` | | `slack-it-bot` | Added to every ticket, and used by the report |
| `JIRA_SET_PRIORITY` | | `true` | The bot retries without a priority if Jira rejects it |
| `JIRA_SET_REPORTER` | | `true` | Make the requester the Jira reporter when possible. See below |
| `JIRA_DONE_TRANSITION` | | | Exact transition name for "That fixed it", if the automatic choice is wrong |
| `OPENAI_API_KEY` | | | Turns on AI triage and replies |
| `OPENAI_MODEL` | | `gpt-6-luna` | Any chat model that supports JSON mode |
| `AI_ALLOWED_LINK_DOMAINS` | | | Comma-separated. If set, links in AI replies to other domains are removed |
| `IT_ENVIRONMENT_FILE` | | `docs/it_environment.md` | Context for the AI |
| `ESCALATION_MENTION` | | "The IT team" | `<@U…>` or `<!subteam^S…>` to ping on escalation |
| `IT_STAFF` | | | Comma-separated member IDs (`U…`) and user group IDs (`S…`) of IT staff. Only their replies stop the AI. If empty, anyone but the requester does. See below |
| `MAX_AI_FOLLOW_UPS` | | `3` | AI replies per ticket after the first answer |
| `MERGE_WINDOW_SECONDS` | | `120` | Extra posts from the same person within this time join their last ticket. `0` turns it off |
| `JIRA_SYNC_SECONDS` | | `60` | How often to check Jira for status changes to post in Slack. `0` turns it off; otherwise at least `10` |
| `MAX_TICKETS_PER_HOUR` | | `10` | Tickets one person can open per hour. Past that, the bot asks them to use an open ticket. `0` turns the limit off |
| `REPORT_ENABLED` / `REPORT_DAY` / `REPORT_HOUR` / `REPORT_TIMEZONE` | | `true` / `mon` / `9` / `UTC` | Weekly report schedule |

### Telling IT staff apart from coworkers

When someone other than the requester replies in a ticket thread, the AI steps back so it doesn't talk over IT. By
default that includes a coworker writing "+1, same here". To make only IT staff count, list them in `IT_STAFF`:

- **People:** in Slack, open someone's profile, click **⋮** (More), then **Copy member ID**. It looks like `U0123ABCD`.
- **A user group** such as `@it-team` (paid Slack plans only): copy its ID, which starts with `S`, from the group's
  page. Then add the `usergroups:read` scope under **OAuth & Permissions** and reinstall the app. The bot re-reads
  group members every 5 minutes.

```dotenv
IT_STAFF=U0123ABCD, U0456EFGH
```

Everyone's replies are still copied to Jira either way.

### Making the requester the Jira reporter

By default every ticket is reported by the bot's Jira account, and the description names who asked. The bot makes
the requester the reporter instead when all of these are true:

1. **The Slack app can read emails.** The manifest includes `users:read.email`. If you created the app before that
   was added, add the scope under **OAuth & Permissions** and reinstall the app.
2. **The bot's Jira account may set the reporter** (Jira's *Modify Reporter* permission). In a company-managed
   project, grant it in the permission scheme. In a team-managed space, only the *Administrator* role has it, so only
   do this if you're comfortable giving the bot that role. The bot checks this at startup and says in its log which
   way it went.
3. **The requester has a Jira account with the same email, and the bot can see that email.** Jira Cloud hides
   people's emails from accounts that aren't admins, unless they set their email to be visible to *Anyone* in their
   Atlassian profile. Jira Data Center usually shows them.

If any of these isn't true, the bot quietly falls back to its own account. Set `JIRA_SET_REPORTER=false` to never
look up emails at all.

## Deploying

Socket Mode only needs a long-running process that can make outbound connections. It needs no inbound port. The
`Procfile` (`worker: python -m helpdesk`) works on hosts such as Railway, Render or Heroku. Set the same environment
variables there instead of using a `.env` file.

**Run only one copy at a time.** If two copies run (for example on your laptop and on a host), Slack splits
incoming messages between them. Each copy then only sees part of every conversation: split posts may not be merged
into one ticket, and both copies post the weekly report. Stop the local copy before starting a hosted one.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every line of the Slack manifest is underlined in red | The first line, `display_information:`, is missing. Add it back or use the JSON tab |
| Bot never reacts | It isn't in the channel (`/invite`), or the app is missing the `message.channels` event |
| `Couldn't reach Jira project` at startup | Wrong URL, email or token, or the account can't see the project |
| Jira returns 401 with a correct token | The token was created "with scopes". Create a plain API token instead |
| `issuetype: … invalid` | `JIRA_ISSUE_TYPE` doesn't exist in the project. Check the names under *Project settings*, then *Issue types* |
| `labels` error when creating | The Labels field isn't on the project's create screen |
| "couldn't find a way to close" | The workflow has no non-cancel transition to a Done status from the current one. Set `JIRA_DONE_TRANSITION` to the transition's exact name |
| Buttons do nothing | Interactivity is off in the Slack app settings (the manifest turns it on) |
| A ticket has no buttons in Slack | Slack rejected the formatted message, so the bot posted plain text instead. The bot's log says why |
| A Jira comment says "The bot couldn't post this ticket in Slack" | Slack was unreachable or refused the message. The requester wasn't told, so contact them. The bot's log has the error |
| A status change in Jira never shows up in Slack | It was made while the bot was stopped, or by the bot's own Jira account, or `JIRA_SYNC_SECONDS` is `0`. If the bot's log says the description "doesn't link to a thread", someone removed the *Slack thread:* line from the ticket |
| A coworker's reply stops the AI | `IT_STAFF` isn't set. List your IT staff there |
| `Couldn't read the IT_STAFF user groups` at startup | The app needs the `usergroups:read` scope (then reinstall it), and user groups need a paid Slack plan. Or list member IDs instead |
| Private channel not found | Use the channel ID in `IT_CHANNEL`, add `groups:history` and `groups:read`, and subscribe to `message.groups` |
