"""
Email service for transactional emails sent through Resend API.

Stage 2 moves email delivery out of daemon threads and into Celery tasks.
Auth routes call the thin enqueue helpers in this module with only a user ID.
The Celery tasks in app/celery_tasks.py re-fetch the user, generate tokens,
render templates, and send the email.

This module is the only place that calls Resend through _send_via_resend()
and the only place that reads RESEND_API_KEY. The API key, rendered email
content, and secret tokens are never passed through Celery task arguments
or stored in the broker.

See ADR-0006 and ADR-0007.
"""


import logging
from uuid import uuid4

import resend
from flask import current_app


logger = logging.getLogger(__name__)


def _send_via_resend(params: dict) -> None:
    """
    Send an email through Resend using Flask config.

    Reads `RESEND_API_KEY` and `MAIL_DEFAULT_SENDER` at call time from the active
    Flask app context, so secrets are not passed through the broker.

    Do not pass a Resend `idempotency_key`: retries generate a fresh token, and
    deduplication could block the email containing the valid token. See ADR-0007.

    `params` contains `to`, `subject`, `html`, and `text`.
    """
    app = current_app._get_current_object()
    api_key = app.config["RESEND_API_KEY"]
    sender = app.config["MAIL_DEFAULT_SENDER"]
    if not api_key or not sender:
        raise ValueError("Email config is missing (RESEND_API_KEY / MAIL_DEFAULT_SENDER)")

    resend.api_key = api_key
    resend.Emails.send({**params, "from": sender})


def queue_verification_email(user_id: int) -> bool:
    """
    Enqueue an email-verification send for the given user id.
    Returns True if enqueued, False if enqueueing failed.
    """
    try:
        # Function-local import avoids an email_service <-> celery_tasks import cycle.
        from app.celery_tasks import send_verification_email

        send_verification_email.delay(user_id, uuid4().hex)
        return True
    except Exception as e:
        logger.error(
            "Failed to enqueue verification email user_id=%s error_type=%s",
            user_id, type(e).__name__,
            extra={
                "user_id": user_id,
                "error_type": type(e).__name__
            }
        )
        return False


def queue_password_reset_email(user_id: int) -> bool:
    """
    Enqueue a password-reset send for the given user id.
    Returns True if enqueued, False if enqueueing failed.
    """
    try:
        from app.celery_tasks import send_password_reset_email

        send_password_reset_email.delay(user_id, uuid4().hex)
        return True
    except Exception as e:
        logger.error(
            "Failed to enqueue password reset email user_id=%s error_type=%s",
            user_id, type(e).__name__,
            extra={
                "user_id": user_id,
                "error_type": type(e).__name__
            }
        )
        return False


def queue_password_changed_email(user_id: int) -> bool:
    """
    Enqueue a 'password changed' security notification for the given user id.
    Returns True if enqueued, False if enqueueing failed.
    """
    try:
        from app.celery_tasks import send_password_changed_email

        send_password_changed_email.delay(user_id, uuid4().hex)
        return True
    except Exception as e:
        logger.error(
            "Failed to enqueue password changed email user_id=%s error_type=%s",
            user_id, type(e).__name__,
            extra={
                "user_id": user_id,
                "error_type": type(e).__name__
            }
        )
        return False


