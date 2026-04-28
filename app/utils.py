from functools import wraps
from urllib.parse import urlsplit

from flask import redirect, url_for, flash, request
from flask_login import current_user


def verified_required(f):
    """
    Decorator that requires the current user to have
    a verified email address before accessing a route.

    Apply after @login_required so you know current_user
    is authenticated before checking is_verified.

    flow:
        @login_required
        @verified_required
        def example_route():
            ...
    """
    @wraps(f)
    def decorated_func(*args, **kwargs):
        if not current_user.is_verified:
            flash(
                "Please verify your email address to access this page.",
                'warning'
            )
            return redirect(url_for("auth.unverified"))
        return f(*args, **kwargs)
    return decorated_func


def normalize_input(x):
    """
    Normalize user input to use in forms/ files.

    - If input is None, return None
    - If string, strip whitespace
    - Otherwise, return unchanged
    """
    if x is None:
        return None
    if isinstance(x, str):
        return x.strip()
    return x


def normalize_email(x):
    """
    Normalize email input to use in forms/ files.

    - If email is None, return None
    - If string, strip whitespace and lowercase
    - Otherwise, return unchanged
    """
    if x is None:
        return None
    if isinstance(x, str):
        return x.strip().lower()
    return x


def mask_email(email):
    """Return a partially obscured email: j***e@domain.com"""
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    masked = local[0] + "***" + (local[-1] if len(local) > 2 else "")
    return f"{masked}@{domain}"


def safe_redirect(default_endpoint: str):
    """
    Redirect user to the page they originally wanted after login.
    Only allows safe local redirects via 'next'.
    If 'next' is missing or unsafe, redirects to default_endpoint.
    Block external URLs and javascript: schemes.
    """
    next_page = request.args.get("next")
    if not next_page:
        return redirect(url_for(default_endpoint))

    parsed_next = urlsplit(next_page)
    if parsed_next.netloc or parsed_next.scheme:
        return redirect(url_for(default_endpoint))

    return redirect(next_page)
