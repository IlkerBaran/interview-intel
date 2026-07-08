"""
Stage 3 unit tests for the analyze_message task, the status endpoint, the retry
backoff, and the reaper.

Covers: re-fetch/skip-if-done guard, transient-vs-permanent classification,
retries-exhausted → FAILED, enqueue-fail → FAILED, the exact status-string, and the
stuck-message reaper.
"""

import anthropic
import httpx
from celery.exceptions import SoftTimeLimitExceeded

import app.celery_tasks as tasks
from app.extensions import db
from app.models import Message, MessageStatus, User


def _make_user(email="analyze@example.com"):
    user = User()
    user.email = email
    user.password = "x"
    user.is_verified = True
    db.session.add(user)
    db.session.commit()
    return user


def _make_message(status=MessageStatus.PENDING, raw="Interview invite for the SWE role."):
    user = _make_user(email=f"u{db.session.query(User).count()}@example.com")
    msg = Message(user_id=user.id, raw_text=raw, status=status)
    db.session.add(msg)
    db.session.commit()
    return msg


def test_missing_message_is_skipped(app):
    # No error, no rows — the None-guard returns before touching the pipeline.
    tasks.analyze_message.apply(args=[999999], throw=True)


def test_skip_if_completed(app, monkeypatch):
    called = []
    monkeypatch.setattr(
        "app.services.workflow_service.run_message_analysis",
        lambda m: called.append(1),
    )
    msg = _make_message(status=MessageStatus.COMPLETED)
    tasks.analyze_message.apply(args=[msg.id], throw=True)
    assert called == []                                  # guard skipped it


def test_transient_retries_exhausted_marks_failed(app, monkeypatch):
    from app.services.llm_service import llm_service
    llm_service._client = object()                       # is_loaded → True
    calls = []
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def always_timeout(*a, **k):
        calls.append(1)
        raise anthropic.APITimeoutError(request=req)     # transient

    monkeypatch.setattr(llm_service, "extract_interview_details", always_timeout)

    msg = _make_message()
    result = tasks.analyze_message.apply(args=[msg.id], throw=False)

    assert len(calls) == 4                               # 1 + max_retries(3)
    assert result.successful()                           # terminal-marked, not FAILURE
    db.session.refresh(msg)
    assert msg.status == MessageStatus.FAILED            # not stuck PROCESSING


def test_permanent_error_marks_failed(app, monkeypatch):
    from app.services.llm_service import llm_service
    llm_service._client = object()
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def bad_request(*a, **k):
        raise anthropic.BadRequestError(
            "bad", response=httpx.Response(400, request=req), body=None)

    monkeypatch.setattr(llm_service, "extract_interview_details", bad_request)

    msg = _make_message()
    # ML+LLM both produce nothing (ML unloaded in tests, extraction permanently fails)
    tasks.analyze_message.apply(args=[msg.id], throw=True)
    db.session.refresh(msg)
    assert msg.status == MessageStatus.FAILED            # nothing-to-show → FAILED


def test_soft_time_limit_marks_failed(app, monkeypatch):
    def raise_soft(_m):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(
        "app.services.workflow_service.run_message_analysis", raise_soft)

    msg = _make_message()
    tasks.analyze_message.apply(args=[msg.id], throw=True)
    db.session.refresh(msg)
    assert msg.status == MessageStatus.FAILED


def test_enqueue_failure_marks_message_failed(app, client_logged_in, monkeypatch):
    def boom_delay(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(tasks.analyze_message, "delay", boom_delay)

    client_logged_in.post("/messages/new", data={
        "raw_text": "We would like to invite you to interview for the SWE role.",
        "subject": "Interview",
        "sender_email": "",
    })

    msg = db.session.execute(db.select(Message)).scalars().first()
    assert msg is not None
    assert msg.status == MessageStatus.FAILED            # not left PENDING


def test_status_endpoint_returns_value_string(app, client_logged_in):
    # Create a message owned by the logged-in user, mark COMPLETED, hit the endpoint.
    user = db.session.execute(db.select(User).where(
        User.email == "route_user@example.com")).scalar_one()
    msg = Message(user_id=user.id, raw_text="hello", status=MessageStatus.COMPLETED)
    db.session.add(msg)
    db.session.commit()

    resp = client_logged_in.get(f"/messages/{msg.id}/status")
    assert resp.get_json() == {"status": "completed"}    # exact value string, not str(enum)


def test_retry_countdown_backs_off_15_30_60():
    assert 15 <= tasks._analysis_retry_countdown(0) < 18
    assert 30 <= tasks._analysis_retry_countdown(1) < 36
    assert 60 <= tasks._analysis_retry_countdown(2) < 72


def test_reaper_marks_only_stale_non_terminal(app):
    from datetime import datetime, UTC, timedelta

    user = _make_user(email="reap@example.com")
    stale_pending = Message(user_id=user.id, raw_text="a", status=MessageStatus.PENDING)
    stale_processing = Message(user_id=user.id, raw_text="b", status=MessageStatus.PROCESSING)
    fresh_processing = Message(user_id=user.id, raw_text="c", status=MessageStatus.PROCESSING)
    completed = Message(user_id=user.id, raw_text="d", status=MessageStatus.COMPLETED)
    db.session.add_all([stale_pending, stale_processing, fresh_processing, completed])
    db.session.commit()

    # Push only the two stale rows' updated_at into the past. Setting updated_at
    # explicitly in a Core UPDATE bypasses the onupdate=now default, so it persists.
    old = datetime.now(UTC) - timedelta(hours=1)
    db.session.execute(
        db.update(Message)
        .where(Message.id.in_([stale_pending.id, stale_processing.id]))
        .values(updated_at=old)
    )
    db.session.commit()

    count = tasks.sweep_stuck_analyses.apply(throw=True).result

    for m in (stale_pending, stale_processing, fresh_processing, completed):
        db.session.refresh(m)
    assert count == 2
    assert stale_pending.status == MessageStatus.FAILED
    assert stale_processing.status == MessageStatus.FAILED
    assert fresh_processing.status == MessageStatus.PROCESSING   # too recent — untouched
    assert completed.status == MessageStatus.COMPLETED           # terminal — untouched
