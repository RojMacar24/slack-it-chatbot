"""Small Jira REST client with just what the bot needs. Works with Jira Cloud and Jira Data Center.

Uses REST API v2 (available on both, and it accepts plain-text descriptions), except for search on Jira Cloud,
which Atlassian moved to /rest/api/3/search/jql.
"""

import logging
import re

import requests

logger = logging.getLogger(__name__)

# How a "Done" transition or resolution is named decides whether it means "fixed" or "abandoned".
_CANCEL_WORDS = re.compile(r"cancel|won'?t|reject|declin|duplicate|obsolete|abandon", re.IGNORECASE)
_RESOLVED_WORDS = re.compile(r"done|resolv|close|complet|fix", re.IGNORECASE)


class JiraError(Exception):
    """A Jira API call failed. The message includes Jira's own explanation when it gave one."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class JiraClient:
    def __init__(self, base_url, api_token, email=None, timeout=20, session=None):
        """With `email`, authenticates the Jira Cloud way (email + API token).
        Without it, treats `api_token` as a Data Center personal access token."""
        self.base_url = base_url.rstrip("/")
        self.is_cloud = email is not None
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        if email:
            self._session.auth = (email, api_token)
        else:
            self._session.headers["Authorization"] = f"Bearer {api_token}"

    def browse_url(self, key):
        return f"{self.base_url}/browse/{key}"

    def check_connection(self, project_key):
        """Confirm the credentials work and the project exists. Returns the Jira user's display name."""
        me = self._request("GET", "/rest/api/2/myself")
        self._request("GET", f"/rest/api/2/project/{project_key}")
        return me.get("displayName") or me.get("name") or "unknown user"

    def create_issue(self, project_key, issue_type, summary, description, labels=(), priority=None):
        """Create an issue and return its key, e.g. "IT-42"."""
        fields = {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": description,
            "labels": list(labels),
        }
        if priority:
            fields["priority"] = {"name": priority}
        try:
            return self._request("POST", "/rest/api/2/issue", json={"fields": fields})["key"]
        except JiraError as exc:
            if not priority or exc.status != 400:
                raise
            # Some projects don't put Priority on the create screen. The ticket matters more than its priority.
            logger.warning("Jira rejected priority %r, creating the issue without it: %s", priority, exc)
            del fields["priority"]
            return self._request("POST", "/rest/api/2/issue", json={"fields": fields})["key"]

    def get_issue(self, key, fields=("status", "labels")):
        return self._request("GET", f"/rest/api/2/issue/{key}", params={"fields": ",".join(fields)})["fields"]

    def add_comment(self, key, body):
        self._request("POST", f"/rest/api/2/issue/{key}/comment", json={"body": body})

    def add_labels(self, key, labels):
        self._request("PUT", f"/rest/api/2/issue/{key}", json={"update": {"labels": [{"add": label} for label in labels]}})

    def transition_to_done(self, key, preferred_name=None):
        """Resolve the issue: move it to a Done-category status that means fixed, not cancelled.

        Returns False if the workflow offers no suitable transition from the current status (for example, it's
        already done, or the only way to Done is "Cancel").
        """
        transitions = self._request("GET", f"/rest/api/2/issue/{key}/transitions",
                                    params={"expand": "transitions.fields"})["transitions"]
        choice = pick_done_transition(transitions, preferred_name)
        if choice is None:
            return False
        payload = {"transition": {"id": choice["id"]}}
        resolution = (choice.get("fields") or {}).get("resolution")
        if resolution and resolution.get("required"):
            payload["fields"] = {"resolution": {"name": pick_resolution(resolution.get("allowedValues") or [])}}
        self._request("POST", f"/rest/api/2/issue/{key}/transitions", json=payload)
        return True

    def search(self, jql, fields, max_issues=1000):
        """Return up to `max_issues` issues matching `jql`, each as Jira returns it ({"key", "fields", ...})."""
        issues = []
        params = {"jql": jql, "fields": ",".join(fields), "maxResults": 100}
        if self.is_cloud:
            while len(issues) < max_issues:
                page = self._request("GET", "/rest/api/3/search/jql", params=params)
                issues += page.get("issues", [])
                if not page.get("nextPageToken"):
                    break
                params["nextPageToken"] = page["nextPageToken"]
        else:
            params["startAt"] = 0
            while len(issues) < max_issues:
                page = self._request("GET", "/rest/api/2/search", params=params)
                batch = page.get("issues", [])
                issues += batch
                params["startAt"] += len(batch)
                if not batch or params["startAt"] >= page.get("total", 0):
                    break
        return issues[:max_issues]

    def _request(self, method, path, **kwargs):
        try:
            response = self._session.request(method, self.base_url + path, timeout=self._timeout, **kwargs)
        except requests.RequestException as exc:
            raise JiraError(f"{method} {path} failed: {exc}") from exc
        if not response.ok:
            raise JiraError(f"{method} {path} returned {response.status_code}: {_error_text(response)}", response.status_code)
        return response.json() if response.content else None


def _error_text(response):
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or response.reason
    if not isinstance(body, dict):
        return response.text[:300]
    messages = list(body.get("errorMessages") or [])
    messages += [f"{field}: {message}" for field, message in (body.get("errors") or {}).items()]
    return "; ".join(messages) or response.text[:300]


def pick_done_transition(transitions, preferred_name=None):
    """Choose the transition that resolves an issue.

    `preferred_name` (JIRA_DONE_TRANSITION) wins if the workflow has it. Otherwise: only transitions into a
    Done-category status, never cancel-style ones, preferring names like Done/Resolve/Close.
    """
    if preferred_name:
        return next((t for t in transitions if t.get("name", "").lower() == preferred_name.lower()), None)

    def described(t):
        return f"{t.get('name', '')} {(t.get('to') or {}).get('name', '')}"

    usable = [
        t for t in transitions
        if (t.get("to") or {}).get("statusCategory", {}).get("key") == "done" and not _CANCEL_WORDS.search(described(t))
    ]
    return next((t for t in usable if _RESOLVED_WORDS.search(described(t))), usable[0] if usable else None)


def pick_resolution(allowed_values):
    """Choose a resolution such as Done or Fixed when a transition requires one."""
    names = [v.get("name", "") for v in allowed_values if v.get("name")]
    usable = [name for name in names if not _CANCEL_WORDS.search(name)]
    return next((name for name in usable if _RESOLVED_WORDS.search(name)), (usable or names or ["Done"])[0])


def is_done(fields):
    """True if an issue's fields (from get_issue or search) show a status in the Done category."""
    return (fields.get("status") or {}).get("statusCategory", {}).get("key") == "done"


def noformat(text):
    """Wrap text so Jira shows it exactly as written instead of reading it as wiki markup."""
    return "{noformat}\n" + text.replace("{noformat}", "{ noformat }") + "\n{noformat}"


# Anything but letters, digits, spaces and . ' ( ) -, which covers Jira's link, formatting and macro characters,
# and the ":" and "/" that would let a URL turn into a link.
_INLINE_UNSAFE = re.compile(r"[^\w .'()-]|_")


def safe_inline(text, limit=80):
    """Make short user-controlled text, such as a Slack display name, safe to show inline in Jira wiki markup."""
    return " ".join(_INLINE_UNSAFE.sub(" ", text).split())[:limit] or "unknown user"
