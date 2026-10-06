"""In-memory stand-ins for Slack, Jira and the AI model, so tests run without network access."""

import copy

from slack_sdk.errors import SlackApiError

from assistant import keyword_triage
from config import load_config
from jira_client import JiraError

BOT_USER_ID = "UBOT"
CHANNEL_ID = "CITHELP01"

BASE_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "IT_CHANNEL": CHANNEL_ID,
    "JIRA_BASE_URL": "https://jira.example",
    "JIRA_EMAIL": "bot@example.com",
    "JIRA_API_TOKEN": "token",
    "JIRA_PROJECT_KEY": "IT",
}


def make_config(**overrides):
    return load_config({**BASE_ENV, **overrides})


class FakeSlack:
    def __init__(self):
        self.calls = []
        self.threads = {}  # parent ts -> messages, parent first (like conversations.replies)
        self.names = {}
        self._clock = 1000

    def next_ts(self):
        self._clock += 1
        return f"{self._clock}.000100"

    def calls_to(self, method):
        return [kwargs for name, kwargs in self.calls if name == method]

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))
        ts = self.next_ts()
        if kwargs.get("thread_ts") in self.threads:
            self.threads[kwargs["thread_ts"]].append(
                {"ts": ts, "user": BOT_USER_ID, "bot_id": "BBOT", "text": kwargs["text"], "blocks": kwargs.get("blocks")}
            )
        return {"ok": True, "ts": ts}

    def chat_postEphemeral(self, **kwargs):
        self.calls.append(("chat_postEphemeral", kwargs))
        return {"ok": True}

    def chat_update(self, **kwargs):
        """Edits the stored message, like Slack does. Raises for any ts in `fail_updates`."""
        self.calls.append(("chat_update", kwargs))
        if kwargs["ts"] in getattr(self, "fail_updates", ()):
            raise SlackApiError("cant_update_message", {"ok": False, "error": "cant_update_message"})
        for messages in self.threads.values():
            for message in messages:
                if message["ts"] == kwargs["ts"]:
                    message.update(text=kwargs["text"], blocks=kwargs.get("blocks"))
        return {"ok": True}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.example/archives/{channel}/p{message_ts.replace('.', '')}"}

    def conversations_replies(self, channel, ts, limit=None):
        return {"messages": copy.deepcopy(self.threads.get(ts, []))}

    def users_info(self, user):
        return {"user": {"id": user, "real_name": self.names.get(user, user)}}


class FakeJira:
    base_url = "https://jira.example"

    def __init__(self):
        self.issues = {}
        self.created = []
        self.comments = []
        self.fail_create = False
        self.can_transition = True

    def browse_url(self, key):
        return f"{self.base_url}/browse/{key}"

    def create_issue(self, project_key, issue_type, summary, description, labels=(), priority=None):
        if self.fail_create:
            raise JiraError("POST /rest/api/2/issue returned 400: issuetype: invalid", 400)
        key = f"{project_key}-{len(self.created) + 1}"
        self.created.append({"key": key, "issue_type": issue_type, "summary": summary,
                             "description": description, "labels": list(labels), "priority": priority})
        self.issues[key] = {"status": {"statusCategory": {"key": "new"}}, "labels": list(labels)}
        return key

    def get_issue(self, key, fields=("status", "labels")):
        return copy.deepcopy(self.issues[key])

    def add_comment(self, key, body):
        self.comments.append((key, body))

    def add_labels(self, key, labels):
        self.issues[key]["labels"] += labels

    def transition_to_done(self, key, preferred_name=None):
        self.preferred_transition = preferred_name
        if not self.can_transition:
            return False
        self.issues[key]["status"] = {"statusCategory": {"key": "done"}}
        return True

    def search(self, jql, fields):
        self.last_jql = jql
        return [{"key": key, "fields": copy.deepcopy(fields)} for key, fields in self.issues.items()]


class FakeAssistant:
    enabled = True

    def __init__(self, first_reply="Try **restarting** the VPN client.", follow_up_reply="Next, check the network settings."):
        self.first_reply = first_reply
        self.follow_up_reply = follow_up_reply
        self.assessed = []
        self.histories = []

    def assess(self, text):
        self.assessed.append(text)
        triage = keyword_triage(text)
        return triage, self.first_reply if triage.kind == "incident" else ""

    def follow_up(self, issue_key, history):
        self.histories.append(history)
        return self.follow_up_reply
