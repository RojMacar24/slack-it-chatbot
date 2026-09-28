# Security

This is a lab project. Review it before pointing it at a production Slack workspace or Jira site.

## Credentials

- All secrets come from environment variables or a local `.env` file, which `.gitignore` excludes. Never commit `.env`.
- Use a dedicated Jira account or token with access only to the IT project, and a Slack app installed only where you
  need it.
- Rotate the Slack tokens, Jira token and OpenAI key if they're ever exposed, and on a regular schedule.

## Where data goes

| Data | Sent to | Why |
|---|---|---|
| Text of posts and thread replies in the IT channel | Jira (description and comments) | The ticket record |
| The same text | OpenAI, only if `OPENAI_API_KEY` is set | Triage and troubleshooting replies |
| Slack display names | Jira | Shows who reported or replied |
| Slack user and channel IDs, message timestamps | Logs | Debugging. Message text isn't logged |

The bot has no database. Before text is sent to Jira or OpenAI, it masks anything that looks like a secret:
`password: …`-style values, Slack tokens, OpenAI-style keys, AWS access key IDs, GitHub tokens and private keys.
The requester is then asked to delete the message and change the secret. This is best-effort pattern matching, so
tell users never to post credentials.

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

- AI output is escaped before posting, so a manipulated reply can't `@channel`, mention users or create hidden links.
- The prompt tells the model never to ask for passwords or MFA codes, never to suggest disabling security tools,
  and to treat user messages as problem descriptions rather than instructions. Prompt injection can't be ruled out
  completely, so treat AI replies as suggestions.
- The AI stops replying after `MAX_AI_FOLLOW_UPS` replies, which also caps cost per ticket.

## Reporting a problem

If you find a security issue in this project, report it privately to the repository owner, for example through a
GitHub private security advisory, instead of opening a public issue.
