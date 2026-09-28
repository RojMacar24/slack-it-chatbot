import pytest

import tickets
from assistant import Triage
from config import ConfigError
from fakes import BOT_USER_ID, FakeJira, make_config
from reports import build_report


def test_report_counts_from_jira():
    jira = FakeJira()
    labels = {
        "IT-1": ["slack-it-bot", "incident", "category-network"],
        "IT-2": ["slack-it-bot", "incident", "category-network", "escalated"],
        "IT-3": ["slack-it-bot", "access-request", "category-software"],
    }
    for key, issue_labels in labels.items():
        jira.issues[key] = {"status": {"statusCategory": {"key": "done" if key == "IT-1" else "new"}}, "labels": issue_labels}

    report = build_report(jira, "IT", "slack-it-bot")
    assert "*Opened:* 3 (2 incidents, 1 access request)" in report
    assert "*Resolved:* 1   *Escalated:* 1   *Still open:* 2" in report
    assert "*Top categories:* Network/VPN (2), Software (1)" in report
    assert 'labels = "slack-it-bot"' in jira.last_jql
    assert "https://jira.example/issues/?jql=project%20%3D" in report


def test_report_with_no_tickets():
    assert build_report(FakeJira(), "IT", "slack-it-bot").endswith("No tickets were opened.")


def test_ticket_ref_round_trips_through_a_button_value():
    ref = tickets.TicketRef("IT-3", "UREQ", "123.456")
    assert tickets.TicketRef.from_value(ref.to_value()) == ref


def test_find_ticket_only_trusts_the_bots_own_message():
    parent = {"ts": "1.0", "user": "UREQ", "text": "help"}
    forged = {"ts": "1.1", "user": "UOTHER", "text": "Ticket IT-9 created: fake"}
    real = {"ts": "1.2", "user": BOT_USER_ID, "bot_id": "B", "text": "Ticket IT-3 created: VPN down"}
    assert tickets.find_ticket([parent, forged], BOT_USER_ID) is None
    assert tickets.find_ticket([parent, forged, real], BOT_USER_ID) == tickets.TicketRef("IT-3", "UREQ", "1.0")
    assert tickets.find_ticket([], BOT_USER_ID) is None


def test_thread_helpers():
    thread = [
        {"ts": "1", "user": "UREQ", "text": "VPN down"},
        {"ts": "2", "user": BOT_USER_ID, "bot_id": "B", "text": "Ticket IT-1 created: VPN down"},
        {"ts": "3", "user": "UREQ", "text": "still down"},
        {"ts": "4", "user": BOT_USER_ID, "bot_id": "B", "text": "Try this"},
        {"ts": "5", "bot_id": "BOTHER", "user": "UOTHERBOT", "text": "other bot"},
        {"ts": "6", "subtype": "channel_join", "text": "joined"},
    ]
    assert not tickets.human_took_over(thread, "UREQ", BOT_USER_ID)
    assert tickets.human_took_over(thread + [{"ts": "7", "user": "UENG", "text": "on it"}], "UREQ", BOT_USER_ID)
    assert tickets.ai_reply_count(thread, BOT_USER_ID) == 1
    history = tickets.conversation_history(thread, "UREQ", BOT_USER_ID, str.upper)
    assert history == [
        {"role": "user", "content": "VPN DOWN"},
        {"role": "assistant", "content": "TICKET IT-1 CREATED: VPN DOWN"},
        {"role": "user", "content": "STILL DOWN"},
        {"role": "assistant", "content": "TRY THIS"},
    ]


def test_ticket_message_escapes_the_summary():
    ref = tickets.TicketRef("IT-1", "UREQ", "1.0")
    triage = Triage("incident", "other", "Medium", "<!channel> & stuff")
    assert tickets.ticket_text(ref, triage, "") == "Ticket IT-1 created: &lt;!channel&gt; &amp; stuff"
    assert "&lt;!channel&gt;" in tickets.ticket_blocks(ref, "https://j/IT-1", triage, "")[0]["text"]["text"]


def test_config_defaults_and_environment_notes():
    config = make_config(JIRA_PROJECT_KEY="it", JIRA_BASE_URL="https://jira.example/")
    assert config.jira_project_key == "IT"
    assert config.jira_base_url == "https://jira.example"
    assert config.jira_request_issue_type == "Task"
    assert config.openai_api_key is None
    assert config.max_ai_follow_ups == 3
    assert "IT environment notes" in config.it_environment


@pytest.mark.parametrize("overrides, message", [
    ({"JIRA_PROJECT_KEY": ""}, "JIRA_PROJECT_KEY"),
    ({"SLACK_BOT_TOKEN": "xapp-wrong"}, "xoxb-"),
    ({"SLACK_APP_TOKEN": "xoxb-wrong"}, "xapp-"),
    ({"MAX_AI_FOLLOW_UPS": "lots"}, "whole number"),
    ({"JIRA_LABEL": "two words"}, "spaces"),
])
def test_config_errors(overrides, message):
    with pytest.raises(ConfigError, match=message):
        make_config(**overrides)
