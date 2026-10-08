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
    """A Jira API call failed. The message includes Jira's own explanation when it gave one, and `fields` names the
    fields Jira rejected, if any."""

    def __init__(self, message, status=None, fields=()):
        super().__init__(message)
        self.status = status
        self.fields = tuple(fields)


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
        me = self.myself()
        self._request("GET", f"/rest/api/2/project/{project_key}")
        return me.get("displayName") or me.get("name") or "unknown user"

    def myself(self):
        """This account, as Jira describes it. account_id() picks out what identifies it."""
        return self._request("GET", "/rest/api/2/myself")

    def has_permission(self, project_key, permission):
        """Whether this account has a project permission, such as MODIFY_REPORTER."""
        found = self._request("GET", "/rest/api/2/mypermissions",
                              params={"projectKey": project_key, "permissions": permission})["permissions"]
        return bool((found.get(permission) or {}).get("havePermission"))

    def find_user(self, email):
        """The account with exactly this email, as a `reporter` for create_issue(), or None.

        Jira's user search also matches the start of names and emails, so only an exact match on a visible email
        counts. Jira Cloud hides most people's emails from accounts that aren't admins, and then this returns None.
        """
        search = {"query": email} if self.is_cloud else {"username": email}
        users = self._request("GET", "/rest/api/2/user/search", params={**search, "maxResults": 20}) or []
        matches = [user for user in users
                   if user.get("active", True) and (user.get("emailAddress") or "").lower() == email.lower()]
        if len(matches) != 1:
            return None
        return {"accountId": matches[0]["accountId"]} if self.is_cloud else {"name": matches[0]["name"]}

    def create_issue(self, project_key, issue_type, summary, description, labels=(), priority=None, reporter=None):
        """Create an issue and return its key, e.g. "IT-42". `reporter` comes from find_user()."""
        fields = {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": description,
            "labels": list(labels),
        }
        if priority:
            fields["priority"] = {"name": priority}
        if reporter:
            fields["reporter"] = reporter
        try:
            return self._request("POST", "/rest/api/2/issue", json={"fields": fields})["key"]
        except JiraError as exc:
            optional = [name for name in ("priority", "reporter") if name in fields]
            if not optional or exc.status != 400:
                raise
            # Some projects don't put Priority on the create screen, or don't let this account set the reporter.
            # The ticket matters more than either, so drop what Jira rejected (or both, if it didn't say) and retry.
            rejected = [name for name in optional if name in exc.fields] or optional
            logger.warning("Jira rejected the %s, creating the issue without it: %s", " and ".join(rejected), exc)
            for name in rejected:
                del fields[name]
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

    def search(self, jql, fields, max_issues=1000, expand=None):
        """Return up to `max_issues` issues matching `jql`, each as Jira returns it ({"key", "fields", ...}).
        `expand="changelog"` adds each issue's change history."""
        issues = []
        params = {"jql": jql, "fields": ",".join(fields), "maxResults": 100}
        if expand:
            params["expand"] = expand
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
            text, fields = _error_details(response)
            raise JiraError(f"{method} {path} returned {response.status_code}: {text}", response.status_code, fields)
        return response.json() if response.content else None


def _error_details(response):
    """Jira's explanation of a failed request, and the names of the fields it rejected."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or response.reason, ()
    if not isinstance(body, dict):
        return response.text[:300], ()
    errors = body.get("errors") or {}
    messages = list(body.get("errorMessages") or []) + [f"{field}: {message}" for field, message in errors.items()]
    return "; ".join(messages) or response.text[:300], tuple(errors)


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


def account_id(user):
    """What identifies a Jira user, such as a changelog author or myself(): accountId on Cloud, key or name on
    Data Center."""
    return user.get("accountId") or user.get("key") or user.get("name")


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
