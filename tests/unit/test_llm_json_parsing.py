"""
Regression tests for the _safe_json_load() shape contract.

The method is annotated -> Dict[str, Any] and its caller indexes the result by key.
Previously the direct-parse branch returned whatever json.loads() produced, so a
model reply that was valid JSON but not an object (a list, string, number, boolean
or null) escaped as a non-dict and raised AttributeError on .get() inside
extract_interview_details().

test_extraction_survives_* is the end-to-end proof: those calls raised before the
fix and now degrade to the complete all-None schema.
"""

import json

import pytest

from app.services.llm_service import LLMService

_parse = LLMService()._safe_json_load

# Valid JSON that is NOT an object. Each must degrade to {}.
NON_OBJECT_PAYLOADS = [
    pytest.param("[1, 2, 3]", id="list"),
    pytest.param("[]", id="empty-list"),
    pytest.param('"just a string"', id="string"),
    pytest.param("42", id="number"),
    pytest.param("3.5", id="float"),
    pytest.param("true", id="boolean-true"),
    pytest.param("false", id="boolean-false"),
    pytest.param("null", id="null"),
]


# ── the shape contract ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS)
def test_non_object_json_becomes_empty_dict(payload):
    assert _parse(payload) == {}


@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS)
def test_non_object_json_is_still_a_dict(payload):
    """The contract is the type, not just the value."""
    assert isinstance(_parse(payload), dict)


@pytest.mark.parametrize("payload", [None, "", "   ", "not json at all", "{unclosed"])
def test_unparseable_input_becomes_empty_dict(payload):
    assert _parse(payload) == {}


# ── objects still parse exactly as before ──────────────────────────────────────

def test_object_is_returned_unchanged():
    assert _parse('{"company_name": "Acme"}') == {"company_name": "Acme"}


def test_nested_object_is_preserved():
    assert _parse('{"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}


def test_empty_object_is_returned_as_empty_dict():
    assert _parse("{}") == {}


def test_object_wrapped_in_prose_is_still_recovered():
    """Strategy 2 is untouched: extra text around the JSON block still parses."""
    raw = 'Here you go:\n{"company_name": "Acme"}\nHope that helps!'

    assert _parse(raw) == {"company_name": "Acme"}


def test_object_inside_a_list_is_recovered():
    """
    Documents a deliberate choice: the direct parse yields a list, then falls through
    to the brace-depth walk rather than short-circuiting to {}. An object wrapped in
    an array is recoverable data, so it is recovered instead of discarded.
    """
    assert _parse('[{"company_name": "Acme"}]') == {"company_name": "Acme"}


# ── end-to-end: the AttributeError that started this ───────────────────────────

class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, payload):
        self._payload = payload

    def create(self, **kwargs):
        return _FakeResp(self._payload)


class _FakeClient:
    def __init__(self, payload):
        self.messages = _FakeMessages(payload)


def _service_returning(payload):
    svc = LLMService()
    svc._client = _FakeClient(payload)
    svc._model = "test-model"
    svc._fake = False
    return svc


EXPECTED_KEYS = {
    "company_name", "role_title", "interview_stage", "interview_format",
    "date_text", "time_text", "location_text", "action_items",
}


@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS)
def test_extraction_survives_non_object_payload(payload):
    """Raised AttributeError before the fix; now degrades to the full schema."""
    details = _service_returning(payload).extract_interview_details("an email")

    assert set(details) == EXPECTED_KEYS
    assert details["action_items"] == []
    assert all(details[k] is None for k in EXPECTED_KEYS - {"action_items"})


def test_extraction_of_a_normal_object_is_unchanged():
    """Guards against the fix altering the happy path."""
    svc = _service_returning(json.dumps({
        "company_name": "Acme Corp",
        "role_title": "Software Engineer",
        "interview_stage": "final",
        "interview_format": "onsite",
        "date_text": "next Tuesday",
        "time_text": "10:00 AM",
        "location_text": "123 Main St, San Francisco",
        "action_items": [{"action": "Confirm your slot", "due_text": "Friday"}],
    }))

    details = svc.extract_interview_details("an email")

    assert details["company_name"] == "Acme Corp"
    assert details["role_title"] == "Software Engineer"
    assert details["interview_stage"] == "final"
    assert details["interview_format"] == "onsite"
    assert details["date_text"] == "next Tuesday"
    assert details["time_text"] == "10:00 AM"
    assert details["location_text"] == "123 Main St, San Francisco"
    assert details["action_items"] == [
        {"action": "Confirm your slot", "due_text": "Friday"}
    ]
