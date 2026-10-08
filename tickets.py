"""How the bot's ticket messages look in Slack, and how it recognises its tickets in a thread later.

The bot keeps no database. A thread belongs to a ticket if it contains a message from this bot whose text starts
with "Ticket <KEY> created". The requester is whoever started the thread, and Jira holds everything else
(status, labels, escalation).
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from assistant import CATEGORY_NAMES, KIND_NAMES
from text_utils import escape_mrkdwn, truncate

RESOLVE_ACTION = "resolve_ticket"
ESCALATE_ACTION = "escalate_ticket"
ESCALATED_LABEL = "escalated"
CATEGORY_LABEL_PREFIX = "category-"

_TICKET_TEXT = re.compile(r"Ticket ([A-Z][A-Z0-9_]*-\d+) created")
# The key may be plain ("IT-6") or a Slack link ("<https://…/browse/IT-6|IT-6>"), the format used since #28.
_LINKED_TEXT = re.compile(r"Added to ticket (?:<[^|>]+\|)?([A-Z][A-Z0-9_]*-\d+)")
_DETAILS_PROMPT = re.compile(r"Hi <@\w+>! What's going on\?")
# The "Slack thread:" line the bot writes in its ticket descriptions. A link to a reply also carries ?thread_ts=…
_THREAD_LINK = re.compile(r"^Slack thread: (https://\S+/archives/([CG][A-Z0-9]+)/p(\d+)(\d{6})\S*)$", re.MULTILINE)
_SLACK_TS = re.compile(r"\d+\.\d{6}")
JIRA_NOTE_ICON = ":arrows_counterclockwise:"  # starts the bot's notes about changes made in Jira
_SECTION_LIMIT = 2900  # Slack allows 3,000 characters in a section block

SECRET_WARNING = (":lock: Your message looks like it contains a password or key. I kept it out of the ticket, "
                  "but please delete it from Slack and change it.")


@dataclass(frozen=True)
class TicketRef:
    key: str
    creator: str  # Slack user ID of the requester
    thread_ts: str

    def to_value(self):
        return json.dumps({"key": self.key, "creator": self.creator, "thread_ts": self.thread_ts})

    @classmethod
    def from_value(cls, value):
        data = json.loads(value)
        return cls(data["key"], data["creator"], data["thread_ts"])


def ticket_labels(base_label, triage):
    return [base_label, triage.kind, CATEGORY_LABEL_PREFIX + triage.category]


def ticket_text(ticket, triage, reply):
    """Plain-text version of the ticket message. find_ticket() looks for its opening words, so keep them."""
    text = f"Ticket {ticket.key} created: {escape_mrkdwn(triage.summary)}"
    return f"{text}\n\n{reply}" if reply else text


def ticket_blocks(ticket, url, triage, reply, secret_removed=False):
    """The first message in a ticket thread. `reply` must already be Slack mrkdwn."""
    blocks = [
        _section(f":ticket: *<{url}|{ticket.key}>* {escape_mrkdwn(triage.summary)}"),
        _context(
            f"{KIND_NAMES[triage.kind]} · {CATEGORY_NAMES[triage.category]} · {triage.priority} priority"
            f" · opened for <@{ticket.creator}>"
        ),
    ]
    if secret_removed:
        blocks.append(_section(SECRET_WARNING))
    if reply:
        blocks.append(_section(reply))
    elif triage.kind == "incident":
        blocks.append(_section("The IT team will follow up in this thread."))
    else:
        blocks.append(_section(
            f"I've logged this as {_with_article(KIND_NAMES[triage.kind].lower())}. "
            "The IT team will follow up in this thread."
        ))
    if triage.kind == "incident":
        blocks.append(_buttons(ticket))
    return blocks


def reply_blocks(text, ticket):
    """A follow-up message in a ticket thread, with the resolve/escalate buttons underneath."""
    return [_section(text), _buttons(ticket)]


def has_buttons(blocks):
    return any(block.get("type") == "actions" for block in blocks or [])


def without_buttons(blocks):
    return [block for block in blocks if block.get("type") != "actions"]


def find_ticket(messages, bot_user_id):
    """Find the ticket this bot opened in a thread. `messages` is conversations.replies output (parent first)."""
    if not messages:
        return None
    parent = messages[0]
    for message in messages[1:]:
        if message.get("user") == bot_user_id:
            match = _TICKET_TEXT.match(message.get("text", ""))
            if match:
                return TicketRef(match.group(1), parent.get("user", ""), parent["ts"])
    return None


def details_prompt_text(user_id):
    """The bot's reply to a post with no details yet ("Hi team"). Keep the opening words: awaiting_details() needs them."""
    return f"Hi <@{user_id}>! What's going on? Reply here with the details and I'll open a ticket."


def awaiting_details(messages, bot_user_id, author):
    """True if `author` started this thread and the bot asked them for details there."""
    return bool(messages) and messages[0].get("user") == author and any(
        message.get("user") == bot_user_id and _DETAILS_PROMPT.match(message.get("text", ""))
        for message in messages[1:]
    )


def linked_text(key, ticket_url, thread_permalink):
    """The bot's reply to an extra post that was added to an existing ticket. find_linked_ticket() reads it."""
    return f"Added to ticket <{ticket_url}|{key}>: <{thread_permalink}|continue in the ticket thread>"


def find_linked_ticket(messages, bot_user_id):
    """The ticket key if this thread is an extra post the bot added to a ticket opened elsewhere."""
    for message in messages[1:]:
        if message.get("user") == bot_user_id:
            match = _LINKED_TEXT.match(message.get("text", ""))
            if match:
                return match.group(1)
    return None


def has_later_message_from(messages, user_id, ts):
    """True if `user_id` posted in the thread after message `ts`, so answering `ts` alone would be out of date."""
    return any(message.get("user") == user_id and float(message.get("ts", 0)) > float(ts) for message in messages)


def human_took_over(messages, creator, bot_user_id, staff=None):
    """True once someone other than the requester and this bot has replied, e.g. someone from IT. With `staff`
    (IT_STAFF), only replies from those people count, so a coworker's "+1" doesn't silence the AI."""
    return any(
        message.get("user") and message["user"] not in (creator, bot_user_id) and not message.get("bot_id")
        and (staff is None or message["user"] in staff)
        for message in messages[1:]
    )


def ai_reply_count(messages, bot_user_id):
    """How many messages the bot has posted in the thread after its ticket message, not counting notes about
    changes made in Jira."""
    count, after_ticket = 0, False
    for message in messages[1:]:
        if message.get("user") != bot_user_id:
            continue
        if _TICKET_TEXT.match(message.get("text", "")):
            after_ticket = True
        elif after_ticket and not is_jira_note(message):
            count += 1
    return count


def conversation_history(messages, creator, bot_user_id, clean):
    """The requester's and the bot's messages as chat history for the AI model. `clean` converts Slack text."""
    history = []
    for message in messages:
        if message.get("user") == bot_user_id:
            if is_jira_note(message):
                continue  # the bot relaying a Jira change, not something the AI said
            role = "assistant"
        elif message.get("user") == creator and not message.get("bot_id"):
            role = "user"
        else:
            continue
        content = clean(message.get("text", ""))
        if content:
            history.append({"role": role, "content": content})
    return history


def slack_thread_in(description):
    """(channel, thread ts) from the "Slack thread:" link in a ticket description the bot wrote, or None.

    Only the lines before the requester's own text (which is in a {noformat} block) are read, so a link someone
    pasted into their message can't point the bot at another thread.
    """
    match = _THREAD_LINK.search((description or "").split("{noformat}", 1)[0])
    if not match:
        return None
    url, channel, seconds, micros = match.groups()
    thread_ts = (parse_qs(urlsplit(url).query).get("thread_ts") or [""])[0]
    return channel, thread_ts if _SLACK_TS.fullmatch(thread_ts) else f"{seconds}.{micros}"


def jira_changes_text(key, url, changes, closed):
    """The bot's note in a ticket thread about status changes someone made in Jira. `changes` is a list of
    (who, from status, to status) as Jira gave them, so they're escaped here."""
    lines = [f"{JIRA_NOTE_ICON} {escape_mrkdwn(who)} moved <{url}|{key}> from *{escape_mrkdwn(old or 'no status')}* "
             f"to *{escape_mrkdwn(new or 'no status')}* in Jira." for who, old, new in changes]
    if closed:
        lines.append("This ticket is closed now. If you still need help, post a new message in the channel.")
    return "\n".join(lines)


def is_jira_note(message):
    return message.get("text", "").startswith(JIRA_NOTE_ICON)


def _with_article(noun):
    return ("an " if noun[0] in "aeiou" else "a ") + noun


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": truncate(text, _SECTION_LIMIT)}}


def _context(text):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _buttons(ticket):
    value = ticket.to_value()
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": RESOLVE_ACTION,
                "style": "primary",
                "text": {"type": "plain_text", "text": "✅ That fixed it", "emoji": True},
                "value": value,
            },
            {
                "type": "button",
                "action_id": ESCALATE_ACTION,
                "style": "danger",
                "text": {"type": "plain_text", "text": "🆘 Escalate to IT", "emoji": True},
                "value": value,
            },
        ],
    }
