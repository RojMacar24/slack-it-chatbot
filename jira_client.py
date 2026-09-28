"""Small Jira REST client with just what the bot needs. Works with Jira Cloud and Jira Data Center.

Uses REST API v2 (available on both, and it accepts plain-text descriptions), except for search on Jira Cloud,
which Atlassian moved to /rest/api/3/search/jql.
"""

import logging

import requests

logger = logging.getLogger(__name__)


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

    def transition_to_done(self, key):
        """Move the issue to a status in Jira's "Done" category.

        Returns False if the workflow offers no such transition from the current status (for example, it's already done).
        """
        transitions = self._request("GET", f"/rest/api/2/issue/{key}/transitions")["transitions"]
        done = [t for t in transitions if t.get("to", {}).get("statusCategory", {}).get("key") == "done"]
        if not done:
            return False
        self._request("POST", f"/rest/api/2/issue/{key}/transitions", json={"transition": {"id": done[0]["id"]}})
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


def is_done(fields):
    """True if an issue's fields (from get_issue or search) show a status in the Done category."""
    return (fields.get("status") or {}).get("statusCategory", {}).get("key") == "done"


def noformat(text):
    """Wrap text so Jira shows it exactly as written instead of reading it as wiki markup."""
    return "{noformat}\n" + text.replace("{noformat}", "{ noformat }") + "\n{noformat}"
