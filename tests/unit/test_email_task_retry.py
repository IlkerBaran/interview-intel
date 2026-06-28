"""
Stage 2 regression tests for email task RETRY classification (ADR-0007).

A transient Resend failure — which includes EVERY network/transport error, because the
SDK wraps those as a base ResendError (code 500, "HttpClientError") — must be RETRIED,
not silently dropped. acks_late + autoretry_for=(ResendError,) with max_retries=3 means
the send is attempted 1 + 3 = 4 times; if it never succeeds the task ends in FAILURE (loud).

A permanent failure (ValidationError, or a missing/invalid API key) must NOT retry: the
task catches it in-body, logs it, and returns normally — no retry storm.

Exception constructors are taken EXACTLY from resend.exceptions (resend 2.28.1), verified
by instantiating them against the installed SDK:

    ResendError(code, error_type, message, suggested_action, headers=None)  # all 4 required
    ValidationError(message, error_type, code, headers=None)                # NO suggested_action

Passing suggested_action to ValidationError raises TypeError, so we use keyword args and
omit it for ValidationError.
"""

import app.celery_tasks as tasks
from app.extensions import db
from app.models import User
from resend.exceptions import ResendError, ValidationError


def _make_user(verified=False, email="retry@example.com"):
    """Create a persisted unverified user so the verification task proceeds to the send."""
    user = User()
    user.email = email
    user.password = "x"
    user.is_verified = verified

    db.session.add(user)
    db.session.commit()

    return user


def _raise_on_send(app, monkeypatch, exc):
    """Make _send_via_resend always raise `exc`; return the list that counts its calls."""
    calls = []

    def boom(params):
        calls.append(1)
        raise exc

    monkeypatch.setattr(tasks, "_send_via_resend", boom)
    monkeypatch.setitem(app.config, "MAIL_SUPPRESS_SEND", False)

    return calls


def test_transient_resend_error_retries_then_fails_loudly(app, monkeypatch):
    # Base ResendError is exactly what resend.Emails.send raises for a wrapped
    # network/transport failure (code=500, error_type "HttpClientError").
    transient = ResendError(
        code=500,
        error_type="HttpClientError",
        message="simulated network failure",
        suggested_action="",
    )
    calls = _raise_on_send(app, monkeypatch, transient)
    user = _make_user()

    # throw=False: the task ends by raising after retries; assert on the result instead of
    # letting the exception propagate out of .apply().
    result = tasks.send_verification_email.apply(args=[user.id, "key"], throw=False)

    assert len(calls) == 4   # 1 initial attempt + max_retries (3)
    assert result.failed()   # exhausted retries -> FAILURE, not silently lost


def test_permanent_validation_error_does_not_retry(app, monkeypatch):
    # ValidationError takes NO suggested_action (hardcoded inside the SDK); passing one
    # would raise TypeError — see module docstring.
    permanent = ValidationError(
        code=422,
        error_type="validation_error",
        message="bad recipient",
    )
    calls = _raise_on_send(app, monkeypatch, permanent)
    user = _make_user()

    result = tasks.send_verification_email.apply(args=[user.id, "key"], throw=False)

    assert len(calls) == 1       # caught in-body, no retry
    assert result.successful()   # returns normally; no retry storm
