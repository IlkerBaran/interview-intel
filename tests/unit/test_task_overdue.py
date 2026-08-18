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

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import Message, MessageStatus, Task, User

# Both templates render the badge as "Overdue ·". Matching the bare word would
# also hit the CSS comment in show_all_tasks.html that explains the state.
OVERDUE_BADGE = "Overdue \u00b7".encode()


def _add_task(user, due_date, *, is_completed=False, task_name="Confirm interview time"):
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
