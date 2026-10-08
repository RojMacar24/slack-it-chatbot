import json
from types import SimpleNamespace

import pytest

from helpdesk.assistant import Assistant, CutOffAnswer, Triage, is_small_talk, keyword_triage


@pytest.mark.parametrize("text", [
    "Hi team", "hello :wave:", "Good morning all!", "quick question", "I need help", "Is anyone around?", "👋",
    "hey, can someone help please", "have a problem", "I have an issue with something", "having some trouble",
    "it's not working", "There's a problem", "this is broken",
])
def test_small_talk(text):
    assert is_small_talk(text)


@pytest.mark.parametrize("text", [
    "Hi, my VPN is down", "help, laptop won't boot", "Need access to Jira",
    "hi team, the printer on floor 3 is jammed again", "VPN not working", "problem with Outlook",
    "my laptop is broken", "keyboard not working",
])
def test_not_small_talk(text):
    assert not is_small_talk(text)


@pytest.mark.parametrize("text, kind, category", [
    ("I need access to the finance dashboard", "access-request", "other"),
    ("Requesting a Figma license for the design team", "access-request", "software"),
    ("Please add me to the #eng-oncall group", "access-request", "other"),
    ("Can you install Docker on my laptop?", "change-request", "hardware"),
    ("Could you please set up a shared mailbox", "change-request", "email"),
    ("I can't access my email", "incident", "email"),
    ("I need help to access the VPN", "incident", "network"),
    ("Can you help me, my laptop won't update", "incident", "hardware"),
    ("What happened to my laptop?", "incident", "hardware"),
    ("VPN won't connect from home", "incident", "network"),
    ("Locked out after too many login attempts", "incident", "account"),
    ("Got a suspicious email asking for my password", "incident", "security"),
    ("Zoom keeps crashing", "incident", "software"),
    ("Something is weird", "incident", "other"),
])
def test_keyword_triage(text, kind, category):
    triage = keyword_triage(text)
    assert (triage.kind, triage.category) == (kind, category)


def test_keyword_triage_priority_and_summary():
    assert keyword_triage("URGENT: whole team is offline").priority == "High"
    assert keyword_triage("Phishing email in my inbox").priority == "High"
    assert keyword_triage("Mouse is a bit laggy").priority == "Medium"
    long = "  My   monitor flickers " + "a lot " * 40 + "\nsecond line"
    summary = keyword_triage(long).summary
    assert summary.startswith("My monitor flickers a lot") and len(summary) == 120
    assert keyword_triage("\n\n").summary == "IT request from Slack"


class FakeOpenAI:
    """Mimics client.chat.completions.create and records the calls."""

    def __init__(self, content=None, error=None, finish_reason="stop"):
        self.content, self.error, self.finish_reason, self.calls = content, error, finish_reason, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        choice = SimpleNamespace(message=SimpleNamespace(content=self.content), finish_reason=self.finish_reason)
        return SimpleNamespace(choices=[choice])


def test_assess_uses_the_model_answer():
    client = FakeOpenAI(json.dumps({
        "kind": "incident", "category": "Network", "priority": "high",
        "summary": "VPN drops every few minutes", "reply": "Try reconnecting.",
    }))
    triage, reply = Assistant(client=client, environment="VPN: WireGuard").assess("vpn keeps dropping")

    assert triage == Triage("incident", "network", "High", "VPN drops every few minutes")
    assert reply == "Try reconnecting."
    call = client.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert "VPN: WireGuard" in call["messages"][0]["content"]
    assert call["messages"][1] == {"role": "user", "content": "vpn keeps dropping"}


def test_assess_falls_back_on_invalid_values():
    client = FakeOpenAI(json.dumps({"kind": "banana", "category": 7, "priority": "urgent!!", "summary": "", "reply": 3}))
    triage, reply = Assistant(client=client).assess("VPN won't connect")
    assert triage == keyword_triage("VPN won't connect")
    assert reply == ""


def test_assess_drops_reply_for_requests():
    client = FakeOpenAI(json.dumps({"kind": "access-request", "category": "software", "priority": "Low",
                                    "summary": "Figma licence", "reply": "Here are steps"}))
    triage, reply = Assistant(client=client).assess("I need a Figma license")
    assert triage.kind == "access-request" and reply == ""


@pytest.mark.parametrize("client", [FakeOpenAI(error=RuntimeError("API down")), FakeOpenAI("not json")])
def test_assess_survives_model_failures(client):
    triage, reply = Assistant(client=client).assess("Printer is jammed")
    assert triage == keyword_triage("Printer is jammed") and reply == ""


def test_default_model_and_token_limits():
    client = FakeOpenAI(json.dumps({"kind": "incident", "category": "network", "priority": "High",
                                    "summary": "VPN down", "reply": "Try this."}))
    assistant = Assistant(client=client)
    assert assistant.model == "gpt-6-luna"
    assistant.assess("VPN down")
    assistant.follow_up("IT-1", [{"role": "user", "content": "Still down"}])
    assert [(call["model"], call["max_completion_tokens"]) for call in client.calls] == [
        ("gpt-6-luna", 2000), ("gpt-6-luna", 1500)]


def test_cut_off_triage_falls_back_to_keyword_rules():
    # A cut-off JSON answer can still parse if it's cut between fields, so the finish reason decides, not json.loads
    client = FakeOpenAI(json.dumps({"kind": "change-request", "category": "other", "priority": "Low",
                                    "summary": "Partial", "reply": ""}), finish_reason="length")
    assert Assistant(client=client).assess("VPN won't connect") == (keyword_triage("VPN won't connect"), "")


def test_cut_off_follow_up_is_never_returned():
    client = FakeOpenAI("1. Restart the VPN client. 2. Check your", finish_reason="length")
    with pytest.raises(CutOffAnswer, match="gpt-6-luna ran out of room after 1500 tokens"):
        Assistant(client=client).follow_up("IT-7", [{"role": "user", "content": "Still failing"}])


def test_openai_client_gives_up_quickly():
    client = Assistant(api_key="sk-test-not-a-real-key")._client
    assert (client.timeout, client.max_retries) == (15, 1)


def test_without_a_key_the_assistant_is_off():
    assistant = Assistant(api_key=None)
    assert not assistant.enabled
    assert assistant.assess("VPN down") == (keyword_triage("VPN down"), "")


def test_follow_up_sends_history_after_the_system_prompt():
    client = FakeOpenAI("Try this next.")
    history = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restart it"}]
    assert Assistant(client=client).follow_up("IT-7", history) == "Try this next."
    messages = client.calls[0]["messages"]
    assert "IT-7" in messages[0]["content"] and messages[1:] == history
