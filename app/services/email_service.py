"""
Email service using Resend API.

Handles all outgoing emails for Interview Intel including
email verification, password reset, and notification emails.
All sends are fire-and-forget via threading to keep
request/response cycle fast for the user.
"""

import resend
import logging
from threading import Thread
from flask import current_app
from typing import Optional

logger = logging.getLogger(__name__)

def _send_email_background(api_key: str, params: dict) -> None:
    """Send email in a background thread."""
    try:
        resend.api_key = api_key
        resend.Emails.send(params)
    except Exception:
        logger.exception("Failed to send email to %s", params.get("to"))

def queue_email(
        to: str,
        subject: str,
        html: str,
        plain: Optional[str]=None
) -> bool:
    """
    Queue an email send in a background thread.

    Returns True if thread started successfully.
    Returns False if setup failed.
    """
    try:
        # Basic validations
        if not to:
            raise ValueError("Recipient email address is missing")

        if not subject:
            raise ValueError("Email subject is missing")

        if not html:
            raise ValueError("Email HTML content is missing")

        app = current_app._get_current_object()

        api_key = app.config["RESEND_API_KEY"]
        sender  = app.config["MAIL_DEFAULT_SENDER"]

        # Config validations(extra safety)
        if not api_key or not sender:
            raise ValueError("Email config is missing")

        params = {
            "from": sender,
            "to": [to],
            "subject": subject,
            "html": html
        }

        if plain: params["text"] = plain

        thread = Thread(
            target=_send_email_background,
            args=(api_key, params),
            daemon=True
        )
        thread.start()

        return True

    except Exception:
        logger.exception("Failed to start background email send")
        return False
