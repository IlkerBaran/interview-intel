from functools import wraps
from flask import redirect, url_for, flash
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