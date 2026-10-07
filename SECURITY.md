# Security

This is a lab project. Review it before pointing it at a production Slack workspace or Jira site.

## Credentials

- All secrets come from environment variables or a local `.env` file, which `.gitignore` excludes. Never commit `.env`.
- Use a dedicated Jira account or token with access only to the IT project, and a Slack app installed only where you
  need it.
- Rotate the Slack tokens, Jira token and OpenAI key if they're ever exposed, and on a regular schedule.
- `JIRA_BASE_URL` must use `https://`, so the Jira token is always encrypted in transit. Plain `http://` is only
  accepted for `localhost`, for local testing.

## Where data goes

| Data | Sent to | Why |
|---|---|---|
| Text of posts and thread replies in the IT channel | Jira (description and comments) | The ticket record |
| The same text | OpenAI, only if `OPENAI_API_KEY` is set | Triage and troubleshooting replies |
| Slack display names | Jira | Shows who reported or replied |
| Slack user and channel IDs, message timestamps, error details | Logs | Debugging. Message text isn't logged |

The bot has no database. Before text is sent to Jira or OpenAI, it masks anything that looks like a secret:
`password: …`-style values, Slack tokens and webhook URLs, OpenAI-style keys, Atlassian API tokens, AWS access key
IDs, Google API keys, GitHub tokens, JWTs, `Bearer` tokens and private keys. Whoever posted it, in a new post, a
thread reply or an extra post added to a ticket, is then asked to delete the message and change the secret. In a
reply, that request is a private message only they can see. This is best-effort pattern matching, so tell users
never to post credentials.

Message text goes into Jira inside a `{noformat}` block, so Jira shows it as written. Slack display names are
stripped of Jira's link and formatting characters first, because anyone can set their display name to Jira markup.

When Jira rejects a request, users see a plain message. The error details, which can include internal addresses,
go to the bot's log only.

OpenAI's retention and training policies for API data apply to anything sent there. Check them for your account.

## Slack permissions

The manifest requests only what the bot uses:

| Scope | Why |
|---|---|
| `channels:history` | Read posts and thread replies in the IT channel |
| `channels:read` | Find the channel by name |
| `chat:write` | Post ticket messages, replies and private notes |
| `app_mentions:read` | Respond to `@IT Help Desk report` |
| `users:read` | Show names instead of user IDs in Jira |

The bot only acts on messages in the configured channel. Only the person who opened a ticket can use its buttons.

## AI safety

- AI output is escaped before posting, so a manipulated reply can't `@channel` or mention users.
- Links in AI replies always show their full address, so a reply can't disguise a phishing link as "the IT portal".
  Set `AI_ALLOWED_LINK_DOMAINS` (for example `microsoft.com, zoom.us`) to remove links to any other domain.
- The prompt tells the model never to ask for passwords or MFA codes, never to suggest disabling security tools,
  and to treat user messages as problem descriptions rather than instructions. Prompt injection can't be ruled out
  completely, so treat AI replies as suggestions.
- The AI stops replying after `MAX_AI_FOLLOW_UPS` replies, which also caps cost per ticket.

## Dependencies and CI

- **Locked dependencies:** `requirements.in` and `requirements-dev.in` hold the accepted version ranges.
  `requirements.txt` and `requirements-dev.txt` are generated from them with `pip-compile --generate-hashes`, so
  every package is pinned to an exact version and checked against its SHA-256 hash when it's installed.
- **Pinned GitHub Actions:** the workflow references each action by its full commit SHA, not a tag that could be moved.
- **Dependabot:** security alerts and security-fix pull requests are on, and `.github/dependabot.yml` opens weekly
  update pull requests for Python packages and GitHub Actions.
- **CI:** every push to `main` and every pull request installs with `--require-hashes`, then runs `pyflakes` and the
  test suite. The workflow can only read the repository, and it doesn't keep the checkout credentials.
- **Linux and Windows:** CI runs on both, because a lock file compiled on one operating system can leave out packages
  the other needs.

## Known limitations

What the bot doesn't protect against, or only partly:

- **Secret masking is pattern-based.** It catches the common formats listed above, not every secret. A secret stays
  visible in the Slack message until its author deletes it; the bot can only ask them to.
- **Prompt injection is reduced, not prevented.** The model has no tools and its output is escaped, with links shown
  in full (or limited to `AI_ALLOWED_LINK_DOMAINS`). A crafted message can still steer what the AI *says*, so treat
  replies as suggestions.
- **Limits live in memory.** The ticket limit (`MAX_TICKETS_PER_HOUR`) and the merge window reset when the bot
  restarts, and only work if one copy of the bot runs. They slow down spam; they don't stop a determined attacker.
  Prepaid OpenAI credit with auto-recharge off is the hard cap on AI spending.
- **The bot can't tell IT staff from coworkers.** A reply from anyone other than the requester stops the AI (#7).
- **Changes made in Jira don't reach Slack** (#6). Closing a ticket in Jira doesn't update its Slack thread.
- **Every ticket is reported by the bot's Jira account** (#9). The requester's name is in the description.
- **Jira and OpenAI keep what they're sent** under their own retention policies. Data already sent can't be
  recalled by the bot.
- **The Jira token can do whatever its account can.** Use a dedicated Jira account that's a member of the IT space
  only, not an administrator.

## Reporting a problem

If you find a security issue in this project, report it privately to the repository owner, for example through a
GitHub private security advisory, instead of opening a public issue.
