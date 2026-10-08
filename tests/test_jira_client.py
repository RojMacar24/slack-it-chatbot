import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from helpdesk.jira_client import (
    JiraClient, JiraError, account_id, is_done, noformat, pick_done_transition, pick_resolution, safe_inline,
)


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = status < 400
        self.reason = "Bad Request" if status >= 400 else "OK"
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Answers requests from a list of (method, path suffix, FakeResponse), in order."""

    def __init__(self, *responses):
        self.headers, self.auth = {}, None
        self.responses, self.requests = list(responses), []

    def request(self, method, url, timeout=None, **kwargs):
        self.requests.append((method, url, copy.deepcopy(kwargs)))  # the client reuses its params dict between pages
        if isinstance(self.responses[0], Exception):
            raise self.responses.pop(0)
        expected_method, expected_path, response = self.responses.pop(0)
        assert (method, url.split("jira.example")[1]) == (expected_method, expected_path)
        return response


def client(*responses, email="bot@example.com"):
    session = FakeSession(*responses)
    return JiraClient("https://jira.example/", "token", email=email, session=session), session


class DroppingJira(BaseHTTPRequestHandler):
    """A local stand-in for Jira that hangs up without answering the first `drops` requests, the way Jira closes
    a kept-alive connection that sat idle, then answers normally."""
    drops = 1
    seen = []

    def _handle(self):
        type(self).seen.append(self.command)
        if len(type(self).seen) <= type(self).drops:
            self.close_connection = True
            return  # no response at all: the client sees "Remote end closed connection without response"
        body = json.dumps({"key": "IT-1", "fields": {"status": {"name": "To Do"}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


@pytest.fixture
def dropping_jira():
    DroppingJira.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), DroppingJira)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield JiraClient(f"http://127.0.0.1:{server.server_port}", "token", email="bot@example.com")
    server.shutdown()
    server.server_close()


def test_reads_are_retried_when_jira_drops_the_connection(dropping_jira):
    assert dropping_jira.get_issue("IT-1") == {"status": {"name": "To Do"}}
    assert DroppingJira.seen == ["GET", "GET"]


def test_writes_are_not_retried_so_nothing_is_created_twice(dropping_jira):
    with pytest.raises(JiraError, match="POST /rest/api/2/issue failed"):
        dropping_jira.create_issue("IT", "Task", "s", "d")
    assert DroppingJira.seen == ["POST"]


def test_cloud_uses_basic_auth_and_data_center_uses_bearer():
    jira, session = client()
    assert session.auth == ("bot@example.com", "token") and jira.is_cloud
    jira, session = client(email=None)
    assert session.headers["Authorization"] == "Bearer token" and not jira.is_cloud


def test_create_issue_sends_fields_and_returns_key():
    jira, session = client(("POST", "/rest/api/2/issue", FakeResponse(201, {"key": "IT-5"})))
    key = jira.create_issue("IT", "Task", "VPN down", "desc", labels=["a", "b"], priority="High")
    assert key == "IT-5"
    fields = session.requests[0][2]["json"]["fields"]
    assert fields == {"project": {"key": "IT"}, "issuetype": {"name": "Task"}, "summary": "VPN down",
                      "description": "desc", "labels": ["a", "b"], "priority": {"name": "High"}}


def test_create_issue_retries_without_priority_when_rejected():
    jira, session = client(
        ("POST", "/rest/api/2/issue", FakeResponse(400, {"errors": {"priority": "Field 'priority' cannot be set."}})),
        ("POST", "/rest/api/2/issue", FakeResponse(201, {"key": "IT-6"})),
    )
    assert jira.create_issue("IT", "Task", "s", "d", priority="High") == "IT-6"
    assert "priority" not in session.requests[1][2]["json"]["fields"]


def test_create_issue_sets_the_reporter():
    jira, session = client(("POST", "/rest/api/2/issue", FakeResponse(201, {"key": "IT-7"})))
    jira.create_issue("IT", "Task", "s", "d", reporter={"accountId": "abc"})
    assert session.requests[0][2]["json"]["fields"]["reporter"] == {"accountId": "abc"}


def test_create_issue_drops_only_the_field_jira_rejected():
    jira, session = client(
        ("POST", "/rest/api/2/issue", FakeResponse(400, {"errors": {"reporter": "Field 'reporter' cannot be set."}})),
        ("POST", "/rest/api/2/issue", FakeResponse(201, {"key": "IT-8"})),
    )
    assert jira.create_issue("IT", "Task", "s", "d", priority="High", reporter={"accountId": "abc"}) == "IT-8"
    fields = session.requests[1][2]["json"]["fields"]
    assert "reporter" not in fields and fields["priority"] == {"name": "High"}


def test_create_issue_drops_both_optional_fields_when_jira_doesnt_say_which():
    jira, session = client(
        ("POST", "/rest/api/2/issue", FakeResponse(400, {"errorMessages": ["Something went wrong"]})),
        ("POST", "/rest/api/2/issue", FakeResponse(201, {"key": "IT-9"})),
    )
    jira.create_issue("IT", "Task", "s", "d", priority="High", reporter={"accountId": "abc"})
    assert not {"priority", "reporter"} & session.requests[1][2]["json"]["fields"].keys()


def test_create_issue_without_optional_fields_doesnt_retry():
    jira, _ = client(("POST", "/rest/api/2/issue", FakeResponse(400, {"errors": {"summary": "too long"}})))
    with pytest.raises(JiraError) as caught:
        jira.create_issue("IT", "Task", "s", "d")
    assert caught.value.fields == ("summary",)


def users(*entries):
    return FakeResponse(200, [dict(entry, active=entry.get("active", True)) for entry in entries])


@pytest.mark.parametrize("found, expected", [
    ([{"accountId": "a1", "emailAddress": "Sam@Example.com"}], {"accountId": "a1"}),
    ([{"accountId": "a1", "emailAddress": "sam@example.com.evil.io"}], None),          # a prefix match
    ([{"accountId": "a1", "displayName": "sam@example.com"}], None),                   # email hidden
    ([{"accountId": "a1", "emailAddress": "sam@example.com", "active": False}], None),
    ([{"accountId": "a1", "emailAddress": "sam@example.com"},
      {"accountId": "a2", "emailAddress": "sam@example.com"}], None),                  # ambiguous
    ([], None),
])
def test_find_user_on_cloud_needs_one_exact_visible_email(found, expected):
    jira, session = client(("GET", "/rest/api/2/user/search", users(*found)))
    assert jira.find_user("sam@example.com") == expected
    assert session.requests[0][2]["params"] == {"query": "sam@example.com", "maxResults": 20}


def test_find_user_on_data_center_searches_by_username_and_returns_the_name():
    jira, session = client(("GET", "/rest/api/2/user/search", users({"name": "sriv", "emailAddress": "sam@example.com"})),
                           email=None)
    assert jira.find_user("sam@example.com") == {"name": "sriv"}
    assert session.requests[0][2]["params"]["username"] == "sam@example.com"


def test_has_permission():
    found = {"permissions": {"MODIFY_REPORTER": {"havePermission": False}}}
    jira, session = client(("GET", "/rest/api/2/mypermissions", FakeResponse(200, found)))
    assert jira.has_permission("IT", "MODIFY_REPORTER") is False
    assert session.requests[0][2]["params"] == {"projectKey": "IT", "permissions": "MODIFY_REPORTER"}


def test_errors_include_jiras_explanation():
    jira, _ = client(("POST", "/rest/api/2/issue",
                      FakeResponse(400, {"errorMessages": ["Bad"], "errors": {"issuetype": "invalid"}})))
    with pytest.raises(JiraError, match="400: Bad; issuetype: invalid") as caught:
        jira.create_issue("IT", "Nope", "s", "d")
    assert caught.value.status == 400


def test_network_errors_become_jira_errors():
    jira, _ = client(requests.ConnectionError("no route"))
    with pytest.raises(JiraError, match="no route"):
        jira.get_issue("IT-1")


def test_transition_to_done_picks_a_done_category_transition():
    transitions = {"transitions": [
        {"id": "11", "to": {"statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "to": {"statusCategory": {"key": "done"}}},
    ]}
    jira, session = client(
        ("GET", "/rest/api/2/issue/IT-1/transitions", FakeResponse(200, transitions)),
        ("POST", "/rest/api/2/issue/IT-1/transitions", FakeResponse(204)),
    )
    assert jira.transition_to_done("IT-1") is True
    assert session.requests[0][2]["params"] == {"expand": "transitions.fields"}
    assert session.requests[1][2]["json"] == {"transition": {"id": "31"}}


def done_transition(id_, name, status, resolution_values=None):
    transition = {"id": id_, "name": name, "to": {"name": status, "statusCategory": {"key": "done"}}}
    if resolution_values is not None:
        transition["fields"] = {"resolution": {"required": True, "allowedValues": [{"name": v} for v in resolution_values]}}
    return transition


@pytest.mark.parametrize("reverse", [False, True])
def test_resolve_style_transitions_beat_cancel_in_any_order(reverse):
    transitions = [done_transition("21", "Cancel request", "Canceled"), done_transition("31", "Resolve this issue", "Resolved")]
    if reverse:
        transitions.reverse()
    assert pick_done_transition(transitions)["id"] == "31"


def test_only_cancel_style_transitions_means_no_choice():
    transitions = [done_transition("21", "Cancel request", "Canceled"), done_transition("22", "Mark duplicate", "Closed")]
    assert pick_done_transition(transitions) is None


def test_configured_transition_name_wins():
    transitions = [done_transition("31", "Resolve", "Resolved"), done_transition("41", "Ship it", "Released")]
    assert pick_done_transition(transitions, "ship IT")["id"] == "41"
    assert pick_done_transition(transitions, "Missing") is None


@pytest.mark.parametrize("values, expected", [
    (["Won't Do", "Fixed", "Duplicate"], "Fixed"),
    (["Declined", "Done"], "Done"),
    (["Works as designed"], "Works as designed"),
    ([], "Done"),
])
def test_pick_resolution(values, expected):
    assert pick_resolution([{"name": v} for v in values]) == expected


def test_transition_to_done_fills_a_required_resolution():
    transitions = {"transitions": [done_transition("31", "Resolve this issue", "Resolved", ["Won't Do", "Done"])]}
    jira, session = client(
        ("GET", "/rest/api/2/issue/IT-1/transitions", FakeResponse(200, transitions)),
        ("POST", "/rest/api/2/issue/IT-1/transitions", FakeResponse(204)),
    )
    assert jira.transition_to_done("IT-1") is True
    assert session.requests[1][2]["json"] == {"transition": {"id": "31"}, "fields": {"resolution": {"name": "Done"}}}


def test_transition_to_done_returns_false_without_a_done_transition():
    jira, _ = client(("GET", "/rest/api/2/issue/IT-1/transitions", FakeResponse(200, {"transitions": []})))
    assert jira.transition_to_done("IT-1") is False


def test_add_labels_uses_an_update_operation():
    jira, session = client(("PUT", "/rest/api/2/issue/IT-1", FakeResponse(204)))
    jira.add_labels("IT-1", ["escalated"])
    assert session.requests[0][2]["json"] == {"update": {"labels": [{"add": "escalated"}]}}


def test_cloud_search_follows_page_tokens():
    jira, session = client(
        ("GET", "/rest/api/3/search/jql", FakeResponse(200, {"issues": [{"key": "IT-1"}], "nextPageToken": "p2"})),
        ("GET", "/rest/api/3/search/jql", FakeResponse(200, {"issues": [{"key": "IT-2"}], "isLast": True})),
    )
    assert [i["key"] for i in jira.search("project = IT", ["status"])] == ["IT-1", "IT-2"]
    assert session.requests[1][2]["params"]["nextPageToken"] == "p2"


def test_search_can_expand_the_changelog():
    jira, session = client(("GET", "/rest/api/3/search/jql", FakeResponse(200, {"issues": []})))
    jira.search("project = IT", ["status"], expand="changelog")
    assert session.requests[0][2]["params"]["expand"] == "changelog"


def test_account_id_on_cloud_and_data_center():
    assert account_id({"accountId": "5b10a", "displayName": "Sam"}) == "5b10a"
    assert account_id({"key": "JIRAUSER10100", "name": "sriv"}) == "JIRAUSER10100"
    assert account_id({"name": "sriv"}) == "sriv"


def test_data_center_search_pages_by_offset():
    jira, session = client(
        ("GET", "/rest/api/2/search", FakeResponse(200, {"issues": [{"key": "IT-1"}], "total": 2})),
        ("GET", "/rest/api/2/search", FakeResponse(200, {"issues": [{"key": "IT-2"}], "total": 2})),
        email=None,
    )
    assert [i["key"] for i in jira.search("project = IT", ["status"])] == ["IT-1", "IT-2"]
    assert session.requests[1][2]["params"]["startAt"] == 1


@pytest.mark.parametrize("name, expected", [
    ("Sam Rivera", "Sam Rivera"),
    ("José O'Neil-Smith (IT)", "José O'Neil-Smith (IT)"),
    ("[Reset your password here|https://evil.example]", "Reset your password here https evil.example"),
    ("*bold* _italic_ {color:red}x{color} !img.png!", "bold italic color red x color img.png"),
    ("👻", "unknown user"),
    ("x" * 200, "x" * 80),
])
def test_safe_inline(name, expected):
    assert safe_inline(name) == expected


def test_helpers():
    assert is_done({"status": {"statusCategory": {"key": "done"}}})
    assert not is_done({"status": None})
    assert noformat("a {noformat} b") == "{noformat}\na { noformat } b\n{noformat}"
