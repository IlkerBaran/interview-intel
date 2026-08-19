"""
Regression tests for the overdue check on Task.due_date.

due_date is written timezone-AWARE (noon UTC, workflow_service.DUE_DATE_HOUR_UTC)
but SQLite has no tz storage: it drops the offset on write and returns the value
NAIVE. Comparing that against datetime.now(UTC) raised
"TypeError: can't compare offset-naive and offset-aware datetimes" inside the
render, which 500'd /dashboard and /tasks for anyone with a dated task.

Task.is_overdue re-attaches UTC on read (utils.ensure_aware) and owns the rule for
both pages. These tests pin the property AND the two renders — a template that
compares due_date to a current time itself would pass the first and fail the second.
"""

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app import models
from app.extensions import db
from app.models import Message, MessageStatus, Task, User

# Both templates render the badge as "Overdue ·". Matching the bare word would
# also hit the CSS comment in show_all_tasks.html that explains the state.
OVERDUE_BADGE = "Overdue \u00b7".encode()


def _add_task(user, due_date, *, due_text=None, is_completed=False,
              task_name="Confirm interview time"):
    """Persist one task for `user`, then expire the session so the next read
    comes back exactly as SQLite hands it over — naive. Reading through the
    identity map instead would keep the aware value in memory and hide the bug."""
    message = Message(
        user_id=user.id,
        subject="Interview invitation",
        sender_email="recruiter@example.com",
        raw_text="Please confirm the time.",
        status=MessageStatus.COMPLETED,
    )
    db.session.add(message)
    db.session.flush()

    task = Task(
        message_id=message.id,
        task_name=task_name,
        due_date=due_date,
        due_text=due_text,
        is_completed=is_completed,
    )
    db.session.add(task)
    db.session.commit()
    db.session.expire_all()
    return task


@pytest.fixture
def user(client_logged_in):
    """The verified user behind the client_logged_in fixture."""
    return db.session.execute(
        db.select(User).where(User.email == "route_user@example.com")
    ).scalar_one()


# ≈≈≈≈ the precondition the bug rested on ≈≈≈≈

def test_stored_due_date_reads_back_naive(user):
    """Documents the trigger: an aware write returns naive under SQLite."""
    task = _add_task(user, datetime.now(UTC) - timedelta(days=2))
    assert task.due_date.tzinfo is None


# ≈≈≈≈ Task.is_overdue ≈≈≈≈

def test_is_overdue_true_for_past_due_date(user):
    task = _add_task(user, datetime.now(UTC) - timedelta(days=2))
    assert task.is_overdue is True


def test_is_overdue_false_for_future_due_date(user):
    task = _add_task(user, datetime.now(UTC) + timedelta(days=2))
    assert task.is_overdue is False


def test_is_overdue_false_without_due_date(user):
    """_parse_due_date returns None rather than guess; a refusal is not a red flag."""
    task = _add_task(user, None)
    assert task.is_overdue is False


def test_is_overdue_false_when_completed(user):
    task = _add_task(user, datetime.now(UTC) - timedelta(days=2), is_completed=True)
    assert task.is_overdue is False


# ≈≈≈≈ the two pages that 500'd ≈≈≈≈

@pytest.mark.parametrize("path", ["/tasks/", "/dashboard/"])
def test_pages_render_with_overdue_task(client_logged_in, user, path):
    _add_task(user, datetime.now(UTC) - timedelta(days=2))
    response = client_logged_in.get(path)
    assert response.status_code == 200
    assert OVERDUE_BADGE in response.data


@pytest.mark.parametrize("path", ["/tasks/", "/dashboard/"])
def test_pages_render_future_task_without_overdue(client_logged_in, user, path):
    _add_task(user, datetime.now(UTC) + timedelta(days=2))
    response = client_logged_in.get(path)
    assert response.status_code == 200
    assert OVERDUE_BADGE not in response.data


@pytest.mark.parametrize("path", ["/tasks/", "/dashboard/"])
def test_pages_render_with_null_due_date(client_logged_in, user, path):
    _add_task(user, None)
    response = client_logged_in.get(path)
    assert response.status_code == 200
    assert OVERDUE_BADGE not in response.data


# ≈≈≈≈ clock and timezone control ≈≈≈≈

@pytest.fixture
def frozen_now(monkeypatch):
    """Pin the clock that `Task.is_overdue` reads.

    The property calls `datetime.now(UTC)` directly, so the interesting instants —
    the first and last second of the due day — can only be tested by controlling
    the clock rather than waiting for it.

    `now()` with no argument returns naive LOCAL time, exactly as the real
    `datetime.now()` does. That is what makes `server_timezone` below meaningful:
    an implementation that read the local calendar instead of UTC would answer
    differently per deployment, and this freeze would not hide it.
    """
    def _freeze(instant):
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return instant.astimezone().replace(tzinfo=None)
                return instant.astimezone(tz)

        monkeypatch.setattr(models, "datetime", _FrozenDatetime)

    return _freeze


@pytest.fixture
def server_timezone():
    """Run the process under a given TZ, restoring the original afterwards.

    Not monkeypatch.setenv: this fixture's teardown must run tzset() AFTER the
    variable is restored, and monkeypatch undoes its own patches after dependent
    fixtures have already finalised.
    """
    original = os.environ.get("TZ")

    def _set(name):
        os.environ["TZ"] = name
        time.tzset()

    yield _set

    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


# ≈≈≈≈ the due-day boundary ≈≈≈≈
# due_date is written at noon UTC (workflow_service.DUE_DATE_HOUR_UTC) because an
# email states a day, never a time. Comparing it as an INSTANT therefore reported
# "overdue" from 12:00 on the due day itself — half a day early. The rule is a
# calendar-date comparison: due today means due, overdue starts the next day.

DUE_DAY = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)   # "confirm by August 18"


def test_not_overdue_the_day_before(user, frozen_now):
    frozen_now(datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
    assert _add_task(user, DUE_DAY).is_overdue is False


def test_not_overdue_at_the_first_instant_of_the_due_day(user, frozen_now):
    frozen_now(datetime(2026, 8, 18, 0, 0, 0, tzinfo=UTC))
    assert _add_task(user, DUE_DAY).is_overdue is False


def test_not_overdue_one_second_past_the_stored_noon(user, frozen_now):
    """The exact instant the old instant-comparison flipped."""
    frozen_now(datetime(2026, 8, 18, 12, 0, 1, tzinfo=UTC))
    assert _add_task(user, DUE_DAY).is_overdue is False


def test_not_overdue_at_the_last_instant_of_the_due_day(user, frozen_now):
    frozen_now(datetime(2026, 8, 18, 23, 59, 59, tzinfo=UTC))
    assert _add_task(user, DUE_DAY).is_overdue is False


def test_overdue_at_the_first_instant_of_the_next_day(user, frozen_now):
    frozen_now(datetime(2026, 8, 19, 0, 0, 0, tzinfo=UTC))
    assert _add_task(user, DUE_DAY).is_overdue is True


@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Pacific/Kiritimati"])
def test_answer_does_not_depend_on_the_server_timezone(user, frozen_now, server_timezone, tz):
    """03:00 UTC on the 19th is still the 18th in Los Angeles (UTC-7) and already
    the 19th in Kiritimati (UTC+14). A task due the 18th is overdue in all three:
    the state belongs to the task, not to where the process happens to run."""
    server_timezone(tz)
    frozen_now(datetime(2026, 8, 19, 3, 0, tzinfo=UTC))
    assert _add_task(user, DUE_DAY).is_overdue is True


@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Pacific/Kiritimati"])
def test_due_day_is_not_overdue_in_any_server_timezone(user, frozen_now, server_timezone, tz):
    server_timezone(tz)
    frozen_now(datetime(2026, 8, 18, 23, 59, 59, tzinfo=UTC))
    assert _add_task(user, DUE_DAY).is_overdue is False


# ≈≈≈≈ tasks carrying no date to compare ≈≈≈≈

def test_no_due_date_and_no_due_text_is_never_overdue(user, frozen_now):
    """Neither field set — the shape of every task written before due dates were
    parsed at all. Must answer False, not raise."""
    frozen_now(datetime(2026, 8, 19, 3, 0, tzinfo=UTC))
    task = _add_task(user, None)
    assert task.due_date is None
    assert task.due_text is None
    assert task.is_overdue is False


def test_due_text_only_is_never_overdue(user, frozen_now):
    """due_text is the email's own wording ("before the call"). _parse_due_date
    refused to turn it into a date, so there is nothing to compare and the task
    can never be late."""
    frozen_now(datetime(2026, 8, 19, 3, 0, tzinfo=UTC))
    task = _add_task(user, None, due_text="before the call")
    assert task.due_date is None
    assert task.due_text == "before the call"
    assert task.is_overdue is False


# ≈≈≈≈ the four pages that read the property ≈≈≈≈
# A template that compared due_date itself would pass the property tests above and
# fail these.

def _page_paths(task):
    return ["/tasks/", "/dashboard/", f"/messages/{task.message_id}",
            f"/tasks/{task.id}"]


def test_no_page_shows_overdue_on_the_due_day(client_logged_in, user, frozen_now):
    frozen_now(datetime(2026, 8, 18, 23, 59, 59, tzinfo=UTC))
    task = _add_task(user, DUE_DAY)
    for path in _page_paths(task):
        response = client_logged_in.get(path)
        assert response.status_code == 200, path
        assert OVERDUE_BADGE not in response.data, path


def test_pages_show_overdue_the_day_after(client_logged_in, user, frozen_now):
    """/tasks/<id> is excluded from the badge assertion on purpose: the single
    task page renders the due date but has never rendered an overdue state."""
    frozen_now(datetime(2026, 8, 19, 0, 0, 1, tzinfo=UTC))
    task = _add_task(user, DUE_DAY)
    for path in ["/tasks/", "/dashboard/", f"/messages/{task.message_id}"]:
        response = client_logged_in.get(path)
        assert response.status_code == 200, path
        assert OVERDUE_BADGE in response.data, path

    single = client_logged_in.get(f"/tasks/{task.id}")
    assert single.status_code == 200


def test_all_pages_render_with_a_bare_task(client_logged_in, user, frozen_now):
    """No due_date, no due_text: every page renders, none claims overdue."""
    frozen_now(datetime(2026, 8, 19, 3, 0, tzinfo=UTC))
    task = _add_task(user, None)
    for path in _page_paths(task):
        response = client_logged_in.get(path)
        assert response.status_code == 200, path
        assert OVERDUE_BADGE not in response.data, path
