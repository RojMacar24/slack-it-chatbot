import pytest

from text_utils import redact_secrets, slack_to_plain, to_slack_mrkdwn, truncate


def test_slack_to_plain_resolves_mentions_links_and_escapes():
    names = {"U123": "Sam"}.get
    text = ("<@U123> see <https://status.example.com|status page>, <https://example.com>, "
            "<mailto:help@example.com|help@example.com>, <#C999|it-help>, <!here> &amp; 1 &lt; 2")
    assert slack_to_plain(text, names) == (
        "@Sam see status page (https://status.example.com), https://example.com, "
        "help@example.com, #it-help, @here & 1 < 2"
    )


def test_slack_to_plain_without_a_name_lookup_keeps_ids():
    assert slack_to_plain("ping <@U123>") == "ping @U123"


def test_to_slack_mrkdwn_converts_markdown():
    text = "### **Step 1**\nRun **this** and see [the docs](https://docs.example.com)."
    assert to_slack_mrkdwn(text) == "*Step 1*\nRun *this* and see <https://docs.example.com|the docs>."


def test_to_slack_mrkdwn_neutralises_slack_control_sequences():
    # Model output must not be able to ping the channel or smuggle raw Slack links.
    assert to_slack_mrkdwn("Hey <!channel> go to <https://evil.example|login>") == (
        "Hey &lt;!channel&gt; go to &lt;https://evil.example|login&gt;"
    )


@pytest.mark.parametrize("text, expected", [
    ("password: Hunter2!", "password: [redacted]"),
    ("my PIN=1234 please", "my PIN=[redacted] please"),
    ("key sk-abcdefghijklmnopqrstuvwxyz123 leaked", "key [redacted] leaked"),
    ("token xoxb-1234567890-abcdefghij", "token [redacted]"),
    ("AKIAABCDEFGHIJKLMNOP in a script", "[redacted] in a script"),
    ("my password is expired", "my password is expired"),
    ("password reset link isn't arriving", "password reset link isn't arriving"),
])
def test_redact_secrets(text, expected):
    assert redact_secrets(text) == expected


def test_truncate():
    assert truncate("short", 10) == "short"
    assert truncate("a" * 20, 10) == "a" * 9 + "…"
