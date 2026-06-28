"""
Stage 2: Regression tests for the email queue boundary.

The queue_* helpers must enqueue only simple identifiers:
    (user_id, idempotency_key)

They must not pass tokens, rendered HTML, URLs, ORM objects, or provider secrets
through Celery. Email content and sensitive values should be created or loaded
inside the worker. Covers ADR-0003 and ADR-0006.
"""

import json

from app.services.email_service import (
    queue_password_reset_email,
    queue_verification_email,
)


def _capture_delay(monkeypatch, task_attr):
    """Replace a Celery task's delay method and capture the queued payload."""
    import app.celery_tasks as tasks

    captured = {}

    def fake_delay(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(getattr(tasks, task_attr), "delay", fake_delay)
    return captured


def _assert_minimal_email_enqueue_payload(captured, app, user_id, forbidden_markers):
    """Assert Celery received only safe email queue identifiers."""
    assert "args" in captured, "Expected task.delay(...) to be called"

    args = captured["args"]

    assert len(args) == 2
    assert args[0] == user_id
    assert isinstance(args[1], str) and len(args[1]) == 32  # uuid4().hex correlation id
    assert not captured["kwargs"]

    serialized_args = json.dumps(list(args))  # JSON-serializable, no ORM objects
    lowered_args = serialized_args.lower()

    assert "<html" not in lowered_args
    assert "http" not in lowered_args

    for marker in forbidden_markers:
        assert marker not in lowered_args

    api_key = app.config.get("RESEND_API_KEY")
    if api_key:
        assert api_key not in serialized_args


def test_verification_enqueue_args_are_minimal_and_jsonsafe(app, unverified_user, monkeypatch):
    captured = _capture_delay(monkeypatch, "send_verification_email")

    assert queue_verification_email(unverified_user.id) is True

    _assert_minimal_email_enqueue_payload(
        captured=captured,
        app=app,
        user_id=unverified_user.id,
        forbidden_markers=[
            "verification_token"
        ]
    )


def test_password_reset_enqueue_args_are_minimal_and_jsonsafe(app, unverified_user, monkeypatch):
    captured = _capture_delay(monkeypatch, "send_password_reset_email")

    assert queue_password_reset_email(unverified_user.id) is True

    _assert_minimal_email_enqueue_payload(
        captured=captured,
        app=app,
        user_id=unverified_user.id,
        forbidden_markers=[
            "reset_token",
            "password_reset_token"
        ]
    )
