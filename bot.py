"""Slack + Jira IT help desk bot.

New posts in the IT channel become Jira tickets. Greetings get asked for details first, and quick extra posts from
the same person join their last ticket. The bot replies in the thread with the ticket link and, if OpenAI is
configured, first troubleshooting steps. It keeps helping the requester in the thread, copies the thread into Jira
as comments, and lets the requester close or escalate the ticket with buttons.
"""

import logging
import re
import threading
from collections import OrderedDict, deque
from functools import lru_cache

from apscheduler.schedulers.background import BackgroundScheduler
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

import reports
import tickets
from assistant import Assistant, CutOffAnswer, is_small_talk
from config import ConfigError, load_config
from jira_client import JiraClient, JiraError, is_done, noformat, safe_inline
from text_utils import redact_secrets, slack_to_plain, to_slack_mrkdwn

logger = logging.getLogger("it_bot")

# Message subtypes that still mean "a person posted something". Plain messages have no subtype.
HANDLED_SUBTYPES = {None, "file_share", "thread_broadcast"}


def is_report_command(text, bot_user_id):
    """True for "@bot report" (or "metrics"/"stats"), but not for "@bot I want to report a problem"."""
    pattern = rf"\s*<@{bot_user_id}>\s*(?:report|metrics|stats)[\s.!?]*"
    return re.fullmatch(pattern, text, re.IGNORECASE) is not None


class RecentKeys:
    """Remembers recently seen keys, so an event Slack delivers twice doesn't open two tickets."""

    def __init__(self, size=1000):
        self._keys = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def first_time(self, key):
        with self._lock:
            if key in self._keys:
                return False
            self._keys[key] = True
            if len(self._keys) > self._size:
                self._keys.popitem(last=False)
            return True


class KeyedLocks:
    """A fixed pool of locks picked by key, so work on the same Slack thread (or user) runs one at a time without
    keeping a lock per key forever. Unrelated keys occasionally share a lock, which only costs a short wait."""

    def __init__(self, size=64):
        self._locks = [threading.Lock() for _ in range(size)]

    def __call__(self, key):
        return self._locks[hash(key) % len(self._locks)]


class HelpDesk:
    def __init__(self, config, slack, jira, assistant, bot_user_id, channel_id):
        self.config = config
        self.slack = slack  # a slack_sdk WebClient
        self.jira = jira
        self.assistant = assistant
        self.bot_user_id = bot_user_id
        self.channel_id = channel_id
        self._seen = RecentKeys()
        self.user_name = lru_cache(maxsize=1024)(self._lookup_user_name)
        # Locking order is always user lock, then thread lock.
        self._user_locks = KeyedLocks()
        self._thread_locks = KeyedLocks()
        self._state_lock = threading.Lock()
        self._opening = {}  # thread ts -> Event, for top-level posts still being turned into tickets
        self._recent_tickets = {}  # user ID -> (TicketRef, ts of their latest post on it), for merging split posts
        self._tickets_opened = {}  # user ID -> deque of when they opened tickets in the last hour, for the limit

    def register(self, app):
        @app.event("message")
        def _on_message(event):
            self.on_message(event)

        @app.event("app_mention")
        def _on_mention(event):
            self.on_mention(event)

        @app.action(tickets.RESOLVE_ACTION)
        def _on_resolve(ack, body):
            ack()
            self.on_button(body, self.resolve)

        @app.action(tickets.ESCALATE_ACTION)
        def _on_escalate(ack, body):
            ack()
            self.on_button(body, self.escalate)

    # --- Slack events ------------------------------------------------------------------------------------------

    def on_message(self, event):
        if event.get("channel") != self.channel_id or event.get("subtype") not in HANDLED_SUBTYPES:
            return
        if event.get("bot_id") or not event.get("user"):
            return
        if is_report_command(event.get("text", ""), self.bot_user_id):
            return  # on_mention answers these
        if not self._seen.first_time(event["ts"]):
            return
        thread_ts = event.get("thread_ts")
        if thread_ts and thread_ts != event["ts"]:
            self.on_thread_reply(event)
        else:
            self.on_top_level_post(event)

    def on_mention(self, event):
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        if is_report_command(event.get("text", ""), self.bot_user_id):
            self.slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=self.report_text())
        elif channel != self.channel_id:
            self.slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"Hi <@{event['user']}>! Post IT problems in <#{self.channel_id}> and I'll open a Jira ticket "
                     "and help troubleshoot. Mention me with `report` for a summary of the last 7 days.",
            )
        # Mentions in the IT channel are ordinary posts, and on_message handles those.

    def on_button(self, body, action):
        ticket = tickets.TicketRef.from_value(body["actions"][0]["value"])
        user = body["user"]["id"]
        channel = body["channel"]["id"]
        if user != ticket.creator:
            self._ephemeral(channel, user, ticket.thread_ts,
                            f"Only <@{ticket.creator}> can use these buttons. IT staff can update {ticket.key} in Jira.")
            return
        try:
            announcement, problem = action(ticket, user)
        except JiraError as exc:
            logger.error("Jira rejected a button action on %s: %s", ticket.key, exc)
            self._ephemeral(channel, user, ticket.thread_ts, f":warning: Jira didn't accept that change to "
                                                             f"{ticket.key}. The IT team can see the details in the bot's log.")
            return
        if problem:
            self._ephemeral(channel, user, ticket.thread_ts, problem)
            return
        self.slack.chat_postMessage(channel=channel, thread_ts=ticket.thread_ts, text=announcement)
        self._remove_buttons(channel, ticket.thread_ts)

    def _remove_buttons(self, channel, thread_ts):
        """Take the buttons off every bot message in the thread, so a closed or escalated ticket no longer looks
        like it's waiting for a click. Message text is kept, because find_ticket() relies on it."""
        try:
            messages = self.slack.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
        except SlackApiError as exc:
            logger.warning("Couldn't read thread %s to remove its buttons: %s", thread_ts, exc)
            return
        for message in messages:
            if message.get("user") != self.bot_user_id or not tickets.has_buttons(message.get("blocks")):
                continue
            try:
                self.slack.chat_update(channel=channel, ts=message["ts"], text=message.get("text", ""),
                                       blocks=tickets.without_buttons(message["blocks"]))
            except SlackApiError as exc:
                logger.warning("Couldn't remove the buttons from message %s: %s", message["ts"], exc)

    # --- Ticket lifecycle --------------------------------------------------------------------------------------

    def on_top_level_post(self, event):
        ts, user = event["ts"], event["user"]
        opened = threading.Event()
        with self._state_lock:
            self._opening[ts] = opened  # replies in this thread wait for it (see on_thread_reply)
        try:
            with self._user_locks(user):
                recent = self._recent_ticket(user, ts)
                if recent:
                    self._add_post_to_ticket(recent, event)
                    return
                with self._thread_locks(ts):
                    ticket = self._handle_new_post(event)
                if ticket:
                    self._remember(user, ticket, ts)
        finally:
            with self._state_lock:
                self._opening.pop(ts, None)
            opened.set()

    def _handle_new_post(self, event):
        """Open a ticket for a new post, or ask for details if it's only a greeting. Returns the TicketRef or None."""
        channel, ts, user = event["channel"], event["ts"], event["user"]
        files = event.get("files") or []
        plain = slack_to_plain(event.get("text", ""), self.user_name)
        if plain and not files and is_small_talk(plain):
            self.slack.chat_postMessage(channel=channel, thread_ts=ts, text=tickets.details_prompt_text(user))
            return None
        return self._create_ticket(channel, ts, user, event.get("text", ""), files, ts)

    def _add_post_to_ticket(self, ticket, event):
        """A post moments after the same person's last ticket is probably more of the same story, so add it there."""
        channel, ts, user = event["channel"], event["ts"], event["user"]
        text = self._clean_and_warn(event, ts)
        files = event.get("files") or []
        if not text and not files:
            return
        self._remember(user, ticket, ts)
        self._comment(ticket.key, f"{self._jira_name(user)} added in a separate Slack post:\n"
                      + noformat(text or f"(No text. Attachments in Slack: {len(files)})"))
        thread_link = self._permalink(channel, ticket.thread_ts) or self.jira.browse_url(ticket.key)
        self.slack.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=tickets.linked_text(ticket.key, self.jira.browse_url(ticket.key), thread_link),
            unfurl_links=False,
            unfurl_media=False,
        )
        if text:
            with self._thread_locks(ticket.thread_ts):
                messages = self.slack.conversations_replies(channel=channel, ts=ticket.thread_ts, limit=200)["messages"]
                self._ai_follow_up(channel, ticket, messages, text, ts)

    def _recent_ticket(self, user, ts):
        """The user's ticket from moments ago, if a new post at `ts` falls inside the merge window."""
        if self.config.merge_window_seconds <= 0:
            return None
        with self._state_lock:
            ticket, last_ts = self._recent_tickets.get(user, (None, 0.0))
        return ticket if ticket and float(ts) - last_ts <= self.config.merge_window_seconds else None

    def _remember(self, user, ticket, ts):
        with self._state_lock:
            self._recent_tickets[user] = (ticket, float(ts))

    def _forget(self, ticket):
        """A closed ticket shouldn't soak up the requester's next post, which is probably a new problem."""
        with self._state_lock:
            recent, _ = self._recent_tickets.get(ticket.creator, (None, 0.0))
            if recent and recent.key == ticket.key:
                del self._recent_tickets[ticket.creator]

    def _over_ticket_limit(self, user, ts):
        """True if `user` already opened MAX_TICKETS_PER_HOUR tickets in the hour before `ts` (0 means no limit)."""
        limit = self.config.max_tickets_per_hour
        if limit <= 0:
            return False
        with self._state_lock:
            opened = self._tickets_opened.get(user)
            while opened and float(ts) - opened[0] >= 3600:
                opened.popleft()
            return bool(opened) and len(opened) >= limit

    def _count_ticket(self, user, ts):
        if self.config.max_tickets_per_hour > 0:
            with self._state_lock:
                self._tickets_opened.setdefault(user, deque()).append(float(ts))

    def _create_ticket(self, channel, thread_ts, requester, raw_text, files, source_ts):
        """Create the Jira ticket and post it in `thread_ts`. `source_ts` is the message with the details."""
        plain = slack_to_plain(raw_text, self.user_name)
        text = redact_secrets(plain)
        if not text and not files:
            return None
        text = text or "(No text. See the attachments in Slack.)"
        if self._over_ticket_limit(requester, source_ts):           # checked before the AI call, so it costs nothing
            limit = self.config.max_tickets_per_hour
            logger.warning("%s has opened %s tickets in the last hour; not opening another", requester, limit)
            self.slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":hourglass: You've opened {limit} tickets in the last hour, which is this help desk's limit. "
                     "Please add details to one of your open tickets, or try again later.",
            )
            return None

        triage, ai_reply = self.assistant.assess(text)
        try:
            key = self.jira.create_issue(
                project_key=self.config.jira_project_key,
                issue_type=self.config.jira_issue_type if triage.kind == "incident" else self.config.jira_request_issue_type,
                summary=triage.summary,
                description=self._description(requester, channel, source_ts, text, len(files)),
                labels=tickets.ticket_labels(self.config.jira_label, triage),
                priority=triage.priority if self.config.jira_set_priority else None,
            )
        except JiraError as exc:
            logger.error("Couldn't create a Jira ticket for message %s: %s", source_ts, exc)
            who = self.config.escalation_mention or "The IT team"
            self.slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: I couldn't create a Jira ticket for this. {who} will need to pick it up manually.",
            )
            return None

        logger.info("Opened %s (%s, %s) for %s", key, triage.kind, triage.category, requester)
        self._count_ticket(requester, source_ts)
        ticket = tickets.TicketRef(key=key, creator=requester, thread_ts=thread_ts)
        reply = self._format_ai(ai_reply) if ai_reply else ""
        self.slack.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=tickets.ticket_text(ticket, triage, reply),
            blocks=tickets.ticket_blocks(ticket, self.jira.browse_url(key), triage, reply, secret_removed=text != plain),
            unfurl_links=False,
            unfurl_media=False,
        )
        if ai_reply:
            self._comment(key, "AI assistant replied in Slack:\n" + noformat(ai_reply))
        return ticket

    def on_thread_reply(self, event):
        channel, thread_ts, author = event["channel"], event["thread_ts"], event["user"]
        self._wait_until_opened(thread_ts)
        with self._thread_locks(thread_ts):
            messages = self.slack.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
            ticket = tickets.find_ticket(messages, self.bot_user_id)
            if ticket is None:
                self._reply_without_ticket(event, messages)
                return
            text = self._clean_and_warn(event, thread_ts)
            if text:
                self._comment(ticket.key, f"{self._jira_name(author)} replied in Slack:\n" + noformat(text))
            if author == ticket.creator:
                self._ai_follow_up(channel, ticket, messages, text, event["ts"])

    def _wait_until_opened(self, thread_ts):
        """If the thread's first post is still becoming a ticket, wait for it so this reply isn't missed."""
        with self._state_lock:
            opened = self._opening.get(thread_ts)
        if opened and not opened.wait(timeout=30):
            logger.warning("Gave up waiting for the ticket in thread %s", thread_ts)

    def _reply_without_ticket(self, event, messages):
        """Details sent after the bot asked for them open the ticket. Replies under a post that was added to a
        ticket elsewhere are copied to that ticket. Anything else isn't the bot's business."""
        channel, thread_ts, author = event["channel"], event["thread_ts"], event["user"]
        if tickets.awaiting_details(messages, self.bot_user_id, author):
            ticket = self._create_ticket(channel, thread_ts, author, event.get("text", ""),
                                         event.get("files") or [], event["ts"])
            if ticket:
                self._remember(author, ticket, event["ts"])
            return
        key = tickets.find_linked_ticket(messages, self.bot_user_id)
        if not key:
            return
        text = self._clean_and_warn(event, thread_ts)
        if text:
            self._comment(key, f"{self._jira_name(author)} replied in Slack:\n" + noformat(text))

    def _ai_follow_up(self, channel, ticket, messages, new_text, new_ts):
        """Post the AI's next reply in the ticket thread, unless it should stay out of the way.

        `messages` is the ticket thread; `new_text`/`new_ts` is the requester's latest message, which may be in it
        or (for a merged extra post) elsewhere.
        """
        if not self.assistant.enabled or not new_text:
            return
        if tickets.human_took_over(messages, ticket.creator, self.bot_user_id):
            return  # someone from IT has joined, so the AI stays out of the way
        if tickets.has_later_message_from(messages, ticket.creator, new_ts):
            return  # they've already said more, and answering that message covers this one too
        try:
            fields = self.jira.get_issue(ticket.key)
        except JiraError as exc:
            logger.warning("Couldn't read %s from Jira, so not replying: %s", ticket.key, exc)
            return
        labels = fields.get("labels") or []
        if is_done(fields) or tickets.ESCALATED_LABEL in labels or "incident" not in labels:
            return

        replies_so_far = tickets.ai_reply_count(messages, self.bot_user_id)
        if replies_so_far >= self.config.max_ai_follow_ups:
            if replies_so_far == self.config.max_ai_follow_ups:  # say this once, then stay quiet
                self._post_reply(channel, ticket, "I've suggested everything I can for this one. "
                                                  "Press *Escalate to IT* and a person will take over.")
            return

        history = tickets.conversation_history(messages, ticket.creator, self.bot_user_id, self._clean)
        if not any(m.get("ts") == new_ts for m in messages):
            history.append({"role": "user", "content": new_text})  # a merged extra post, or not indexed by Slack yet
        try:
            answer = self.assistant.follow_up(ticket.key, history)
        except CutOffAnswer as exc:
            logger.warning("Not posting a follow-up for %s: %s", ticket.key, exc)
            return
        except Exception:
            logger.exception("AI follow-up failed for %s", ticket.key)
            return
        if answer:
            self._post_reply(channel, ticket, self._format_ai(answer))
            self._comment(ticket.key, "AI assistant replied in Slack:\n" + noformat(answer))

    def resolve(self, ticket, user):
        """Close the ticket in Jira. Returns (thread announcement, None) or (None, reason it wasn't changed)."""
        if is_done(self.jira.get_issue(ticket.key)):
            return None, f"{ticket.key} is already closed."
        if not self.jira.transition_to_done(ticket.key, self.config.jira_done_transition):
            logger.warning("No suitable Done transition for %s. Set JIRA_DONE_TRANSITION to the exact name of the "
                           "workflow transition that resolves tickets.", ticket.key)
            return None, f"I couldn't find a way to close {ticket.key} in its Jira workflow. IT staff can close it in Jira."
        self._forget(ticket)
        self._comment(ticket.key, f"{self._jira_name(user)} marked this resolved from Slack.")
        return f":white_check_mark: <{self.jira.browse_url(ticket.key)}|{ticket.key}> is closed. Glad it's sorted!", None

    def escalate(self, ticket, user):
        """Flag the ticket for a person. Returns (thread announcement, None) or (None, reason it wasn't changed)."""
        fields = self.jira.get_issue(ticket.key)
        if is_done(fields):
            return None, f"{ticket.key} is already closed. Post a new message in the channel if you still need help."
        if tickets.ESCALATED_LABEL in (fields.get("labels") or []):
            return None, f"{ticket.key} has already been escalated."
        self.jira.add_labels(ticket.key, [tickets.ESCALATED_LABEL])
        self._comment(ticket.key, f"{self._jira_name(user)} escalated this from Slack. The AI assistant has stopped replying.")
        who = self.config.escalation_mention or "The IT team"
        return f":sos: <{self.jira.browse_url(ticket.key)}|{ticket.key}> has been escalated. {who} will take it from here.", None

    # --- Reports -----------------------------------------------------------------------------------------------

    def report_text(self):
        try:
            return reports.build_report(self.jira, self.config.jira_project_key, self.config.jira_label)
        except JiraError as exc:
            logger.error("Couldn't build the report: %s", exc)
            return (":warning: I couldn't build the report because Jira didn't respond as expected. "
                    "The IT team can see the details in the bot's log.")

    def post_weekly_report(self):
        self.slack.chat_postMessage(channel=self.channel_id, text=self.report_text())

    # --- Helpers -----------------------------------------------------------------------------------------------

    def _clean(self, text):
        return redact_secrets(slack_to_plain(text, self.user_name))

    def _clean_and_warn(self, event, thread_ts):
        """The message's text with secrets masked. If anything was masked, privately ask its author to delete the
        message and change the secret: the bot keeps it out of Jira and the AI, but it's still visible in Slack."""
        plain = slack_to_plain(event.get("text", ""), self.user_name)
        text = redact_secrets(plain)
        if text != plain:
            self._ephemeral(event["channel"], event["user"], thread_ts, tickets.SECRET_WARNING)
        return text

    def _jira_name(self, user_id):
        """A Slack display name made safe for Jira, since anyone can set their display name to Jira markup."""
        return safe_inline(self.user_name(user_id))

    def _format_ai(self, text):
        return to_slack_mrkdwn(text, self.config.ai_allowed_link_domains)

    def _description(self, requester, channel, ts, text, file_count):
        lines = [f"Reported in Slack by {self._jira_name(requester)}."]
        permalink = self._permalink(channel, ts)
        if permalink:
            lines.append(f"Slack thread: {permalink}")
        if file_count:
            lines.append(f"Attachments in Slack: {file_count}")
        return "\n".join(lines) + "\n\n" + noformat(text)

    def _post_reply(self, channel, ticket, mrkdwn):
        self.slack.chat_postMessage(
            channel=channel,
            thread_ts=ticket.thread_ts,
            text=mrkdwn,
            blocks=tickets.reply_blocks(mrkdwn, ticket),
            unfurl_links=False,
            unfurl_media=False,
        )

    def _comment(self, key, body):
        try:
            self.jira.add_comment(key, body)
        except JiraError as exc:
            logger.warning("Couldn't add a comment to %s: %s", key, exc)

    def _ephemeral(self, channel, user, thread_ts, text):
        """A message in the thread that only `user` can see."""
        self.slack.chat_postEphemeral(channel=channel, user=user, thread_ts=thread_ts, text=text)

    def _permalink(self, channel, ts):
        try:
            return self.slack.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
        except SlackApiError as exc:
            logger.warning("Couldn't get a permalink for %s: %s", ts, exc)
            return None

    def _lookup_user_name(self, user_id):
        try:
            user = self.slack.users_info(user=user_id)["user"]
        except SlackApiError as exc:
            logger.warning("Couldn't look up Slack user %s: %s", user_id, exc)
            return user_id
        profile = user.get("profile") or {}
        return profile.get("display_name") or user.get("real_name") or user.get("name") or user_id


def resolve_channel_id(slack, channel):
    """IT_CHANNEL can be a channel ID (C0123...) or a public channel's name, with or without the #."""
    if re.fullmatch(r"[CG][A-Z0-9]{6,}", channel):
        return channel
    name = channel.lstrip("#").lower()
    cursor = None
    while True:
        kwargs = {"types": "public_channel", "exclude_archived": True, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            page = slack.conversations_list(**kwargs)
        except SlackApiError as exc:
            raise ConfigError(f"Couldn't list Slack channels to find #{name} ({_slack_error(exc)}). Check that the "
                              "app has the channels:read scope, or set IT_CHANNEL to the channel ID.") from None
        for found in page["channels"]:
            if found["name"] == name:
                return found["id"]
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            raise ConfigError(f"There's no public channel named #{name}. Check IT_CHANNEL, "
                              "or set it to the channel ID if the channel is private.")


def check_channel_access(slack, channel_id):
    """Fail at startup, with a fix, if the bot can't read the IT channel. Returns whether it's a member yet."""
    try:
        channel = slack.conversations_info(channel=channel_id)["channel"]
    except SlackApiError as exc:
        raise ConfigError(
            f"The bot can't read channel {channel_id} ({_slack_error(exc)}). If it's a private channel, add the "
            "groups:read and groups:history scopes and the message.groups event to the Slack app, reinstall the app, "
            "and invite the bot to the channel.") from None
    return bool(channel.get("is_member"))


def _slack_error(exc):
    response = getattr(exc, "response", None)
    return (response.get("error") if response is not None else None) or "unknown error"


def schedule_weekly_report(desk, config):
    """A scheduler (not yet started) that posts the report to the IT channel once a week."""
    scheduler = BackgroundScheduler(timezone=config.report_timezone)
    scheduler.add_job(desk.post_weekly_report, "cron", day_of_week=config.report_day, hour=config.report_hour)
    return scheduler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(f"Configuration problem: {exc}") from None

    jira = JiraClient(config.jira_base_url, config.jira_api_token, email=config.jira_email)
    try:
        jira_user = jira.check_connection(config.jira_project_key)
    except JiraError as exc:
        raise SystemExit(f"Couldn't reach Jira project {config.jira_project_key}: {exc}") from None
    logger.info("Connected to Jira as %s, project %s", jira_user, config.jira_project_key)

    assistant = Assistant(api_key=config.openai_api_key, model=config.openai_model, environment=config.it_environment)
    if assistant.enabled:
        logger.info("AI replies are on (model %s)", config.openai_model)
    else:
        logger.info("AI replies are off (no OPENAI_API_KEY). Tickets are triaged with keyword rules.")

    app = App(token=config.slack_bot_token)
    bot_user_id = app.client.auth_test()["user_id"]
    try:
        channel_id = resolve_channel_id(app.client, config.it_channel)
        is_member = check_channel_access(app.client, channel_id)
    except ConfigError as exc:
        raise SystemExit(f"Configuration problem: {exc}") from None
    if not is_member:
        logger.warning("The bot isn't a member of %s yet, so it won't see any posts. Run /invite @<bot name> there.",
                       config.it_channel)

    desk = HelpDesk(config, app.client, jira, assistant, bot_user_id, channel_id)
    desk.register(app)
    if config.report_enabled:
        schedule_weekly_report(desk, config).start()
        logger.info("Weekly report scheduled for %s at %02d:00 %s", config.report_day, config.report_hour,
                    config.report_timezone)

    logger.info("Watching %s for IT requests", config.it_channel)
    SocketModeHandler(app, config.slack_app_token).start()


if __name__ == "__main__":
    main()
