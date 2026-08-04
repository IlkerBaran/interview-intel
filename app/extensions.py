"""
extensions.py

Central place where all Flask extensions are created.
They are initialized later inside create_app()
"""
import hashlib
import hmac

from flask import current_app, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFProtect

from app.utils import normalize_email

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()


# ≈≈≈≈ Rate limiting ≈≈≈≈
# Default limits are keyed by client IP. Routes that need a different scope
# explicitly provide a key function, such as user_key() for authenticated
# accounts or pending_email_key() for a verification-email destination.

# Authentication routes use IP-based limits so the limiter key does not depend
# on whether the submitted email belongs to an account. This also restricts one
# client from attempting many different email addresses to evade the limit.

# Any destination-based limit must be applied uniformly to both existing and
# nonexistent accounts; applying it only after confirming that an account
# exists could reveal account existence through different rate-limit behavior.
limiter = Limiter(key_func=get_remote_address) # set default rate-limit per user IP address


def user_key() -> str:
    """
    Use the authenticated user's Flask-Login ID when available; otherwise,
    fall back to the client IP.
    """
    user_id = current_user.get_id() if current_user.is_authenticated else None
    if user_id is not None:
        return f"user:{user_id}"
    return f"ip:{get_remote_address()}"


def pending_email_key() -> str:
    """
    Key resend-verification limits by the destination stored in the session.
    A keyed digest prevents the raw email address from appearing in Redis.
    Fall back to the client IP if the pending address is unavailable.
    """
    email = normalize_email(session.get("pending_verification_email")) or ""
    if not email:
        return f"ip:{get_remote_address()}"
    secret = current_app.config["RATELIMIT_KEY_SECRET"].encode("utf-8")
    digest = hmac.new(
        secret,
        email.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()[:32]
    return f"pending:{digest}"
