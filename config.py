"""Settings for the bot, read from environment variables. A local .env file is loaded automatically."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from assistant import DEFAULT_MODEL

PROJECT_DIR = Path(__file__).resolve().parent
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# A member ID (U… or W…) or user group ID (S…), plain or as a Slack mention: <@U…> or <!subteam^S…|@name>
_STAFF_ENTRY = re.compile(r"<@([UW][A-Z0-9]{2,})(?:\|[^>]*)?>|([UW][A-Z0-9]{2,})"
                          r"|<!subteam\^(S[A-Z0-9]{2,})(?:\|[^>]*)?>|(S[A-Z0-9]{2,})")


class ConfigError(Exception):
    """A setting is missing or invalid."""


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    it_channel: str
    jira_base_url: str
    jira_email: str | None
    jira_api_token: str
    jira_project_key: str
    jira_issue_type: str
    jira_request_issue_type: str
    jira_label: str
    jira_set_priority: bool
    jira_set_reporter: bool
    jira_done_transition: str | None
    openai_api_key: str | None
    openai_model: str
    ai_allowed_link_domains: tuple[str, ...]
    it_environment: str
    escalation_mention: str | None
    it_staff_users: tuple[str, ...]
    it_staff_groups: tuple[str, ...]
    max_ai_follow_ups: int
    merge_window_seconds: int
    max_tickets_per_hour: int
    jira_sync_seconds: int
    report_enabled: bool
    report_day: str
    report_hour: int
    report_timezone: str


def load_config(env=None) -> Config:
    """Build a Config from `env`, which defaults to os.environ after loading .env."""
    if env is None:
        load_dotenv()
        env = os.environ

    def get(name, default=None):
        value = (env.get(name) or "").strip()
        return value or default

    def required(name):
        value = get(name)
        if value is None:
            raise ConfigError(f"Missing required setting {name}. See .env.example.")
        return value

    def number(name, default):
        value = get(name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise ConfigError(f"{name} must be a whole number, got {value!r}.") from None

    def flag(name, default):
        value = get(name)
        return default if value is None else value.lower() in {"1", "true", "yes", "on"}

    bot_token = required("SLACK_BOT_TOKEN")
    if not bot_token.startswith("xoxb-"):
        raise ConfigError("SLACK_BOT_TOKEN should be the Bot User OAuth Token, which starts with xoxb-.")
    app_token = required("SLACK_APP_TOKEN")
    if not app_token.startswith("xapp-"):
        raise ConfigError("SLACK_APP_TOKEN should be an App-Level Token with connections:write, which starts with xapp-.")

    label = get("JIRA_LABEL", "slack-it-bot")
    if " " in label:
        raise ConfigError("JIRA_LABEL can't contain spaces (Jira labels are single words).")

    jira_url = required("JIRA_BASE_URL").rstrip("/")
    url = urlsplit(jira_url)
    if not url.hostname or not (url.scheme == "https" or (url.scheme == "http" and url.hostname in _LOCAL_HOSTS)):
        raise ConfigError("JIRA_BASE_URL must start with https:// (for example https://your-site.atlassian.net), "
                          "so the Jira API token is encrypted on its way to Jira.")

    max_tickets_per_hour = number("MAX_TICKETS_PER_HOUR", 10)
    if max_tickets_per_hour < 0:
        raise ConfigError("MAX_TICKETS_PER_HOUR must be 0 (no limit) or more.")
    jira_sync_seconds = number("JIRA_SYNC_SECONDS", 60)
    if jira_sync_seconds != 0 and jira_sync_seconds < 10:
        raise ConfigError("JIRA_SYNC_SECONDS must be 0 (off) or at least 10, so the bot doesn't flood Jira with requests.")

    report_enabled = flag("REPORT_ENABLED", True)
    report_day = get("REPORT_DAY", "mon")
    report_hour = number("REPORT_HOUR", 9)
    report_timezone = get("REPORT_TIMEZONE", "UTC")
    if report_enabled:
        _check_report_schedule(report_day, report_hour, report_timezone)

    staff_users, staff_groups = _staff_list(get("IT_STAFF", ""))
    issue_type = get("JIRA_ISSUE_TYPE", "Task")
    return Config(
        slack_bot_token=bot_token,
        slack_app_token=app_token,
        it_channel=required("IT_CHANNEL"),
        jira_base_url=jira_url,
        jira_email=get("JIRA_EMAIL"),
        jira_api_token=required("JIRA_API_TOKEN"),
        jira_project_key=required("JIRA_PROJECT_KEY").upper(),
        jira_issue_type=issue_type,
        jira_request_issue_type=get("JIRA_REQUEST_ISSUE_TYPE", issue_type),
        jira_label=label,
        jira_set_priority=flag("JIRA_SET_PRIORITY", True),
        jira_set_reporter=flag("JIRA_SET_REPORTER", True),
        jira_done_transition=get("JIRA_DONE_TRANSITION"),
        openai_api_key=get("OPENAI_API_KEY"),
        openai_model=get("OPENAI_MODEL", DEFAULT_MODEL),
        ai_allowed_link_domains=_domain_list(get("AI_ALLOWED_LINK_DOMAINS", "")),
        it_environment=_read_environment_notes(get("IT_ENVIRONMENT_FILE", "it_environment.md")),
        escalation_mention=get("ESCALATION_MENTION"),
        it_staff_users=staff_users,
        it_staff_groups=staff_groups,
        max_ai_follow_ups=number("MAX_AI_FOLLOW_UPS", 3),
        merge_window_seconds=number("MERGE_WINDOW_SECONDS", 120),
        max_tickets_per_hour=max_tickets_per_hour,
        jira_sync_seconds=jira_sync_seconds,
        report_enabled=report_enabled,
        report_day=report_day,
        report_hour=report_hour,
        report_timezone=report_timezone,
    )


def _check_report_schedule(day, hour, timezone):
    """Catch schedule mistakes here, with a clear message, instead of as a traceback when the scheduler starts."""
    if not 0 <= hour <= 23:
        raise ConfigError(f"REPORT_HOUR must be between 0 and 23, got {hour}.")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"REPORT_TIMEZONE {timezone!r} isn't a known time zone. "
                          "Use a name like UTC or America/New_York.") from None
    try:
        CronTrigger(day_of_week=day, hour=hour, timezone=timezone)
    except ValueError:
        raise ConfigError(f"REPORT_DAY {day!r} isn't valid. Use mon, tue, wed, thu, fri, sat or sun, "
                          "or a range such as mon-fri.") from None


def _domain_list(value):
    """"example.com, *.docs.example.org" -> ("example.com", "docs.example.org")."""
    domains = (part.strip().lower().removeprefix("*.").strip(".") for part in value.split(","))
    return tuple(domain for domain in domains if domain)


def _staff_list(value):
    """"U012AB, <!subteam^S034CD|@it-team>" -> (("U012AB",), ("S034CD",)): Slack user IDs and user group IDs, written
    plainly or in the mention syntax ESCALATION_MENTION uses."""
    users, groups = [], []
    for part in filter(None, (part.strip() for part in value.split(","))):
        match = _STAFF_ENTRY.fullmatch(part)
        if not match:
            raise ConfigError(f"IT_STAFF entry {part!r} isn't a Slack member ID (U…) or user group ID (S…). "
                              "Separate entries with commas.")
        user, group = match.group(1) or match.group(2), match.group(3) or match.group(4)
        (users if user else groups).append(user or group)
    return tuple(users), tuple(groups)


def _read_environment_notes(filename):
    path = Path(filename)
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.read_text(encoding="utf-8") if path.is_file() else ""
