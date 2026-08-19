"""
Tests for the year inference in workflow_service._parse_due_date.

An email states a deadline in words ("by Monday 18 August"). Turning that into a
date requires choosing a year the text never gave, anchored on Message.created_at
— which is PASTE time, not send time. The rule is therefore deliberately narrow:
take the reference's year, allow a short window into the past so a deadline that
has just gone by still reads as late, and refuse everything else.

The rule used to roll a past date FORWARD into the next year. That turned a
deadline of yesterday into one eleven months out and wrote it to the database as
a confident, wrong date — the exact failure a NULL due_date exists to prevent.

Dates here are fixed, never derived from today, so the suite does not change
behaviour as the calendar moves.
"""

from datetime import UTC, datetime

import pytest

from app.services.workflow_service import (
    DUE_DATE_BACKWARD_GRACE_DAYS,
    DUE_DATE_HOUR_UTC,
    _parse_due_date,
)

# 18 August 2026 is a TUESDAY; 17 August is the Monday. Both are load-bearing:
# the weekday check below depends on the calendar, not on a convention.
REFERENCE = datetime(2026, 8, 19, 6, 37, tzinfo=UTC)   # the real Solenne paste time


def _date(result):
    return None if result is None else result.date()


# ≈≈≈≈ the case that was reported ≈≈≈≈

def test_yesterdays_deadline_stays_in_the_current_year():
    """The Solenne row: 'by Monday 18 August' pasted on the 19th was written to
    the database as 2027-08-18. A deadline of yesterday is late, not next year."""
    result = _parse_due_date("by 18 August", REFERENCE)
    assert _date(result) == datetime(2026, 8, 18).date()


def test_a_past_deadline_is_never_rolled_into_the_next_year():
    """Whatever the rule decides, it must not answer with a date eleven months
    out. Pinned separately from the case above so a future rewrite that returns
    None here still fails loudly if it ever returns 2027."""
    result = _parse_due_date("by 18 August", REFERENCE)
    assert result is None or result.year == 2026


# ≈≈≈≈ due today: the boundary that was suspected, and was already correct ≈≈≈≈

@pytest.mark.parametrize("hour", [0, 12, 23])
def test_due_today_stays_in_the_current_year(hour):
    """Checked at both ends of the reference day: a deadline of today is due,
    and must not be treated as past for the purpose of choosing a year."""
    reference = datetime(2026, 8, 18, hour, 0, tzinfo=UTC)
    assert _date(_parse_due_date("by 18 August", reference)) == datetime(2026, 8, 18).date()


def test_due_tomorrow_stays_in_the_current_year():
    reference = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    assert _date(_parse_due_date("by 18 August", reference)) == datetime(2026, 8, 18).date()


def test_a_date_later_this_year_is_kept():
    assert _date(_parse_due_date("by 5 September", REFERENCE)) == datetime(2026, 9, 5).date()


# ≈≈≈≈ the grace window ≈≈≈≈

def test_the_last_day_inside_the_grace_window_is_kept():
    reference = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    due = reference.date().toordinal() - DUE_DATE_BACKWARD_GRACE_DAYS
    due = datetime.fromordinal(due)
    result = _parse_due_date(f"by {due:%d %B}", reference)
    assert _date(result) == due.date()


def test_the_first_day_outside_the_grace_window_is_refused():
    """Past the window the two readings stop being lopsided, so the date is
    refused and Task.due_text carries the sender's own wording instead."""
    reference = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    due = datetime.fromordinal(reference.date().toordinal() - DUE_DATE_BACKWARD_GRACE_DAYS - 1)
    assert _parse_due_date(f"by {due:%d %B}", reference) is None


def test_a_deadline_months_past_is_refused_not_rolled_forward():
    reference = datetime(2026, 12, 5, 9, 0, tzinfo=UTC)
    assert _parse_due_date("by 18 August", reference) is None


# ≈≈≈≈ the stated weekday ≈≈≈≈

def test_a_matching_weekday_parses():
    """18 August 2026 is a Tuesday."""
    result = _parse_due_date("by Tuesday 18 August", REFERENCE)
    assert _date(result) == datetime(2026, 8, 18).date()


def test_a_contradictory_weekday_is_refused():
    """The Solenne email says 'Monday 18 August', but the 18th is a Tuesday and
    Monday is the 17th. The text describes no real day, so no date is invented."""
    assert _parse_due_date("by Monday 18 August", REFERENCE) is None


def test_a_contradictory_weekday_is_refused_with_an_explicit_year():
    """18 August 2027 is a Wednesday. An explicit year removes the inference but
    not the contradiction."""
    assert _parse_due_date("by Monday 18 August 2027", REFERENCE) is None


def test_a_matching_weekday_parses_with_an_explicit_year():
    assert _date(_parse_due_date("Wednesday 18 August 2027", REFERENCE)) == \
        datetime(2027, 8, 18).date()


def test_comma_separated_weekday_still_recognised():
    assert _parse_due_date("by Monday, 18 August", REFERENCE) is None
    assert _date(_parse_due_date("by Tuesday, 18 August", REFERENCE)) == \
        datetime(2026, 8, 18).date()


def test_a_bare_weekday_is_still_refused():
    """Stripping the weekday leaves nothing to parse; it must not become a
    relative date."""
    assert _parse_due_date("by Monday", REFERENCE) is None


# ≈≈≈≈ unchanged refusals and shapes ≈≈≈≈

@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_due_text_is_refused(text):
    assert _parse_due_date(text, REFERENCE) is None


@pytest.mark.parametrize("text", [
    "tomorrow", "next week", "in 3 days",     # relative: created_at is paste time
    "11/08/2026", "08/11/2026",               # slash forms are ambiguous
    "sometime soon", "ASAP",
])
def test_unparseable_forms_are_refused(text):
    assert _parse_due_date(text, REFERENCE) is None


@pytest.mark.parametrize("text,expected", [
    ("August 20, 2026", datetime(2026, 8, 20)),
    ("Aug 20, 2026", datetime(2026, 8, 20)),
    ("20 August 2026", datetime(2026, 8, 20)),
    ("20 Aug 2026", datetime(2026, 8, 20)),
    ("2026-08-20", datetime(2026, 8, 20)),
    ("August 20", datetime(2026, 8, 20)),
    ("20 Aug", datetime(2026, 8, 20)),
])
def test_supported_formats_still_parse(text, expected):
    assert _date(_parse_due_date(text, REFERENCE)) == expected.date()


@pytest.mark.parametrize("prefix", ["by", "before", "on", "due", "no later than"])
def test_leading_prepositions_are_stripped(prefix):
    assert _date(_parse_due_date(f"{prefix} 20 August", REFERENCE)) == \
        datetime(2026, 8, 20).date()


def test_result_is_anchored_at_noon_utc():
    """Noon, so localtime.js cannot shift the stated day for viewers west of UTC."""
    result = _parse_due_date("by 20 August", REFERENCE)
    assert result.hour == DUE_DATE_HOUR_UTC
    assert (result.minute, result.second, result.microsecond) == (0, 0, 0)
    assert result.tzinfo is UTC


def test_an_explicit_past_year_is_kept_untouched():
    """An explicit year is a statement, not an inference — the grace window has
    no business overriding it."""
    assert _date(_parse_due_date("18 August 2024", REFERENCE)) == datetime(2024, 8, 18).date()
