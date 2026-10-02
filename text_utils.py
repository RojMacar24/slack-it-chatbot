"""Text conversions between Slack, Jira and the AI model, plus secret redaction."""

import html
import re
from urllib.parse import urlsplit

_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL_MENTION = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
_GROUP_MENTION = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]+))?>")
_SPECIAL_MENTION = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]+))?>")

_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$", re.MULTILINE)
# A web address in text that has already been through escape_mrkdwn(): it ends at whitespace, "|" or an escaped < >.
_URL = re.compile(r"https?://(?:(?!&lt;|&gt;)[^\s|])+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?)'\""
LINK_REMOVED = "[link removed]"

_SECRETS = [
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"),  # Slack app-level tokens
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"),  # Slack incoming webhook URLs
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI-style API keys
    re.compile(r"\bATATT[A-Za-z0-9_=-]{20,}"),  # Atlassian API tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key IDs
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),  # Google API keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWTs (header.payload.signature)
    re.compile(r"(?<=\bbearer )[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),  # "Authorization: Bearer <token>"
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]
# "password: hunter2" or "pin=1234". Deliberately not "password is ...", which is usually "password is expired".
_LABELLED_SECRET = re.compile(
    r"\b(password|passwd|pwd|passcode|pin|otp|mfa code|api key|secret|token)(\s*[:=]\s*)(\S+)",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def slack_to_plain(text, user_name=None):
    """Replace Slack markup (<@U123>, <https://x|label>, &amp;) with readable plain text.

    `user_name` maps a Slack user ID to a display name; without it, mentions keep the raw ID.
    """
    if user_name is None:
        def user_name(user_id):
            return user_id

    text = _USER_MENTION.sub(lambda m: "@" + (m.group(2) or user_name(m.group(1))), text)
    text = _CHANNEL_MENTION.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = _GROUP_MENTION.sub(lambda m: "@" + (m.group(1) or "group").lstrip("@"), text)
    text = _SPECIAL_MENTION.sub(lambda m: "@" + m.group(1), text)
    text = _LINK.sub(_plain_link, text)
    return html.unescape(text).strip()


def _plain_link(match):
    url, label = match.group(1), match.group(2)
    if url.startswith("mailto:"):
        return label or url[len("mailto:"):]
    return f"{label} ({url})" if label and label != url else url


def escape_mrkdwn(text):
    """Escape the characters Slack treats as markup, so text can't mention @channel or fake a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_slack_mrkdwn(text, allowed_link_domains=()):
    """Make AI output safe to post in Slack, then convert the Markdown models tend to write into Slack's mrkdwn.

    Links always show their full address, so a manipulated reply can't disguise where a link goes. If
    `allowed_link_domains` is given, links to any other domain (or its subdomains) are replaced with LINK_REMOVED.
    """
    text = escape_mrkdwn(text)
    text = _MD_LINK.sub(lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    if allowed_link_domains:
        text = _URL.sub(lambda m: _keep_allowed_link(m.group(0), allowed_link_domains), text)
    text = _MD_BOLD.sub(r"*\1*", text)
    text = _MD_HEADING.sub(lambda m: "*" + m.group(1).strip("*") + "*", text)
    return text.strip()


def _keep_allowed_link(url, allowed_domains):
    core = url.rstrip(_URL_TRAILING)  # punctuation after a link isn't part of it
    try:
        host = (urlsplit(html.unescape(core)).hostname or "").lower()
    except ValueError:
        host = ""
    allowed = any(host == domain or host.endswith("." + domain) for domain in allowed_domains)
    return (core if allowed else LINK_REMOVED) + url[len(core):]


def redact_secrets(text):
    """Mask things that look like passwords, tokens or keys before text is sent to Jira or the AI model."""
    for pattern in _SECRETS:
        text = pattern.sub(REDACTED, text)
    return _LABELLED_SECRET.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)


def truncate(text, limit):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
