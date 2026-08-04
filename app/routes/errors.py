from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_wtf.csrf import CSRFError

from app.extensions import db

errors_bp = Blueprint("errors", __name__)


@errors_bp.app_errorhandler(404)
def not_found_error(error):
    return render_template("errors/404.html"), 404


@errors_bp.app_errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template("errors/500.html"), 500


@errors_bp.app_errorhandler(429)
def too_many_requests(error):
    """
    Rate limit exceeded.

    Flask-Limiter injects Retry-After and the X-RateLimit-* headers onto the response
    itself (RATELIMIT_HEADERS_ENABLED), so they survive this handler and must not be
    re-created here. Note RateLimitExceeded.retry_after is None — the value lives in
    the header, not the attribute.

    Content negotiated: the notification AJAX calls and the analysis status poller
    expect JSON, a browser navigation expects the styled page. text/html is listed
    FIRST so a */* client (fetch() with no Accept header, curl) gets HTML, while an
    explicit "Accept: application/json" still selects JSON.
    """
    # error.description is the limit that tripped, e.g. "3 per 1 hour".
    limit = getattr(error, "description", None)

    wants_json = request.accept_mimetypes.best_match(
        ["text/html", "application/json"]
    ) == "application/json"

    if wants_json:
        return jsonify(
            ok=False,
            error="rate_limit_exceeded",
            message="Too many requests. Please wait a moment and try again.",
            limit=str(limit) if limit else None,
        ), 429

    return render_template("errors/429.html", limit=limit), 429


@errors_bp.app_errorhandler(CSRFError)
def handle_csrf_error(error):
    """
    Handle expired or missing CSRF tokens gracefully.
    Redirects back to referrer with a clear message
    instead of showing a dead-end error page.
    """
    flash("Your session expired — please try again.", "warning")

    referrer = request.referrer
    if referrer:
        return redirect(referrer)
    return redirect(url_for("main.home"))
