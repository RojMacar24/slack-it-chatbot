import json

import pytest
from slack_bolt import App
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request import BoltRequest

import tickets
from assistant import Assistant
from bot import HelpDesk, is_report_command, resolve_channel_id, schedule_weekly_report
from config import ConfigError
from fakes import BOT_USER_ID, CHANNEL_ID, FakeAssistant, FakeJira, FakeSlack, make_config

REQUESTER = "UREQ"
ENGINEER = "UENG"


@pytest.fixture
def slack():
    return FakeSlack()


@pytest.fixture
def jira():
    return FakeJira()


@pytest.fixture
def ai():
    return FakeAssistant()


@pytest.fixture
def desk(slack, jira, ai):
    return HelpDesk(make_config(ESCALATION_MENTION="<!subteam^SIT>"), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)


def post(desk, slack, text, user=REQUESTER, **extra):
    """A new top-level post in the IT channel."""
    ts = slack.next_ts()
    event = {"type": "message", "channel": CHANNEL_ID, "user": user, "text": text, "ts": ts, **extra}
    slack.threads[ts] = [dict(event)]
    desk.on_message(event)
    return ts


def reply(desk, slack, thread_ts, text, user=REQUESTER):
    ts = slack.next_ts()
    event = {"type": "message", "channel": CHANNEL_ID, "user": user, "text": text, "ts": ts, "thread_ts": thread_ts}
    slack.threads[thread_ts].append(dict(event))
    desk.on_message(event)
    return ts


def click(desk, slack, thread_ts, action_id, user=REQUESTER):
    """Press a button on the first bot message in the thread that has buttons."""
    message = next(m for m in slack.threads[thread_ts] if m.get("blocks") and tickets.without_buttons(m["blocks"]) != m["blocks"])
    buttons = next(b for b in message["blocks"] if b["type"] == "actions")["elements"]
    button = next(b for b in buttons if b["action_id"] == action_id)
    body = {
        "user": {"id": user},
        "channel": {"id": CHANNEL_ID},
        "actions": [{"action_id": action_id, "value": button["value"]}],
        "container": {"message_ts": message["ts"]},
        "message": message,
    }
    desk.on_button(body, desk.resolve if action_id == tickets.RESOLVE_ACTION else desk.escalate)


def last_post(slack):
    return slack.calls_to("chat_postMessage")[-1]


# --- Opening tickets ---------------------------------------------------------------------------------------------

def test_new_post_opens_a_labelled_ticket_and_replies_in_thread(desk, slack, jira, ai):
    slack.names[REQUESTER] = "Sam Rivera"
    ts = post(desk, slack, "My VPN keeps disconnecting")

    [issue] = jira.created
    assert issue["issue_type"] == "Task"
    assert issue["labels"] == ["slack-it-bot", "incident", "category-network"]
    assert issue["priority"] == "Medium"
    assert "Reported in Slack by Sam Rivera." in issue["description"]
    assert f"https://slack.example/archives/{CHANNEL_ID}/p" in issue["description"]

    message = last_post(slack)
    assert message["thread_ts"] == ts
    assert message["text"].startswith("Ticket IT-1 created: My VPN keeps disconnecting")
    rendered = json.dumps(message["blocks"])
    assert "https://jira.example/browse/IT-1" in rendered
    assert "*restarting*" in rendered  # Markdown bold converted for Slack
    assert tickets.RESOLVE_ACTION in rendered and tickets.ESCALATE_ACTION in rendered
    assert jira.comments == [("IT-1", "AI assistant replied in Slack:\n{noformat}\nTry **restarting** the VPN client.\n{noformat}")]


def test_passwords_are_kept_out_of_jira_and_the_ai(desk, slack, jira, ai):
    post(desk, slack, "Can't log in, password: Hunter2! please help")

    assert "Hunter2" not in jira.created[0]["description"]
    assert "Hunter2" not in ai.assessed[0]
    assert "delete it from Slack" in json.dumps(last_post(slack)["blocks"])


def test_access_requests_use_the_request_issue_type_and_get_no_buttons(slack, jira, ai):
    desk = HelpDesk(make_config(JIRA_REQUEST_ISSUE_TYPE="Service Request"), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)
    post(desk, slack, "I need access to the finance dashboard")

    assert jira.created[0]["issue_type"] == "Service Request"
    assert "access-request" in jira.created[0]["labels"]
    blocks = last_post(slack)["blocks"]
    assert "logged this as an access request" in json.dumps(blocks)
    assert tickets.without_buttons(blocks) == blocks


def test_jira_failure_is_reported_in_the_thread(desk, slack, jira):
    jira.fail_create = True
    ts = post(desk, slack, "Printer is jammed")

    message = last_post(slack)
    assert message["thread_ts"] == ts
    assert "couldn't create a Jira ticket" in message["text"]
    assert "<!subteam^SIT>" in message["text"]


def test_ignores_other_channels_bots_edits_and_duplicate_events(desk, slack, jira):
    desk.on_message({"channel": "COTHER", "user": REQUESTER, "text": "hi", "ts": "1.1"})
    desk.on_message({"channel": CHANNEL_ID, "bot_id": "B1", "user": "U9", "text": "hi", "ts": "1.2"})
    desk.on_message({"channel": CHANNEL_ID, "subtype": "message_changed", "text": "hi", "ts": "1.3"})
    event = {"channel": CHANNEL_ID, "user": REQUESTER, "text": "Laptop won't boot", "ts": "1.4"}
    slack.threads["1.4"] = [dict(event)]
    desk.on_message(event)
    desk.on_message(dict(event))  # Slack redelivered it
    assert len(jira.created) == 1


def test_report_command_is_not_treated_as_a_ticket(desk, slack, jira):
    post(desk, slack, f"<@{BOT_USER_ID}> report")
    assert jira.created == []


@pytest.mark.parametrize("text, expected", [
    (f"<@{BOT_USER_ID}> report", True),
    (f"<@{BOT_USER_ID}>   Stats!", True),
    (f"<@{BOT_USER_ID}> I want to report a problem", False),
    ("report", False),
])
def test_is_report_command(text, expected):
    assert is_report_command(text, BOT_USER_ID) is expected


# --- Thread conversations --------------------------------------------------------------------------------------

def test_requester_reply_gets_an_ai_follow_up_and_is_copied_to_jira(desk, slack, jira, ai):
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "Restarted it, still failing")

    assert ("IT-1", "UREQ replied in Slack:\n{noformat}\nRestarted it, still failing\n{noformat}") in jira.comments
    history = ai.histories[0]
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    assert history[-1]["content"] == "Restarted it, still failing"
    follow_up = last_post(slack)
    assert follow_up["text"] == "Next, check the network settings."
    assert tickets.ESCALATE_ACTION in json.dumps(follow_up["blocks"])


def test_ai_stays_quiet_once_someone_from_it_joins(desk, slack, jira, ai):
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "Looking into it now", user=ENGINEER)
    reply(desk, slack, ts, "Thanks!")

    assert ai.histories == []
    assert [key for key, _ in jira.comments].count("IT-1") == 3  # first AI reply + both Slack replies


def test_ignores_threads_without_a_ticket(desk, slack, jira, ai):
    ts = slack.next_ts()
    slack.threads[ts] = [{"ts": ts, "user": REQUESTER, "text": "Lunch?"}]
    reply(desk, slack, ts, "Sure")
    assert jira.comments == [] and ai.histories == []


def test_ai_stops_after_escalation(desk, slack, jira, ai):
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.ESCALATE_ACTION)
    reply(desk, slack, ts, "Any update?")
    assert ai.histories == []


def test_ai_follow_ups_are_capped(slack, jira, ai):
    desk = HelpDesk(make_config(MAX_AI_FOLLOW_UPS="2"), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)
    ts = post(desk, slack, "VPN won't connect")
    for attempt in range(5):
        reply(desk, slack, ts, f"Still broken ({attempt})")

    assert len(ai.histories) == 2
    bot_texts = [m["text"] for m in slack.threads[ts] if m.get("user") == BOT_USER_ID]
    assert sum("suggested everything I can" in text for text in bot_texts) == 1


def test_without_ai_the_bot_still_opens_tickets_and_mirrors_replies(slack, jira):
    desk = HelpDesk(make_config(), slack, jira, Assistant(), BOT_USER_ID, CHANNEL_ID)
    ts = post(desk, slack, "URGENT: can't work, laptop screen is black")
    reply(desk, slack, ts, "Tried a restart")

    assert jira.created[0]["priority"] == "High"
    assert jira.created[0]["labels"][2] == "category-hardware"
    assert jira.comments == [("IT-1", "UREQ replied in Slack:\n{noformat}\nTried a restart\n{noformat}")]
    assert len(slack.calls_to("chat_postMessage")) == 1  # just the ticket message


# --- Buttons ---------------------------------------------------------------------------------------------------

def test_only_the_requester_can_use_the_buttons(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.RESOLVE_ACTION, user=ENGINEER)

    [ephemeral] = slack.calls_to("chat_postEphemeral")
    assert ephemeral["user"] == ENGINEER and "Only <@UREQ>" in ephemeral["text"]
    assert jira.issues["IT-1"]["status"]["statusCategory"]["key"] == "new"


def test_resolve_closes_the_ticket_and_removes_the_buttons(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.RESOLVE_ACTION)

    assert jira.issues["IT-1"]["status"]["statusCategory"]["key"] == "done"
    assert ":white_check_mark:" in last_post(slack)["text"]
    [update] = slack.calls_to("chat_update")
    assert update["text"].startswith("Ticket IT-1 created")  # keeps the text find_ticket relies on
    assert "actions" not in json.dumps(update["blocks"])

    click(desk, slack, ts, tickets.RESOLVE_ACTION)
    assert "already closed" in slack.calls_to("chat_postEphemeral")[-1]["text"]


def test_resolve_explains_when_the_workflow_has_no_done_transition(desk, slack, jira):
    jira.can_transition = False
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.RESOLVE_ACTION)
    assert "couldn't find a way to close IT-1" in slack.calls_to("chat_postEphemeral")[-1]["text"]
    assert slack.calls_to("chat_update") == []


def test_escalate_labels_the_ticket_and_mentions_the_it_team(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.ESCALATE_ACTION)

    assert "escalated" in jira.issues["IT-1"]["labels"]
    assert "<!subteam^SIT> will take it from here" in last_post(slack)["text"]
    click(desk, slack, ts, tickets.ESCALATE_ACTION)
    assert "already been escalated" in slack.calls_to("chat_postEphemeral")[-1]["text"]


# --- Reports, setup and Bolt wiring ----------------------------------------------------------------------------

def test_report_mention_posts_summary_in_thread(desk, slack, jira):
    post(desk, slack, "VPN won't connect")
    desk.on_mention({"channel": CHANNEL_ID, "user": REQUESTER, "text": f"<@{BOT_USER_ID}> report", "ts": "9.9"})
    message = last_post(slack)
    assert message["thread_ts"] == "9.9"
    assert "*Opened:* 1 (1 incident)" in message["text"]


def test_mention_elsewhere_points_people_to_the_it_channel(desk, slack):
    desk.on_mention({"channel": "COTHER", "user": REQUESTER, "text": f"<@{BOT_USER_ID}> hello", "ts": "9.9"})
    assert f"<#{CHANNEL_ID}>" in last_post(slack)["text"]


def test_resolve_channel_id_pages_through_channels():
    class Pages:
        def conversations_list(self, **kwargs):
            if "cursor" not in kwargs:
                return {"channels": [{"name": "general", "id": "C1"}], "response_metadata": {"next_cursor": "next"}}
            return {"channels": [{"name": "it-help", "id": "C2"}], "response_metadata": {"next_cursor": ""}}

    assert resolve_channel_id(Pages(), "#it-help") == "C2"
    assert resolve_channel_id(Pages(), "C0ABCDEF12") == "C0ABCDEF12"
    with pytest.raises(ConfigError):
        resolve_channel_id(Pages(), "missing")


def test_weekly_report_schedule_uses_configured_time(desk):
    scheduler = schedule_weekly_report(desk, make_config(REPORT_DAY="fri", REPORT_HOUR="16", REPORT_TIMEZONE="America/New_York"))
    [job] = scheduler.get_jobs()
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "fri" and fields["hour"] == "16"
    assert str(job.trigger.timezone) == "America/New_York"


def test_bolt_routes_events_and_button_clicks_to_the_desk(desk, slack, jira):
    app = App(
        signing_secret="unused",
        authorize=lambda **_: AuthorizeResult(enterprise_id=None, team_id="T1", bot_token="xoxb-test",
                                              bot_user_id=BOT_USER_ID, bot_id="BBOT"),
        process_before_response=True,
        request_verification_enabled=False,
    )
    desk.register(app)

    ts = slack.next_ts()
    event = {"type": "message", "channel": CHANNEL_ID, "user": REQUESTER, "text": "Wi-Fi is down", "ts": ts}
    slack.threads[ts] = [dict(event)]
    response = app.dispatch(BoltRequest(mode="socket_mode", body={
        "type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event_id": "Ev1", "event": event,
    }))
    assert response.status == 200
    assert [issue["key"] for issue in jira.created] == ["IT-1"]

    message = slack.threads[ts][-1]
    value = next(b for b in message["blocks"] if b["type"] == "actions")["elements"][0]["value"]
    response = app.dispatch(BoltRequest(mode="socket_mode", body={
        "type": "block_actions", "team": {"id": "T1"}, "api_app_id": "A1", "user": {"id": REQUESTER},
        "channel": {"id": CHANNEL_ID}, "container": {"message_ts": message["ts"]}, "message": message,
        "actions": [{"type": "button", "action_id": tickets.RESOLVE_ACTION, "block_id": "b", "value": value}],
    }))
    assert response.status == 200
    assert jira.issues["IT-1"]["status"]["statusCategory"]["key"] == "done"
