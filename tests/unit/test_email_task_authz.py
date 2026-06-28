"""
Stage 2 regression tests for email task authorization.

Email workers must re-fetch the user by id and re-check whether sending is still
allowed before generating tokens, building links, or calling the email provider.
"""

import re

import app.celery_tasks as tasks
from app.extensions import db
from app.models import User
from app.services.auth_service import _hash_token


def _make_user(verified=False, email="h@example.com"):
    """Create a persisted user for email task tests."""
    user = User()
    user.email = email
    user.password = "x"
    user.is_verified = verified

    db.session.add(user)
    db.session.commit()

    return user


def _capture_resend_sends(app, monkeypatch):
    """Capture outbound Resend payloads instead of sending email."""
    sent = []

    monkeypatch.setattr(tasks, "_send_via_resend", lambda params: sent.append(params))
    monkeypatch.setitem(app.config, "MAIL_SUPPRESS_SEND", False)

    return sent


def _run_task(task, user_id, idempotency_key="key"):
    """Run a Celery task synchronously and fail loudly on exceptions."""
    return task.apply(args=[user_id, idempotency_key], throw=True)


def _extract_token_from_html_link(html, link_prefix):
    """Extract the raw token from an email HTML link."""
    match = re.search(rf"{re.escape(link_prefix)}([^\"'<\s]+)", html)
    assert match, f"Expected email HTML to contain link starting with {link_prefix}"
    return match.group(1)


def test_verification_skips_missing_user(app, monkeypatch):
    sent = _capture_resend_sends(app, monkeypatch)

    _run_task(tasks.send_verification_email, 999_999)

    assert sent == []


def test_verification_skips_already_verified(app, monkeypatch):
    sent = _capture_resend_sends(app, monkeypatch)
    user = _make_user(verified=True)

    _run_task(tasks.send_verification_email, user.id)

    assert sent == []


def test_verification_happy_path_builds_link_sends_and_persists_matching_token(app, monkeypatch):
    sent = _capture_resend_sends(app, monkeypatch)
    user = _make_user(verified=False)

    _run_task(tasks.send_verification_email, user.id)

    assert len(sent) == 1

    params = sent[0]
    assert params["to"] == ["h@example.com"]

    link_prefix = "http://localhost/auth/verify/"
    raw_token = _extract_token_from_html_link(params["html"], link_prefix)

    db.session.refresh(user)
    assert user.verification_token == _hash_token(raw_token)


def test_password_reset_skips_missing_user(app, monkeypatch):
    sent = _capture_resend_sends(app, monkeypatch)

    _run_task(tasks.send_password_reset_email, 999_999)

    assert sent == []


def test_password_reset_happy_path_builds_link_sends_and_persists_matching_token(app, monkeypatch):
    sent = _capture_resend_sends(app, monkeypatch)
    user = _make_user(verified=True)  # reset works for existing verified users

    _run_task(tasks.send_password_reset_email, user.id)

    assert len(sent) == 1

    params = sent[0]
    assert params["to"] == ["h@example.com"]

    link_prefix = "http://localhost/auth/reset-password/"
    raw_token = _extract_token_from_html_link(params["html"], link_prefix)

    db.session.refresh(user)
    assert user.password_reset_token == _hash_token(raw_token)
