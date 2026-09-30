import copy
import json

import pytest
import requests

from jira_client import JiraClient, JiraError, is_done, noformat, pick_done_transition, pick_resolution


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


def test_data_center_search_pages_by_offset():
    jira, session = client(
        ("GET", "/rest/api/2/search", FakeResponse(200, {"issues": [{"key": "IT-1"}], "total": 2})),
        ("GET", "/rest/api/2/search", FakeResponse(200, {"issues": [{"key": "IT-2"}], "total": 2})),
        email=None,
    )
    assert [i["key"] for i in jira.search("project = IT", ["status"])] == ["IT-1", "IT-2"]
    assert session.requests[1][2]["params"]["startAt"] == 1


def test_helpers():
    assert is_done({"status": {"statusCategory": {"key": "done"}}})
    assert not is_done({"status": None})
    assert noformat("a {noformat} b") == "{noformat}\na { noformat } b\n{noformat}"
