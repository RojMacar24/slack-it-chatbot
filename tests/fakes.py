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
        self.emails = {}  # user ID -> email, as users.info shows it with the users:read.email scope
        self.groups = {}  # user group ID -> member IDs
        self._clock = 1000

    def next_ts(self):
        self._clock += 1
        return f"{self._clock}.000100"

    def calls_to(self, method):
        return [kwargs for name, kwargs in self.calls if name == method]

    def chat_postMessage(self, **kwargs):
        """Raises SlackApiError for posts that `fail_post` (if set) says should fail."""
        self.calls.append(("chat_postMessage", kwargs))
        if getattr(self, "fail_post", None) and self.fail_post(kwargs):
            raise SlackApiError("invalid_blocks", {"ok": False, "error": "invalid_blocks"})
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
        """Like Slack's: a link to a reply also names its thread."""
        link = f"https://slack.example/archives/{channel}/p{message_ts.replace('.', '')}"
        parent = next((ts for ts, messages in self.threads.items()
                       if ts != message_ts and any(m["ts"] == message_ts for m in messages)), None)
        return {"permalink": link + (f"?thread_ts={parent}&cid={channel}" if parent else "")}

    def conversations_replies(self, channel, ts, limit=None):
        return {"messages": copy.deepcopy(self.threads.get(ts, []))}

    def users_info(self, user):
        self.calls.append(("users_info", {"user": user}))
        profile = {"email": self.emails[user]} if user in self.emails else {}
        return {"user": {"id": user, "real_name": self.names.get(user, user), "profile": profile}}

    def usergroups_users_list(self, usergroup):
        """Members of `usergroup` from `groups`. Raises like Slack does when the app lacks usergroups:read."""
        self.calls.append(("usergroups_users_list", {"usergroup": usergroup}))
        if getattr(self, "groups_missing_scope", False):
            raise SlackApiError("missing_scope", {"ok": False, "error": "missing_scope"})
        return {"users": list(self.groups.get(usergroup, []))}


BOT_JIRA_ACCOUNT = "acct-bot"


class FakeJira:
    base_url = "https://jira.example"

    def __init__(self):
        self.issues = {}
        self.histories = {}  # key -> changelog histories, like search(expand="changelog") returns them
        self._next_history_id = 10000
        self.created = []
        self.comments = []
        self.fail_create = False
        self.can_transition = True
        self.accounts = {}  # email -> account ID, for find_user
        self.user_lookups = []
        self.permissions = {"MODIFY_REPORTER"}

    def browse_url(self, key):
        return f"{self.base_url}/browse/{key}"

    def has_permission(self, project_key, permission):
        return permission in self.permissions

    def find_user(self, email):
        self.user_lookups.append(email)
        return {"accountId": self.accounts[email]} if email in self.accounts else None

    def create_issue(self, project_key, issue_type, summary, description, labels=(), priority=None, reporter=None):
        if self.fail_create:
            raise JiraError("POST /rest/api/2/issue returned 400: issuetype: invalid", 400)
        key = f"{project_key}-{len(self.created) + 1}"
        self.created.append({"key": key, "issue_type": issue_type, "summary": summary, "description": description,
                             "labels": list(labels), "priority": priority, "reporter": reporter})
        self.issues[key] = {"status": {"name": "To Do", "statusCategory": {"key": "new"}}, "labels": list(labels),
                            "description": description}
        return key

    def myself(self):
        return {"accountId": BOT_JIRA_ACCOUNT, "displayName": "IT Help Desk Bot"}

    def change_status(self, key, name, category="indeterminate", by="acct-alex", who="Alex Kim"):
        """Someone (by default a person, not the bot) moves the issue to another status in Jira."""
        old = self.issues[key]["status"].get("name")
        self.issues[key]["status"] = {"name": name, "statusCategory": {"key": category}}
        self._next_history_id += 1
        self.histories.setdefault(key, []).append({
            "id": str(self._next_history_id),
            "author": {"accountId": by, "displayName": who},
            "items": [{"field": "status", "fromString": old, "toString": name}],
        })

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
        self.change_status(key, "Done", "done", by=BOT_JIRA_ACCOUNT, who="IT Help Desk Bot")
        return True

    def search(self, jql, fields, expand=None):
        self.last_jql = jql
        found = [{"key": key, "fields": copy.deepcopy(fields)} for key, fields in self.issues.items()]
        if expand == "changelog":
            for issue in found:
                issue["changelog"] = {"histories": copy.deepcopy(self.histories.get(issue["key"], []))}
        return found


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
