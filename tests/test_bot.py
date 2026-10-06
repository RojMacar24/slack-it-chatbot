import copy
import json
import threading
import time

import pytest
from slack_bolt import App
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request import BoltRequest
from slack_sdk.errors import SlackApiError

import tickets
from assistant import Assistant
from bot import HelpDesk, check_channel_access, is_report_command, resolve_channel_id, schedule_weekly_report
from config import ConfigError
from fakes import BOT_USER_ID, CHANNEL_ID, FakeAssistant, FakeJira, FakeSlack, make_config
from jira_client import JiraError

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


def reply(desk, slack, thread_ts, text, user=REQUESTER, dispatch=True):
    """A reply in a thread. With dispatch=False it's only added to the thread, as if its event hadn't arrived yet."""
    ts = slack.next_ts()
    event = {"type": "message", "channel": CHANNEL_ID, "user": user, "text": text, "ts": ts, "thread_ts": thread_ts}
    slack.threads[thread_ts].append(dict(event))
    if dispatch:
        desk.on_message(event)
    return event


def button_body(slack, thread_ts, action_id, user=REQUESTER, nth=0):
    """The payload Slack sends when `user` presses a button on the nth bot message in the thread that has buttons."""
    message = copy.deepcopy([m for m in slack.threads[thread_ts] if tickets.has_buttons(m.get("blocks"))][nth])
    buttons = next(b for b in message["blocks"] if b["type"] == "actions")["elements"]
    button = next(b for b in buttons if b["action_id"] == action_id)
    return {
        "user": {"id": user},
        "channel": {"id": CHANNEL_ID},
        "actions": [{"action_id": action_id, "value": button["value"]}],
        "container": {"message_ts": message["ts"]},
        "message": message,
    }


def click(desk, slack, thread_ts, action_id, user=REQUESTER, nth=0):
    body = button_body(slack, thread_ts, action_id, user, nth)
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


# --- Greetings, split posts and timing -------------------------------------------------------------------------

def test_greeting_asks_for_details_and_the_answer_opens_the_ticket_there(desk, slack, jira, ai):
    ts = post(desk, slack, "Hi team :wave:")
    assert jira.created == []
    assert last_post(slack)["text"].startswith(f"Hi <@{REQUESTER}>! What's going on?")

    details = reply(desk, slack, ts, "My VPN keeps disconnecting")
    [issue] = jira.created
    assert issue["summary"] == "My VPN keeps disconnecting"
    assert "p" + details["ts"].replace(".", "") in issue["description"]  # permalink points at the details
    assert last_post(slack)["thread_ts"] == ts and last_post(slack)["text"].startswith("Ticket IT-1 created")

    reply(desk, slack, ts, "Restarted, still failing")
    assert len(ai.histories) == 1  # the greeting prompt doesn't count towards the AI reply limit


@pytest.mark.parametrize("text", ["have a problem", "I have an issue", "it's not working", "Something is wrong"])
def test_posts_without_details_get_asked_for_them(desk, slack, jira, text):
    post(desk, slack, text)
    assert jira.created == []
    assert last_post(slack)["text"].startswith(f"Hi <@{REQUESTER}>! What's going on?")


def test_only_the_person_who_said_hi_can_open_a_ticket_from_the_greeting(desk, slack, jira):
    ts = post(desk, slack, "Hello, anyone around?")
    reply(desk, slack, ts, "What's up?", user=ENGINEER)
    assert jira.created == []


def test_split_posts_go_into_one_ticket(desk, slack, jira, ai):
    first = post(desk, slack, "My VPN keeps disconnecting")
    second = post(desk, slack, "It shows error 809")

    assert len(jira.created) == 1
    assert ("IT-1", "UREQ added in a separate Slack post:\n{noformat}\nIt shows error 809\n{noformat}") in jira.comments
    pointer = next(call for call in slack.calls_to("chat_postMessage") if call["thread_ts"] == second)
    assert pointer["text"].startswith(
        f"Added to ticket <https://jira.example/browse/IT-1|IT-1>: <https://slack.example/archives/{CHANNEL_ID}/p")
    assert last_post(slack)["thread_ts"] == first  # the AI answers in the ticket thread...
    assert ai.histories[-1][-1] == {"role": "user", "content": "It shows error 809"}  # ...with the new detail


def test_posts_outside_the_window_or_from_others_open_their_own_tickets(desk, slack, jira):
    post(desk, slack, "My VPN keeps disconnecting")
    post(desk, slack, "Printer is jammed", user=ENGINEER)
    slack._clock += 121
    post(desk, slack, "Now Outlook won't open")
    assert [issue["summary"] for issue in jira.created] == [
        "My VPN keeps disconnecting", "Printer is jammed", "Now Outlook won't open"]


def test_a_post_after_resolving_opens_a_new_ticket(desk, slack, jira):
    ts = post(desk, slack, "My VPN keeps disconnecting")
    click(desk, slack, ts, tickets.RESOLVE_ACTION)
    post(desk, slack, "Different thing: the printer is jammed")
    assert len(jira.created) == 2


def test_merging_can_be_turned_off(slack, jira, ai):
    desk = HelpDesk(make_config(MERGE_WINDOW_SECONDS="0"), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)
    post(desk, slack, "My VPN keeps disconnecting")
    post(desk, slack, "It shows error 809")
    assert len(jira.created) == 2


def test_replies_under_a_merged_post_are_copied_to_the_ticket(desk, slack, jira, ai):
    post(desk, slack, "My VPN keeps disconnecting")
    second = post(desk, slack, "It shows error 809")
    answers_before = len(ai.histories)
    reply(desk, slack, second, "Only on Wi-Fi")
    assert ("IT-1", "UREQ replied in Slack:\n{noformat}\nOnly on Wi-Fi\n{noformat}") in jira.comments
    assert len(ai.histories) == answers_before  # the conversation stays in the ticket thread


def test_reply_sent_while_the_ticket_is_being_created_is_not_lost(slack, jira):
    release = threading.Event()

    class SlowAssistant(FakeAssistant):
        def assess(self, text):
            assert release.wait(5)
            return super().assess(text)

    ai = SlowAssistant()
    desk = HelpDesk(make_config(), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)
    ts = slack.next_ts()
    top = {"type": "message", "channel": CHANNEL_ID, "user": REQUESTER, "text": "VPN won't connect", "ts": ts}
    slack.threads[ts] = [dict(top)]
    opener = threading.Thread(target=desk.on_message, args=(top,))
    opener.start()
    deadline = time.monotonic() + 5
    while ts not in desk._opening and time.monotonic() < deadline:
        time.sleep(0.01)

    early = reply(desk, slack, ts, "It started after the update", dispatch=False)
    replier = threading.Thread(target=desk.on_message, args=(early,))
    replier.start()
    time.sleep(0.2)
    assert jira.created == [] and jira.comments == []  # the reply is waiting for the ticket
    release.set()
    opener.join(5)
    replier.join(5)

    assert not opener.is_alive() and not replier.is_alive()
    assert ("IT-1", "UREQ replied in Slack:\n{noformat}\nIt started after the update\n{noformat}") in jira.comments
    assert len(ai.histories) == 1


def test_quick_replies_get_one_answer_covering_both(desk, slack, jira, ai):
    ts = post(desk, slack, "VPN won't connect")
    first = reply(desk, slack, ts, "Restarted, still failing", dispatch=False)
    second = reply(desk, slack, ts, "Also tried another network", dispatch=False)
    desk.on_message(first)
    desk.on_message(second)

    assert len(ai.histories) == 1
    said = [m["content"] for m in ai.histories[0] if m["role"] == "user"]
    assert said[-2:] == ["Restarted, still failing", "Also tried another network"]
    assert [body.split(":")[0] for key, body in jira.comments].count("UREQ replied in Slack") == 2


def test_resolve_passes_the_configured_transition_name(slack, jira, ai):
    desk = HelpDesk(make_config(JIRA_DONE_TRANSITION="Resolve this issue"), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.RESOLVE_ACTION)
    assert jira.preferred_transition == "Resolve this issue"


# --- Security --------------------------------------------------------------------------------------------------

def test_display_names_cant_inject_jira_markup(desk, slack, jira):
    slack.names[REQUESTER] = "[Reset your password here|https://evil.example]"
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "Still broken")

    first_line = jira.created[0]["description"].splitlines()[0]
    assert first_line == "Reported in Slack by Reset your password here https evil.example."
    comment_headers = [body.splitlines()[0] for _, body in jira.comments]
    assert "Reset your password here https evil.example replied in Slack:" in comment_headers
    assert not any(char in header for header in comment_headers for char in "[]|")


def test_ai_links_outside_the_allowlist_are_removed(slack, jira):
    ai = FakeAssistant(first_reply="Reset it [on the portal](https://evil.example/login), or see "
                                   "https://support.microsoft.com/vpn")
    desk = HelpDesk(make_config(AI_ALLOWED_LINK_DOMAINS="microsoft.com"), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)
    post(desk, slack, "VPN won't connect")
    rendered = json.dumps(last_post(slack)["blocks"])
    assert "evil.example" not in rendered
    assert "on the portal ([link removed])" in rendered and "https://support.microsoft.com/vpn" in rendered


def test_jira_errors_arent_shown_to_users(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")

    def broken(*args, **kwargs):
        raise JiraError("GET https://jira.internal/rest/api/2/issue/IT-1 returned 500: NullPointerException", 500)

    jira.get_issue = jira.search = broken
    click(desk, slack, ts, tickets.RESOLVE_ACTION)
    note = slack.calls_to("chat_postEphemeral")[-1]["text"]
    assert "IT-1" in note and "jira.internal" not in note and "NullPointer" not in note

    desk.on_mention({"channel": CHANNEL_ID, "user": REQUESTER, "text": f"<@{BOT_USER_ID}> report", "ts": "9.9"})
    report = last_post(slack)["text"]
    assert "couldn't build the report" in report and "jira.internal" not in report


# --- Buttons ---------------------------------------------------------------------------------------------------

def test_only_the_requester_can_use_the_buttons(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.RESOLVE_ACTION, user=ENGINEER)

    [ephemeral] = slack.calls_to("chat_postEphemeral")
    assert ephemeral["user"] == ENGINEER and "Only <@UREQ>" in ephemeral["text"]
    assert jira.issues["IT-1"]["status"]["statusCategory"]["key"] == "new"


def test_resolve_closes_the_ticket_and_removes_the_buttons(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    stale = button_body(slack, ts, tickets.RESOLVE_ACTION)  # the same button, still showing in another window
    click(desk, slack, ts, tickets.RESOLVE_ACTION)

    assert jira.issues["IT-1"]["status"]["statusCategory"]["key"] == "done"
    assert ":white_check_mark:" in last_post(slack)["text"]
    [update] = slack.calls_to("chat_update")
    assert update["text"].startswith("Ticket IT-1 created")  # keeps the text find_ticket relies on
    assert not tickets.has_buttons(update["blocks"])

    desk.on_button(stale, desk.resolve)
    assert "already closed" in slack.calls_to("chat_postEphemeral")[-1]["text"]


def test_closing_removes_the_buttons_from_every_message_in_the_thread(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "Still failing")  # the AI's follow-up has buttons too
    with_buttons = [m["ts"] for m in slack.threads[ts] if tickets.has_buttons(m.get("blocks"))]
    assert len(with_buttons) == 2

    click(desk, slack, ts, tickets.RESOLVE_ACTION, nth=1)  # press the newer message's button
    assert not any(tickets.has_buttons(m.get("blocks")) for m in slack.threads[ts])
    assert sorted(update["ts"] for update in slack.calls_to("chat_update")) == sorted(with_buttons)
    assert tickets.find_ticket(slack.threads[ts], BOT_USER_ID).key == "IT-1"  # ticket message text untouched


def test_one_failed_button_removal_doesnt_stop_the_rest(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "Still failing")
    first, second = [m for m in slack.threads[ts] if tickets.has_buttons(m.get("blocks"))]
    slack.fail_updates = {first["ts"]}

    click(desk, slack, ts, tickets.ESCALATE_ACTION, nth=1)
    assert "escalated" in jira.issues["IT-1"]["labels"]
    assert tickets.has_buttons(first["blocks"]) and not tickets.has_buttons(second["blocks"])


def test_resolve_explains_when_the_workflow_has_no_done_transition(desk, slack, jira):
    jira.can_transition = False
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.RESOLVE_ACTION)
    assert "couldn't find a way to close IT-1" in slack.calls_to("chat_postEphemeral")[-1]["text"]
    assert slack.calls_to("chat_update") == []


def test_escalate_labels_the_ticket_and_mentions_the_it_team(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    stale = button_body(slack, ts, tickets.ESCALATE_ACTION)
    click(desk, slack, ts, tickets.ESCALATE_ACTION)

    assert "escalated" in jira.issues["IT-1"]["labels"]
    assert "<!subteam^SIT> will take it from here" in last_post(slack)["text"]
    assert not any(tickets.has_buttons(m.get("blocks")) for m in slack.threads[ts])
    desk.on_button(stale, desk.escalate)
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


class SlackWithoutScopes:
    """A Slack client whose channel calls fail the way they do when the app lacks a scope."""

    def __init__(self, member=True):
        self.member = member

    def conversations_info(self, channel):
        if channel.startswith("G"):
            raise SlackApiError("missing scope", {"ok": False, "error": "missing_scope"})
        return {"channel": {"id": channel, "is_member": self.member}}

    def conversations_list(self, **kwargs):
        raise SlackApiError("missing scope", {"ok": False, "error": "missing_scope"})


def test_startup_explains_what_to_fix_when_the_channel_cant_be_read():
    assert check_channel_access(SlackWithoutScopes(member=True), "C0ABCDEF12") is True
    assert check_channel_access(SlackWithoutScopes(member=False), "C0ABCDEF12") is False
    with pytest.raises(ConfigError, match=r"can't read channel G0PRIVATE1 \(missing_scope\).*groups:history"):
        check_channel_access(SlackWithoutScopes(), "G0PRIVATE1")
    with pytest.raises(ConfigError, match=r"Couldn't list Slack channels to find #it-help \(missing_scope\)"):
        resolve_channel_id(SlackWithoutScopes(), "#it-help")


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
