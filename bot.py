"""Slack + Jira IT help desk bot.

Every new post in the IT channel becomes a Jira ticket. The bot replies in the thread with the ticket link and,
if OpenAI is configured, first troubleshooting steps. It keeps helping the requester in the thread, copies the
thread into Jira as comments, and lets the requester close or escalate the ticket with buttons.
"""

import logging
import re
import threading
from collections import OrderedDict
from functools import lru_cache

from apscheduler.schedulers.background import BackgroundScheduler
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

import reports
import tickets
from assistant import Assistant
from config import ConfigError, load_config
from jira_client import JiraClient, JiraError, is_done, noformat
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
            self.open_ticket(event)

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
            self._ephemeral(channel, user, ticket,
                            f"Only <@{ticket.creator}> can use these buttons. IT staff can update {ticket.key} in Jira.")
            return
        try:
            announcement, problem = action(ticket, user)
        except JiraError as exc:
            logger.error("Jira rejected a button action on %s: %s", ticket.key, exc)
            self._ephemeral(channel, user, ticket, f":warning: Jira didn't accept that change: {exc}")
            return
        if problem:
            self._ephemeral(channel, user, ticket, problem)
            return
        self.slack.chat_postMessage(channel=channel, thread_ts=ticket.thread_ts, text=announcement)
        message = body.get("message") or {}
        if message.get("blocks"):
            self.slack.chat_update(
                channel=channel,
                ts=body["container"]["message_ts"],
                text=message.get("text", ""),
                blocks=tickets.without_buttons(message["blocks"]),
            )

    # --- Ticket lifecycle --------------------------------------------------------------------------------------

    def open_ticket(self, event):
        channel, ts, requester = event["channel"], event["ts"], event["user"]
        plain = slack_to_plain(event.get("text", ""), self.user_name)
        text = redact_secrets(plain)
        files = event.get("files") or []
        if not text and not files:
            return
        text = text or "(No text. See the attachments in Slack.)"

        triage, ai_reply = self.assistant.assess(text)
        try:
            key = self.jira.create_issue(
                project_key=self.config.jira_project_key,
                issue_type=self.config.jira_issue_type if triage.kind == "incident" else self.config.jira_request_issue_type,
                summary=triage.summary,
                description=self._description(requester, channel, ts, text, len(files)),
                labels=tickets.ticket_labels(self.config.jira_label, triage),
                priority=triage.priority if self.config.jira_set_priority else None,
            )
        except JiraError as exc:
            logger.error("Couldn't create a Jira ticket for message %s: %s", ts, exc)
            who = self.config.escalation_mention or "The IT team"
            self.slack.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=f":warning: I couldn't create a Jira ticket for this. {who} will need to pick it up manually.",
            )
            return

        logger.info("Opened %s (%s, %s) for %s", key, triage.kind, triage.category, requester)
        ticket = tickets.TicketRef(key=key, creator=requester, thread_ts=ts)
        reply = to_slack_mrkdwn(ai_reply) if ai_reply else ""
        self.slack.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=tickets.ticket_text(ticket, triage, reply),
            blocks=tickets.ticket_blocks(ticket, self.jira.browse_url(key), triage, reply, secret_removed=text != plain),
            unfurl_links=False,
            unfurl_media=False,
        )
        if ai_reply:
            self._comment(key, "AI assistant replied in Slack:\n" + noformat(ai_reply))

    def on_thread_reply(self, event):
        channel, thread_ts, author = event["channel"], event["thread_ts"], event["user"]
        messages = self.slack.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
        ticket = tickets.find_ticket(messages, self.bot_user_id)
        if ticket is None:
            return  # not a thread this bot opened a ticket in

        text = self._clean(event.get("text", ""))
        if text:
            self._comment(ticket.key, f"{self.user_name(author)} replied in Slack:\n" + noformat(text))

        if author != ticket.creator or not self.assistant.enabled:
            return
        if tickets.human_took_over(messages, ticket.creator, self.bot_user_id):
            return  # someone from IT has joined, so the AI stays out of the way
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
        if text and not any(m.get("ts") == event["ts"] for m in messages):
            history.append({"role": "user", "content": text})  # Slack hadn't indexed the new reply yet
        try:
            answer = self.assistant.follow_up(ticket.key, history)
        except Exception:
            logger.exception("AI follow-up failed for %s", ticket.key)
            return
        if answer:
            self._post_reply(channel, ticket, to_slack_mrkdwn(answer))
            self._comment(ticket.key, "AI assistant replied in Slack:\n" + noformat(answer))

    def resolve(self, ticket, user):
        """Close the ticket in Jira. Returns (thread announcement, None) or (None, reason it wasn't changed)."""
        if is_done(self.jira.get_issue(ticket.key)):
            return None, f"{ticket.key} is already closed."
        if not self.jira.transition_to_done(ticket.key):
            return None, f"I couldn't find a way to close {ticket.key} in its Jira workflow. IT staff can close it in Jira."
        self._comment(ticket.key, f"{self.user_name(user)} marked this resolved from Slack.")
        return f":white_check_mark: <{self.jira.browse_url(ticket.key)}|{ticket.key}> is closed. Glad it's sorted!", None

    def escalate(self, ticket, user):
        """Flag the ticket for a person. Returns (thread announcement, None) or (None, reason it wasn't changed)."""
        fields = self.jira.get_issue(ticket.key)
        if is_done(fields):
            return None, f"{ticket.key} is already closed. Post a new message in the channel if you still need help."
        if tickets.ESCALATED_LABEL in (fields.get("labels") or []):
            return None, f"{ticket.key} has already been escalated."
        self.jira.add_labels(ticket.key, [tickets.ESCALATED_LABEL])
        self._comment(ticket.key, f"{self.user_name(user)} escalated this from Slack. The AI assistant has stopped replying.")
        who = self.config.escalation_mention or "The IT team"
        return f":sos: <{self.jira.browse_url(ticket.key)}|{ticket.key}> has been escalated. {who} will take it from here.", None

    # --- Reports -----------------------------------------------------------------------------------------------

    def report_text(self):
        try:
            return reports.build_report(self.jira, self.config.jira_project_key, self.config.jira_label)
        except JiraError as exc:
            logger.error("Couldn't build the report: %s", exc)
            return f":warning: I couldn't build the report: {exc}"

    def post_weekly_report(self):
        self.slack.chat_postMessage(channel=self.channel_id, text=self.report_text())

    # --- Helpers -----------------------------------------------------------------------------------------------

    def _clean(self, text):
        return redact_secrets(slack_to_plain(text, self.user_name))

    def _description(self, requester, channel, ts, text, file_count):
        lines = [f"Reported in Slack by {self.user_name(requester)}."]
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

    def _ephemeral(self, channel, user, ticket, text):
        self.slack.chat_postEphemeral(channel=channel, user=user, thread_ts=ticket.thread_ts, text=text)

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
        page = slack.conversations_list(**kwargs)
        for found in page["channels"]:
            if found["name"] == name:
                return found["id"]
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            raise ConfigError(f"There's no public channel named #{name}. Check IT_CHANNEL, "
                              "or set it to the channel ID if the channel is private.")


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
    except ConfigError as exc:
        raise SystemExit(f"Configuration problem: {exc}") from None
    if not app.client.conversations_info(channel=channel_id)["channel"].get("is_member"):
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
