"""Settings for the bot, read from environment variables. A local .env file is loaded automatically."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


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
    jira_done_transition: str | None
    openai_api_key: str | None
    openai_model: str
    ai_allowed_link_domains: tuple[str, ...]
    it_environment: str
    escalation_mention: str | None
    max_ai_follow_ups: int
    merge_window_seconds: int
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
        jira_done_transition=get("JIRA_DONE_TRANSITION"),
        openai_api_key=get("OPENAI_API_KEY"),
        openai_model=get("OPENAI_MODEL", "gpt-4o-mini"),
        ai_allowed_link_domains=_domain_list(get("AI_ALLOWED_LINK_DOMAINS", "")),
        it_environment=_read_environment_notes(get("IT_ENVIRONMENT_FILE", "it_environment.md")),
        escalation_mention=get("ESCALATION_MENTION"),
        max_ai_follow_ups=number("MAX_AI_FOLLOW_UPS", 3),
        merge_window_seconds=number("MERGE_WINDOW_SECONDS", 120),
        report_enabled=flag("REPORT_ENABLED", True),
        report_day=get("REPORT_DAY", "mon"),
        report_hour=number("REPORT_HOUR", 9),
        report_timezone=get("REPORT_TIMEZONE", "UTC"),
    )


def _domain_list(value):
    """"example.com, *.docs.example.org" -> ("example.com", "docs.example.org")."""
    domains = (part.strip().lower().removeprefix("*.").strip(".") for part in value.split(","))
    return tuple(domain for domain in domains if domain)


def _read_environment_notes(filename):
    path = Path(filename)
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.read_text(encoding="utf-8") if path.is_file() else ""
