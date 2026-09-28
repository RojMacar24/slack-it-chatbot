"""How the bot's ticket messages look in Slack, and how it recognises its tickets in a thread later.

The bot keeps no database. A thread belongs to a ticket if it contains a message from this bot whose text starts
with "Ticket <KEY> created". The requester is whoever started the thread, and Jira holds everything else
(status, labels, escalation).
"""

import json
import re
from dataclasses import dataclass

from assistant import CATEGORY_NAMES, KIND_NAMES
from text_utils import escape_mrkdwn, truncate

RESOLVE_ACTION = "resolve_ticket"
ESCALATE_ACTION = "escalate_ticket"
ESCALATED_LABEL = "escalated"
CATEGORY_LABEL_PREFIX = "category-"

_TICKET_TEXT = re.compile(r"Ticket ([A-Z][A-Z0-9_]*-\d+) created")
_SECTION_LIMIT = 2900  # Slack allows 3,000 characters in a section block


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
        blocks.append(_section(
            ":lock: Your message looks like it contains a password or key. I kept it out of the ticket, "
            "but please delete it from Slack and change it."
        ))
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


def human_took_over(messages, creator, bot_user_id):
    """True once anyone other than the requester and this bot has replied, e.g. someone from IT."""
    return any(
        message.get("user") and message["user"] not in (creator, bot_user_id) and not message.get("bot_id")
        for message in messages[1:]
    )


def ai_reply_count(messages, bot_user_id):
    """How many messages the bot has posted in the thread after the ticket message."""
    return sum(
        1 for message in messages[1:]
        if message.get("user") == bot_user_id and not _TICKET_TEXT.match(message.get("text", ""))
    )


def conversation_history(messages, creator, bot_user_id, clean):
    """The requester's and the bot's messages as chat history for the AI model. `clean` converts Slack text."""
    history = []
    for message in messages:
        if message.get("user") == bot_user_id:
            role = "assistant"
        elif message.get("user") == creator and not message.get("bot_id"):
            role = "user"
        else:
            continue
        content = clean(message.get("text", ""))
        if content:
            history.append({"role": role, "content": content})
    return history


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
