"""
Regression tests for scalar extraction fields before they reach the database.

These tests protect two things:

1. Fields stored in String(n) columns are capped to the matching database width
   so PostgreSQL does not reject an overlong LLM value.

2. location_text is intentionally left uncapped because it uses db.Text and may
   contain long meeting URLs.

test_caps_match_column_widths also keeps
MAX_EXTRACTION_FIELD_LENGTHS in sync with the AnalysisResult column sizes.
"""

import pytest

from app.models import AnalysisResult
from app.services.llm_service import (
    LLMService,
    MAX_EXTRACTION_FIELD_LENGTHS,
)

_coerce = LLMService._coerce_scalar

# A realistic worst case: a Teams join URL plus the dial-in fallback that almost
# always accompanies it. 295 characters — 40 over the old String(255) column.
TEAMS_LOCATION = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_"
    "NGU0YzM2MjItZmY1Zi00YTc4LWE0MmMtOTM4ZmYyZDVlZjNi%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%2272f988bf-86f1-41af-91ab-2d7cd011db47%22%2c"
    "%22Oid%22%3a%22a1b2c3d4-e5f6-7890-abcd-ef1234567890%22%7d"
    " or dial +1 323-555-0142, conference ID 284 916 553#"
)

SCALAR_FIELDS = [
    "company_name",
    "role_title",
    "interview_stage",
    "interview_format",
    "date_text",
    "time_text",
    "location_text",
]


# ── location_text must never be truncated ──────────────────────────────────────

def test_location_text_is_not_truncated():
    """A meeting URL longer than the old 255 column survives intact."""
    assert len(TEAMS_LOCATION) > 255, "fixture must exceed the old column width"

    result = _coerce({"location_text": TEAMS_LOCATION}, "location_text")

    assert result == TEAMS_LOCATION
    assert len(result) == len(TEAMS_LOCATION)


def test_location_text_has_no_configured_cap():
    """location_text is db.Text — absence from the mapping is the mechanism."""
    assert "location_text" not in MAX_EXTRACTION_FIELD_LENGTHS


def test_long_location_text_round_trips_through_the_database(app, db):
    """The column really is unbounded: >255 chars persists and reads back whole."""
    from app.models import Message, User

    user = User()
    user.email = "extraction@example.com"
    user.password = "T3st-secret*"
    user.is_verified = True
    db.session.add(user)
    db.session.commit()

    message = Message(user_id=user.id, raw_text="interview invite", status="completed")
    db.session.add(message)
    db.session.commit()

    db.session.add(AnalysisResult(message_id=message.id, location_text=TEAMS_LOCATION))
    db.session.commit()
    db.session.expire_all()

    stored = db.session.query(AnalysisResult).filter_by(message_id=message.id).one()
    assert stored.location_text == TEAMS_LOCATION
    assert len(stored.location_text) > 255


# ── bounded fields are capped to their column width ────────────────────────────

@pytest.mark.parametrize("field,limit", sorted(MAX_EXTRACTION_FIELD_LENGTHS.items()))
def test_bounded_fields_are_capped(field, limit):
    overlong = "X" * (limit + 50)

    result = _coerce({field: overlong}, field)

    assert len(result) == limit


def test_caps_match_column_widths():
    """
    Anti-drift: the code caps must equal the actual String(n) widths, and every
    field NOT capped must be an unbounded column type.
    """
    columns = AnalysisResult.__table__.columns

    for field, limit in MAX_EXTRACTION_FIELD_LENGTHS.items():
        assert columns[field].type.length == limit, (
            f"{field}: cap {limit} != column width {columns[field].type.length}"
        )

    for field in SCALAR_FIELDS:
        if field not in MAX_EXTRACTION_FIELD_LENGTHS:
            assert getattr(columns[field].type, "length", None) is None, (
                f"{field} has no cap but is a bounded column — it would truncate in "
                f"PostgreSQL without a code backstop"
            )


# ── normal values are untouched ────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("company_name", "Stripe"),
    ("role_title", "Software Engineer, Platform"),
    ("interview_stage", "technical interview"),
    ("interview_format", "Zoom"),
    ("date_text", "Thursday, 27 August"),
    ("time_text", "11:00 AM PT"),
    ("location_text", "123 Main St, San Francisco"),
])
def test_normal_values_pass_through_unchanged(field, value):
    assert _coerce({field: value}, field) == value


def test_multi_slot_scheduling_text_is_preserved():
    """The case that motivated widening date_text/time_text to 255."""
    date_text = ("Tuesday August 12, Wednesday August 13, or Thursday August 14, "
                 "whichever suits you best")
    time_text = ("9:00 AM, 11:00 AM or 2:00 PM Pacific Time / 5:00 PM, 7:00 PM or "
                 "10:00 PM Central European Time")

    assert _coerce({"date_text": date_text}, "date_text") == date_text
    assert _coerce({"time_text": time_text}, "time_text") == time_text


def test_surrounding_whitespace_is_stripped():
    assert _coerce({"company_name": "  Acme  "}, "company_name") == "Acme"


# ── type safety: nothing but a non-empty string survives ───────────────────────

@pytest.mark.parametrize("value", [None, 42, True, 3.5, {"a": 1}, ["x"], "", "   "])
def test_non_string_and_blank_values_become_none(value):
    assert _coerce({"company_name": value}, "company_name") is None


def test_missing_key_becomes_none():
    assert _coerce({}, "company_name") is None


@pytest.mark.parametrize("parsed", [["not", "an", "object"], "a bare string", 7, None])
def test_non_dict_payload_does_not_raise(parsed):
    """_safe_json_load returns whatever valid JSON the model produced."""
    assert _coerce(parsed, "company_name") is None


# ── end-to-end through extract_interview_details ───────────────────────────────

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


def test_extraction_applies_coercion_end_to_end():
    """The wiring, not just the helper: caps applied, location_text left whole."""
    import json

    svc = _service_returning(json.dumps({
        "company_name": "A" * 400,
        "role_title": "Software Engineer",
        "interview_stage": "S" * 300,
        "interview_format": "onsite",
        "date_text": "Thursday, 27 August",
        "time_text": "11:00 AM PT",
        "location_text": TEAMS_LOCATION,
        "action_items": [],
    }))

    details = svc.extract_interview_details("some interview email")

    assert len(details["company_name"]) == MAX_EXTRACTION_FIELD_LENGTHS["company_name"]
    assert len(details["interview_stage"]) == MAX_EXTRACTION_FIELD_LENGTHS["interview_stage"]
    assert details["location_text"] == TEAMS_LOCATION
    assert details["role_title"] == "Software Engineer"
    assert details["time_text"] == "11:00 AM PT"


def test_extraction_returns_complete_schema_on_unparseable_response():
    """Degraded extraction still yields all 8 keys, with None for the scalars."""
    svc = _service_returning("this is not JSON at all")

    details = svc.extract_interview_details("some interview email")

    assert set(details) == set(SCALAR_FIELDS) | {"action_items"}
    for field in SCALAR_FIELDS:
        assert details[field] is None
    assert details["action_items"] == []
