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
from assistant import Assistant, CutOffAnswer
from bot import ITStaff, HelpDesk, check_channel_access, is_report_command, resolve_channel_id, schedule_jobs
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


def desk_with(slack, jira, ai, **overrides):
    return HelpDesk(make_config(**overrides), slack, jira, ai, BOT_USER_ID, CHANNEL_ID)


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


def test_requester_becomes_the_reporter_when_their_email_matches(desk, slack, jira):
    slack.emails[REQUESTER] = "Sam@Example.com"
    jira.accounts["sam@example.com"] = "acct-sam"
    desk.check_reporter_permission()
    post(desk, slack, "VPN won't connect")
    slack._clock += 121
    post(desk, slack, "Printer is jammed")

    assert [issue["reporter"] for issue in jira.created] == [{"accountId": "acct-sam"}] * 2
    assert jira.user_lookups == ["sam@example.com"]  # looked up once, then remembered
    assert "Reported in Slack by UREQ." in jira.created[0]["description"]


@pytest.mark.parametrize("setup", ["no_match", "no_slack_email", "no_permission", "turned_off", "lookup_fails"])
def test_bot_stays_the_reporter_when_the_requester_cant_be_matched(slack, jira, ai, setup):
    slack.emails[REQUESTER] = "sam@example.com"
    jira.accounts["sam@example.com"] = "acct-sam"
    if setup == "no_match":
        jira.accounts.clear()
    elif setup == "no_slack_email":
        slack.emails.clear()  # the app doesn't have users:read.email
    elif setup == "no_permission":
        jira.permissions.clear()
    elif setup == "lookup_fails":
        def broken(email):
            raise JiraError("GET /rest/api/2/user/search returned 503: unavailable", 503)
        jira.find_user = broken
    desk = desk_with(slack, jira, ai, JIRA_SET_REPORTER="false" if setup == "turned_off" else "true")
    desk.check_reporter_permission()
    post(desk, slack, "VPN won't connect")

    assert jira.created[0]["reporter"] is None
    if setup in ("no_permission", "turned_off", "no_slack_email"):
        assert jira.user_lookups == []  # nobody's email is sent to Jira for nothing


def test_jira_failure_is_reported_in_the_thread(desk, slack, jira):
    jira.fail_create = True
    ts = post(desk, slack, "Printer is jammed")

    message = last_post(slack)
    assert message["thread_ts"] == ts
    assert "couldn't create a Jira ticket" in message["text"]
    assert "<!subteam^SIT>" in message["text"]


def test_rejected_ticket_formatting_falls_back_to_plain_text(desk, slack, jira, ai, caplog):
    slack.fail_post = lambda post: post.get("blocks") is not None and post["text"].startswith("Ticket ")
    ts = post(desk, slack, "VPN won't connect")

    plain = last_post(slack)
    assert plain["thread_ts"] == ts and "blocks" not in plain
    assert plain["text"].startswith("Ticket IT-1 created") and "*restarting*" in plain["text"]
    assert "retrying as plain text" in caplog.text
    assert jira.comments == [("IT-1", "AI assistant replied in Slack:\n{noformat}\nTry **restarting** the VPN client.\n{noformat}")]

    reply(desk, slack, ts, "Still failing")  # the thread is still linked to the ticket
    assert ("IT-1", "UREQ replied in Slack:\n{noformat}\nStill failing\n{noformat}") in jira.comments


def test_ticket_that_cant_be_posted_in_slack_is_flagged_in_jira(desk, slack, jira, ai, caplog):
    slack.fail_post = lambda post: post["text"].startswith("Ticket ")
    post(desk, slack, "VPN won't connect")

    assert "Created IT-1 but couldn't tell UREQ in Slack" in caplog.text
    [(key, note)] = jira.comments
    assert key == "IT-1" and note.startswith("The bot couldn't post this ticket in Slack")
    assert "Try **restarting** the VPN client." in note  # the unposted AI suggestion, for IT
    assert "AI assistant replied in Slack" not in note

    slack.fail_post = None
    second = post(desk, slack, "It shows error 809")  # the requester's next post joins the ticket and links to it
    assert len(jira.created) == 1
    assert next(c for c in slack.calls_to("chat_postMessage") if c["thread_ts"] == second)["text"].startswith(
        "Added to ticket <https://jira.example/browse/IT-1|IT-1>")


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


COWORKER = "UCOWORKER"


@pytest.mark.parametrize("staff_setting", ["UENG", "<@UENG|alex>", "S0ITTEAM"])
def test_with_it_staff_set_only_their_replies_silence_the_ai(slack, jira, ai, staff_setting):
    slack.groups["S0ITTEAM"] = [ENGINEER]
    desk = desk_with(slack, jira, ai, IT_STAFF=staff_setting)
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "+1, same here", user=COWORKER)
    reply(desk, slack, ts, "Restarted it, still failing")
    assert len(ai.histories) == 1  # the coworker didn't stop the AI...
    assert all(m["content"] != "+1, same here" for m in ai.histories[0])  # ...and isn't part of its conversation

    reply(desk, slack, ts, "Looking into it now", user=ENGINEER)
    reply(desk, slack, ts, "Thanks!")
    assert len(ai.histories) == 1  # IT staff did
    mirrored = [body.splitlines()[0] for key, body in jira.comments if "replied in Slack" in body]
    assert mirrored.count("UCOWORKER replied in Slack:") == 1 and mirrored.count("UENG replied in Slack:") == 1


def test_it_staff_member_opening_their_own_ticket_still_gets_ai_help(slack, jira, ai):
    desk = desk_with(slack, jira, ai, IT_STAFF=ENGINEER)
    ts = post(desk, slack, "VPN won't connect", user=ENGINEER)
    reply(desk, slack, ts, "Restarted it, still failing", user=ENGINEER)
    assert len(ai.histories) == 1


def test_it_staff_groups_are_read_again_after_a_while(slack):
    now = [0.0]
    slack.groups["S0ITTEAM"] = [ENGINEER]
    staff = ITStaff(slack, users=["UBOSS"], groups=["S0ITTEAM"], clock=lambda: now[0])
    assert ENGINEER in staff and "UBOSS" in staff and COWORKER not in staff

    slack.groups["S0ITTEAM"] = [COWORKER]
    assert COWORKER not in staff  # still the cached list
    now[0] += ITStaff.REFRESH_SECONDS
    assert COWORKER in staff and ENGINEER not in staff
    assert len(slack.calls_to("usergroups_users_list")) == 2


def test_unreadable_it_staff_groups_stop_startup_with_a_fix_and_keep_the_last_list_later(slack, caplog):
    now = [0.0]
    slack.groups["S0ITTEAM"] = [ENGINEER]
    staff = ITStaff(slack, groups=["S0ITTEAM"], clock=lambda: now[0])
    staff.load()
    assert ENGINEER in staff

    slack.groups_missing_scope = True
    now[0] += ITStaff.REFRESH_SECONDS
    assert ENGINEER in staff
    assert "using the last list" in caplog.text
    with pytest.raises(ConfigError, match=r"\(missing_scope\).*usergroups:read"):
        ITStaff(slack, groups=["S0ITTEAM"]).load()
    ITStaff(slack, users=[ENGINEER]).load()  # nothing to read without groups


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


def test_a_cut_off_ai_answer_is_not_posted(slack, jira, caplog):
    class CutOffAssistant(FakeAssistant):
        def follow_up(self, issue_key, history):
            raise CutOffAnswer("gpt-6-luna ran out of room after 1500 tokens, so its answer is incomplete.")

    desk = HelpDesk(make_config(), slack, jira, CutOffAssistant(), BOT_USER_ID, CHANNEL_ID)
    ts = post(desk, slack, "VPN won't connect")
    posts_before = len(slack.calls_to("chat_postMessage"))
    reply(desk, slack, ts, "Still failing")

    assert len(slack.calls_to("chat_postMessage")) == posts_before  # nothing half-finished goes to Slack
    assert "Not posting a follow-up for IT-1" in caplog.text
    assert ("IT-1", "UREQ replied in Slack:\n{noformat}\nStill failing\n{noformat}") in jira.comments


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


def warnings_to(slack, user):
    return [call for call in slack.calls_to("chat_postEphemeral")
            if call["user"] == user and call["text"] == tickets.SECRET_WARNING]


@pytest.mark.parametrize("author", [REQUESTER, ENGINEER])
def test_secret_in_a_thread_reply_is_masked_and_its_author_warned_privately(desk, slack, jira, ai, author):
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "my password: Hunter2! still fails", user=author)

    [warning] = warnings_to(slack, author)
    assert warning["thread_ts"] == ts
    assert not any("Hunter2" in body for _, body in jira.comments)
    assert not any("Hunter2" in m["content"] for history in ai.histories for m in history)
    assert not any("Hunter2" in call["text"] for call in slack.calls_to("chat_postMessage"))


def test_secret_in_an_extra_post_is_masked_and_its_author_warned_privately(desk, slack, jira):
    post(desk, slack, "VPN won't connect")
    second = post(desk, slack, "token: xoxb-1234567890-abcdefghij")

    [warning] = warnings_to(slack, REQUESTER)
    assert warning["thread_ts"] == second
    assert not any("xoxb-1234567890" in body for _, body in jira.comments)


def test_secret_in_a_reply_under_an_extra_post_is_masked_and_its_author_warned(desk, slack, jira):
    post(desk, slack, "VPN won't connect")
    second = post(desk, slack, "It shows error 809")
    reply(desk, slack, second, "pin=4321 is what I typed")

    [warning] = warnings_to(slack, REQUESTER)
    assert warning["thread_ts"] == second
    assert ("IT-1", "UREQ replied in Slack:\n{noformat}\npin=[redacted] is what I typed\n{noformat}") in jira.comments


def test_no_secret_no_warning(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "my password is expired, I think")
    unrelated = slack.next_ts()
    slack.threads[unrelated] = [{"ts": unrelated, "user": ENGINEER, "text": "Lunch?"}]
    reply(desk, slack, unrelated, "password: not-for-the-bot", user=REQUESTER)  # not a ticket thread: none of our business
    assert slack.calls_to("chat_postEphemeral") == []


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


# --- Ticket limit ----------------------------------------------------------------------------------------------

def test_ticket_limit_blocks_the_next_ticket_until_an_hour_has_passed(slack, jira, ai):
    desk = desk_with(slack, jira, ai, MAX_TICKETS_PER_HOUR="2", MERGE_WINDOW_SECONDS="0")
    post(desk, slack, "VPN won't connect")
    post(desk, slack, "Printer is jammed")
    blocked = post(desk, slack, "Outlook keeps crashing")

    assert len(jira.created) == 2 and len(ai.assessed) == 2  # the blocked post never reached the AI
    assert last_post(slack)["thread_ts"] == blocked
    assert last_post(slack)["text"].startswith(":hourglass: You've opened 2 tickets in the last hour")

    slack._clock += 3600
    post(desk, slack, "Outlook keeps crashing")
    assert len(jira.created) == 3


def test_ticket_limit_zero_means_no_limit(slack, jira, ai):
    desk = desk_with(slack, jira, ai, MAX_TICKETS_PER_HOUR="0", MERGE_WINDOW_SECONDS="0")
    for n in range(12):
        post(desk, slack, f"Printer number {n} is jammed")
    assert len(jira.created) == 12


def test_merged_posts_and_greetings_dont_count_towards_the_limit(slack, jira, ai):
    desk = desk_with(slack, jira, ai, MAX_TICKETS_PER_HOUR="2")
    post(desk, slack, "VPN won't connect")             # ticket 1
    post(desk, slack, "It shows error 809")            # added to ticket 1: doesn't count
    slack._clock += 121
    greeting = post(desk, slack, "Hi team")            # greeting prompt: doesn't count
    reply(desk, slack, greeting, "Printer is jammed")  # ticket 2
    slack._clock += 121
    post(desk, slack, "Outlook keeps crashing")        # third ticket within the hour: blocked

    assert len(jira.created) == 2
    assert last_post(slack)["text"].startswith(":hourglass:")


def test_failed_jira_creates_dont_count_towards_the_limit(slack, jira, ai):
    desk = desk_with(slack, jira, ai, MAX_TICKETS_PER_HOUR="1")
    jira.fail_create = True
    post(desk, slack, "VPN won't connect")
    jira.fail_create = False
    post(desk, slack, "VPN still won't connect")
    assert len(jira.created) == 1


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


# --- Jira to Slack ---------------------------------------------------------------------------------------------

def jira_notes(slack, thread_ts):
    return [m["text"] for m in slack.threads[thread_ts] if tickets.is_jira_note(m)]


def test_status_changes_made_in_jira_are_posted_in_the_thread_once(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    jira.change_status("IT-1", "In Progress")
    desk.sync_from_jira()
    desk.sync_from_jira()  # nothing new the second time

    assert jira_notes(slack, ts) == [
        ":arrows_counterclockwise: Alex Kim moved <https://jira.example/browse/IT-1|IT-1> from *To Do* to "
        "*In Progress* in Jira."]
    assert tickets.has_buttons(slack.threads[ts][1]["blocks"])  # still open, so the buttons stay
    assert 'labels = "slack-it-bot"' in jira.last_jql and "updated >= -5m" in jira.last_jql


def test_closing_in_jira_removes_the_buttons_and_ends_the_merge_window(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    reply(desk, slack, ts, "Still failing")
    jira.change_status("IT-1", "In Progress")
    jira.change_status("IT-1", "Done", "done", by="acct-jo", who="Jo Park")
    desk.sync_from_jira()

    [note] = jira_notes(slack, ts)
    assert note.splitlines() == [
        ":arrows_counterclockwise: Alex Kim moved <https://jira.example/browse/IT-1|IT-1> from *To Do* to *In Progress* in Jira.",
        ":arrows_counterclockwise: Jo Park moved <https://jira.example/browse/IT-1|IT-1> from *In Progress* to *Done* in Jira.",
        "This ticket is closed now. If you still need help, post a new message in the channel.",
    ]
    assert not any(tickets.has_buttons(m.get("blocks")) for m in slack.threads[ts])
    post(desk, slack, "Now the printer is jammed")  # within the merge window, but the old ticket is closed
    assert len(jira.created) == 2


def test_the_bots_own_changes_arent_repeated(desk, slack, jira):
    ts = post(desk, slack, "VPN won't connect")
    click(desk, slack, ts, tickets.RESOLVE_ACTION)
    desk.sync_from_jira()
    assert jira_notes(slack, ts) == []
    assert ":white_check_mark:" in last_post(slack)["text"]  # the bot's own announcement is enough


def test_after_a_restart_older_changes_arent_posted_again(slack, jira, ai):
    ts = post(desk_with(slack, jira, ai), slack, "VPN won't connect")
    jira.change_status("IT-1", "In Progress")
    restarted = desk_with(slack, jira, ai)
    restarted.sync_from_jira()
    assert jira_notes(slack, ts) == []  # it can't know whether the previous run relayed this

    jira.change_status("IT-1", "Waiting for user")
    restarted.sync_from_jira()
    assert len(jira_notes(slack, ts)) == 1 and "to *Waiting for user*" in jira_notes(slack, ts)[0]


def test_greeting_threads_are_found_from_the_reply_link(desk, slack, jira):
    ts = post(desk, slack, "Hi team")
    reply(desk, slack, ts, "My VPN keeps disconnecting")
    assert "?thread_ts=" + ts in jira.created[0]["description"]
    jira.change_status("IT-1", "In Progress")
    desk.sync_from_jira()
    assert len(jira_notes(slack, ts)) == 1


@pytest.mark.parametrize("description", [
    "Edited by IT: no link here",
    "Reported in Slack by Sam.\nSlack thread: https://slack.example/archives/COTHER/p1001000100\n\n{noformat}\nx\n{noformat}",
    "Reported in Slack by Sam.\n\n{noformat}\nSlack thread: https://slack.example/archives/CITHELP01/p1001000100\n{noformat}",
])
def test_jira_changes_are_only_posted_to_the_tickets_own_thread(desk, slack, jira, description, caplog):
    ts = post(desk, slack, "VPN won't connect")
    jira.issues["IT-1"]["description"] = description
    jira.change_status("IT-1", "In Progress")
    desk.sync_from_jira()
    assert jira_notes(slack, ts) == []
    assert "doesn't link to a thread in the IT channel" in caplog.text


def test_a_link_to_another_tickets_thread_is_ignored(desk, slack, jira, caplog):
    first = post(desk, slack, "VPN won't connect")
    slack._clock += 121
    post(desk, slack, "Printer is jammed")
    jira.issues["IT-2"]["description"] = jira.issues["IT-1"]["description"]  # points at IT-1's thread
    jira.change_status("IT-2", "In Progress")
    desk.sync_from_jira()
    assert jira_notes(slack, first) == []
    assert "isn't its ticket thread" in caplog.text


def test_jira_notes_dont_use_up_ai_replies_or_reach_the_ai(slack, jira, ai):
    desk = desk_with(slack, jira, ai, MAX_AI_FOLLOW_UPS="1")
    ts = post(desk, slack, "VPN won't connect")
    jira.change_status("IT-1", "In Progress", who="<!channel> *Alex*")
    desk.sync_from_jira()
    assert jira_notes(slack, ts)[0].startswith(":arrows_counterclockwise: &lt;!channel&gt; *Alex* moved")

    reply(desk, slack, ts, "Still failing")
    assert len(ai.histories) == 1
    assert not any("moved" in m["content"] for m in ai.histories[0])


def test_jira_sync_survives_jira_and_slack_errors(desk, slack, jira, caplog):
    ts = post(desk, slack, "VPN won't connect")
    jira.change_status("IT-1", "In Progress")

    def broken(*args, **kwargs):
        raise JiraError("GET /rest/api/3/search/jql returned 503: unavailable", 503)

    working_search, jira.search = jira.search, broken
    desk.sync_from_jira()
    assert "Couldn't check Jira for changes" in caplog.text

    jira.search = working_search
    slack.fail_post = lambda post: tickets.is_jira_note(post)
    desk.sync_from_jira()
    assert "Couldn't relay the Jira changes to IT-1 to Slack" in caplog.text
    assert jira_notes(slack, ts) == []


@pytest.mark.parametrize("description, expected", [
    ("Reported in Slack by Sam.\nSlack thread: https://x.slack.com/archives/C0AB12/p1700000000000100", ("C0AB12", "1700000000.000100")),
    ("Slack thread: https://x.slack.com/archives/G0AB12/p1700000001000200?thread_ts=1700000000.000100&cid=G0AB12",
     ("G0AB12", "1700000000.000100")),
    ("Slack thread: https://x.slack.com/archives/C0AB12/p1700000001000200?thread_ts=bogus", ("C0AB12", "1700000001.000200")),
    ("Slack thread: http://x.slack.com/archives/C0AB12/p1700000000000100", None),
    ("", None),
    (None, None),
])
def test_slack_thread_in(description, expected):
    assert tickets.slack_thread_in(description) == expected


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


def test_schedule_has_the_weekly_report_and_the_jira_sync(desk):
    scheduler = schedule_jobs(desk, make_config(REPORT_DAY="fri", REPORT_HOUR="16", REPORT_TIMEZONE="America/New_York",
                                                JIRA_SYNC_SECONDS="45"))
    jobs = {job.id: job for job in scheduler.get_jobs()}
    fields = {f.name: str(f) for f in jobs["weekly_report"].trigger.fields}
    assert fields["day_of_week"] == "fri" and fields["hour"] == "16"
    assert str(jobs["weekly_report"].trigger.timezone) == "America/New_York"
    assert jobs["jira_sync"].trigger.interval.total_seconds() == 45 and jobs["jira_sync"].max_instances == 1


def test_schedule_leaves_out_what_is_turned_off(desk):
    # With the report off, its time zone isn't checked, so a bad one mustn't break the scheduler
    config = make_config(REPORT_ENABLED="false", REPORT_TIMEZONE="Mars/Base", JIRA_SYNC_SECONDS="0")
    assert schedule_jobs(desk, config).get_jobs() == []


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
