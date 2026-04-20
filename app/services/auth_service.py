"""
Authentication Service System

Handles secure token generation and verification for:
    - Email confirmation
    - password reset

Security model:
    - A raw token is generated and sent to the user by email only
    - Only the SHA-256 hash version of the token is stored in the database
    - If the database is compromised, attackers still do not have the raw tokens
    needed to use verification links directly.
"""

import hashlib
import logging
import secrets

from datetime import UTC, datetime, timedelta
from flask import current_app

from app.extensions import db
from app.models import User


logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    """
    Return the SHA-256 hex digest of a token.
    Always hash tokens before storing or comparing them.

    Flow: token → encode → hash → hex string
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def generate_verification_token(user: User) -> str:
    """
    Generate a secure email verification token for the given user.
    """
    expiry_hours = current_app.config.get("EMAIL_VERIFICATION_TOKEN_EXPIRY_HOURS", 24)
    raw_token = secrets.token_urlsafe(32) # 256 bits


    # saved the hashed vers of the token on the user
    user.verification_token = _hash_token(raw_token)

    # saved expiration timestamp on the user
    user.token_expires_at = datetime.now(UTC) + timedelta(hours=expiry_hours)

    logger.info(
        "Verification token generated for user=%s expires_at=%s",
        user.email, user.token_expires_at
    )

    return raw_token

def verify_email_token(token: str) -> tuple[bool, str]:
    """
    Verify an email confirmation token and mark the user as verified.

    Looks up the user by hashed token, checks expiry, sets
    is_verified=True, and clears the token fields on success.
    """

    if not token:
        logger.warning("verify_email_token called with empty token")
        return False, "Invalid verification link."

    hashed = _hash_token(token)
    user = db.session.execute(
        db.select(User).where(
            User.verification_token == hashed
        )
    ).scalar_one_or_none()

    # Token is invalid or already used
    if not user:
        logger.warning("verify_email_token: no user found for provided token")
        return False, "Invalid or already used verification link."

    # Token is missing expiry or is expired
    if not user.token_expires_at or datetime.now(UTC) > user.token_expires_at:
        logger.warning(
            "verify_email_token: expired or missing expiry for user_id=%s expires_at=%s",
            user.id, user.token_expires_at
        )
        return False, "Verification link has expired. Please request a new one."

    user.is_verified = True
    user.verification_token = None
    user.token_expires_at = None

    logger.info("Email verified successfully for user_id=%s", user.id)
    return True, "Email confirmed successfully. You can now log in."


def generate_password_reset_token(user: User) -> str:
    """
    Generate a secure password reset token for the given user.
    """
    expiry_hours = current_app.config.get("PASSWORD_RESET_TOKEN_EXPIRY_HOURS", 1)
    raw_token = secrets.token_urlsafe(32)  # 256 bits

    # saved the hashed vers of the token on the user
    user.password_reset_token = _hash_token(raw_token)

    # saved expiration timestamp on the user
    user.password_reset_expires_at = datetime.now(UTC) + timedelta(hours=expiry_hours)

    logger.info(
        "Password reset token generated for user=%s expires_at=%s",
        user.email, user.password_reset_expires_at
    )

    return raw_token

def verify_password_reset_token(token: str) -> tuple[bool, str, User | None]:
    """
    Verify a password reset token and return the associated user.

    Looks up the user by hashed token and checks expiry.
    Does NOT clear the token — that happens after the new
    password is successfully saved.
    """

    if not token:
        logger.warning("verify_password_reset_token called with empty token")
        return False, "Invalid password reset link.", None

    hashed = _hash_token(token)
    user = db.session.execute(
        db.select(User).where(
            User.password_reset_token == hashed
        )
    ).scalar_one_or_none()

    if not user:
        logger.warning("verify_password_reset_token: no user found for provided token")
        return False, "Invalid or already used password reset link.", None

    if not user.password_reset_expires_at or datetime.now(UTC) > user.password_reset_expires_at:
        logger.warning(
            "verify_password_reset_token: expired token for user_id=%s expires_at=%s",
            user.id, user.password_reset_expires_at
        )
        return False, "Password reset link has expired. Please request a new one.", None

    return True, "Token is valid.", user

