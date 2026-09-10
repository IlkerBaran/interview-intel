import os
import logging
from datetime import UTC

from flask import Flask, has_request_context
from flask_login import current_user
from flask_talisman import Talisman
from sqlalchemy import func
from werkzeug.middleware.proxy_fix import ProxyFix

from .proxy import ForwardedForLeftmost

from .extensions import db, migrate, login_manager, csrf, limiter
from .routes import register_blueprints
from .cli import register_cli
from .models import User, Message, Task, AnalysisResult, Notification, AgentRun
from .services.ml_service import ml_service
from .services.llm_service import llm_service
from .celery_app import celery_init_app
from config import config_by_name

logger = logging.getLogger(__name__)


# ≈≈≈≈ Content Security Policy ≈≈≈≈
# Derived from what the templates actually reference, not from a default policy.
#
# script-src is 'self' with no 'unsafe-inline' and no nonce: every inline
# <script> lives in app/static/js/ and every former on*= handler is an
# addEventListener there, so there is no inline script left to authorize.
# Keep it that way — one inline handler silently disables a page's JS.
# tests/unit/test_security_headers.py fails the build if one reappears.
#
# style-src keeps 'unsafe-inline' for the 100 static style="" attributes across
# the served templates (a further 41 live in templates/email/, which no CSP
# governs). Nonces cannot help here: a nonce covers <style> ELEMENTS, never
# style="" ATTRIBUTES, and adding one would make browsers ignore
# 'unsafe-inline' and break all of them at once.
#
# Google Fonts needs TWO entries: the stylesheet from fonts.googleapis.com
# (style-src) and the font files it @font-faces to from fonts.gstatic.com
# (font-src). Dropping font-src fails silently as a fallback typeface.
#
# img-src is 'self' with no data:, and GSAP/ScrollTrigger are vendored under
# static/js/vendor/ rather than pulled from a CDN — both verified against the
# templates and CSS, which contain no data: URIs and no url() at all.
CSP = {
    "default-src":     "'self'",
    "script-src":      "'self'",
    "style-src":       ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
    "font-src":        ["'self'", "https://fonts.gstatic.com"],
    "img-src":         "'self'",
    "connect-src":     "'self'",
    "form-action":     "'self'",
    "frame-ancestors": "'self'",
    "base-uri":        "'self'",
    "object-src":      "'none'",
}


def _safe_rollback():
    """
    Clear a failed transaction so the rest of a render can still query.

    Called from the context processors below after a DB fault: SQLAlchemy leaves
    the session in a failed state, and without this every later query in the same
    render fails too. Swallows its own errors — if the connection is gone there is
    nothing left to do, and raising here would defeat the whole point.
    """
    try:
        db.session.rollback()
    except Exception:
        logger.exception("context processor: session rollback failed")


def create_app():
    """
    Application factory function.

    Creates and configures the Flask application instance,
    loads configuration settings, initializes Flask extensions,
    and returns the fully configured app.
    """
    app = Flask(__name__) # app object


    env = os.getenv("FLASK_ENV")
    if env is None:
        raise ValueError("FLASK_ENV must be set")

    if env not in config_by_name:
        raise ValueError(f"Invalid FLASK_ENV value: {env}. Must be one of: {list(config_by_name.keys())}")

    config_class = config_by_name[env]
    app.config.from_object(config_class)


    db.init_app(app)             # db connection to app
    migrate.init_app(app, db)    # migrate connection to app and db
    login_manager.init_app(app)  # login_manager connection to app
    csrf.init_app(app)           # csrf_token() calls

    # ≈≈≈≈ Security headers ≈≈≈≈
    # Talisman replaces the old after_request security-header code.
    # It keeps the same X-Content-Type-Options, X-Frame-Options, and
    # Referrer-Policy behavior, while also adding CSP and HSTS support.
    # # Create a new Talisman instance for each Flask app instead of sharing the
    # module-level instance. Talisman stores app-specific configuration and keeps
    # a reference to the app, so reusing one instance across multiple create_app()
    # calls could cause one app to overwrite another app's Talisman settings.
    #
    # Set every option explicitly, even when it matches Talisman's default.
    # This keeps security behavior predictable if library defaults change later.
    https = app.config.get("TALISMAN_HTTPS", False)
    Talisman(
        app,
        content_security_policy=CSP,
        # No CSP nonce is needed because there are no remaining inline scripts.
        force_https=https,
        # Use a temporary 302 redirect instead of a permanent 301 redirect.
        force_https_permanent=False,
        # Enable HSTS only when HTTPS enforcement is enabled.
        strict_transport_security=https,
        strict_transport_security_max_age=app.config["TALISMAN_HSTS_MAX_AGE"],
        # Do not apply HSTS to all subdomains. The current certificate covers
        # only this host, and other subdomains may not be ready for HTTPS.
        strict_transport_security_include_subdomains=False,
        # Do not enable HSTS preload. Preload is a long-term commitment and
        # requires at least a one-year HSTS max-age.
        strict_transport_security_preload=False,
        # Require HTTPS for session cookies only when HTTPS is enabled.
        session_cookie_secure=https,
        # Prevent JavaScript from reading the session cookie.
        session_cookie_http_only=True,
        # Pinned. Flask leaves SameSite unset, so browsers apply their own Lax
        # default — behavior that is real but undeclared. Stating it here means
        # neither a Talisman default nor a browser default can move it.
        session_cookie_samesite="Lax",
        # Allow this site to be framed only by pages from the same origin.
        frame_options="SAMEORIGIN",
        # Explicitly keep the current referrer policy instead of relying
        # on a library default.
        referrer_policy="strict-origin-when-cross-origin",
        # Prevent browsers from guessing a response's content type.
        x_content_type_options=True,
        # Disable the obsolete X-XSS-Protection browser feature.
        # Modern browsers no longer use it; CSP provides the modern protection.
        x_xss_protection=False,
        # Talisman 1.1.0 ships this default itself (opting out of the Topics
        # API). Restated so the emitted header is ours rather than inherited.
        permissions_policy={"browsing-topics": "()"},
    )
    logger.info(
        "Talisman enabled — CSP on, HTTPS enforcement %s",
        "on" if https else "off (plain-HTTP deploy)",
    )

    # ≈≈≈≈ Reverse proxy — must run BEFORE the limiter sees any request ≈≈≈≈
    # Scheme and client IP are handled separately because Railway's forwarded
    # headers have different trust rules (see config.py and ADR-0012).
    #
    # X-Forwarded-Proto:
    # ProxyFix reads from the right, so one trusted TLS terminator is represented
    # by x_proto=1. This is independent of the X-Forwarded-For chain length.
    # x_for stays 0 because ProxyFix must never rewrite the client IP here.
    #
    # X-Forwarded-For:
    # Railway's chain length can vary, so the client IP is taken from the leftmost
    # value instead of using a hop count. This is safe only when the ingress
    # overwrites client-supplied X-Forwarded-For.
    #
    # With no trusted proxy, neither forwarded header is trusted.
    proto_hops = app.config.get("TRUSTED_PROXY_PROTO_HOPS", 0)
    if proto_hops > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=proto_hops)
        logger.info("ProxyFix enabled — x_proto=%s (scheme only)", proto_hops)

    client_ip_source = app.config.get("CLIENT_IP_SOURCE", "remote-addr")
    if client_ip_source == "xff-leftmost":
        app.wsgi_app = ForwardedForLeftmost(app.wsgi_app)
        logger.info(
            "Client IP from the leftmost X-Forwarded-For value — "
            "valid only while a header-overwriting ingress is in front"
        )
    else:
        logger.info("Client IP from the socket peer — no forwarded header trusted")

    # Initialized AFTER login_manager so the per-user key funcs in extensions.py can
    # resolve current_user.
    limiter.init_app(app)

    # ≈≈≈≈ Celery / Redis wiring ≈≈≈≈
    celery_init_app(app)        # Configure Celery with this Flask app and app context.
    from . import celery_tasks  # noqa: F401  import registers @shared_task  (e.g. ping)

    # ≈≈≈≈ Authentication Config ≈≈≈≈
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "info"


    @login_manager.user_loader
    def load_user(user_id):
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return None
        return db.session.get(User, uid)


    @app.shell_context_processor
    def make_shell_context():
        """
        Provide objects automatically available in the Flask shell for easier debugging and testing.
        """
        return {
            "db": db,
            "User": User,
            "Message": Message,
            "Task": Task,
            "AnalysisResult": AnalysisResult,
            "AgentRun": AgentRun,
            "ml_service": ml_service,
            "llm_service": llm_service
        }

    # ≈≈≈≈ timestamp rendering ≈≈≈≈
    @app.template_filter("utc_iso")
    def utc_iso(dt):
        """
        Render a stored timestamp as an unambiguous UTC ISO-8601 string.

        Every timestamp column is DateTime(timezone=True) and every writer uses
        datetime.now(UTC), but SQLite has no native tz storage — it drops the
        offset on write and hands back a NAIVE datetime on read. The value is
        still UTC; only the label is gone. Re-attach it rather than guessing.

        The trailing 'Z' is load-bearing. Per the ECMAScript spec a date-time
        string with no timezone designator is parsed as LOCAL by new Date(),
        which shifts every value by the viewer's offset. Do not emit
        .isoformat() directly into markup — use this filter.

        Registered on the app (not a blueprint) so macros.html can use it.
        """
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


    @app.template_filter("llm_markup")
    def llm_markup(text):
        """
        Render enrichment prose written in the LLM's light Markdown.

        The model answers with `# headings`, `**bold**` and `- bullets`; a
        pre-wrap block showed those characters literally on the message page and
        the public demo. See app/markup.py for why this is a small escape-first
        renderer rather than a Markdown library.

        Registered on the app (not a blueprint) so the shared macros in
        messages/_analysis.html can use it.
        """
        from .markup import render_llm_text
        return render_llm_text(text)


    @app.context_processor
    def inject_unread_notification_count():
        # Provides unread_count globally to templates for displaying the notification badge but
        # Context processors run on EVERY render_template — including emails rendered
        # inside a Celery worker, where there is no request context and current_user
        # resolves to None (AttributeError on .is_authenticated). Guard first.
        if not has_request_context():
            return {"unread_count": 0}

        # Everything below can touch the database — including the guards, because
        # reading current_user runs the user_loader. A context processor that raises
        # takes down EVERY render, errors/500.html included, turning a handled 500
        # into an unhandled one. Degrade to the anonymous value instead; the badge
        # disappearing is a far better outcome than a dead error page.
        try:
            if not current_user.is_authenticated or not current_user.is_verified:
                return {"unread_count": 0}

            count = db.session.scalar(
                db.select(func.count(Notification.id))
                .where(Notification.user_id == current_user.id)
                .where(Notification.is_read.is_(False))
            )
            return {"unread_count": count or 0}
        except Exception:
            logger.exception("unread notification count unavailable — rendering without it")
            _safe_rollback()
            return {"unread_count": 0}


    @app.context_processor
    def inject_analysis_quota():
        """
        Expose the LIFETIME analysis quota to every template.

        analyses_remaining is derived (allowance - used) and clamped to
        [0, allowance] in quota_service.get_remaining(), so it can never render
        above the allowance even if a refund landed on a pre-quota message, nor
        below zero if the allowance is later lowered. An in-flight analysis has
        already consumed its slot, so the decremented number shows for the whole
        run and only returns if the analysis failed.

        Guarded exactly like the notification processor above: templates also
        render inside Celery workers where there is no request context, and a DB
        fault degrades to None rather than propagating out of the render.

        Returning None (not 0) for anonymous, unverified and degraded cases is what
        lets the templates write `{% if analyses_remaining is not none %}` and show
        nothing at all, rather than claiming the user has 0 analyses left.
        """
        if not has_request_context():
            return {"analyses_remaining": None, "analyses_allowance": None}

        # As above: the guards themselves can hit the database via the user_loader,
        # so they sit inside the try. A raise here would break every page render,
        # including the error templates.
        try:
            if not current_user.is_authenticated or not current_user.is_verified:
                return {"analyses_remaining": None, "analyses_allowance": None}

            from .services.quota_service import get_allowance, get_remaining
            return {
                "analyses_remaining": get_remaining(current_user.id),
                "analyses_allowance": get_allowance()
            }
        except Exception:
            logger.exception("analysis quota unavailable — rendering without it")
            _safe_rollback()
            return {"analyses_remaining": None, "analyses_allowance": None}


    # ≈≈≈≈ load ML + LLM once at startup — unless this process doesn't need them ≈≈≈≈
    # Email-only Celery workers run with LOAD_MODELS=0 so they don't carry the model
    # weights (memory that would otherwise multiply across prefork children × replicas).
    # This is the precondition for Stage 3's fast-email / slow-ML queue split. (ADR-0008)
    if app.config.get("LOAD_MODELS", True):
        with app.app_context():
            # ≈≈≈≈ ML service load ≈≈≈≈
            try:
                ml_service.load()
            except FileNotFoundError:
                # Missing models should fail in production, but development can still start without them.
                if os.getenv("FLASK_ENV") == "production":
                    raise
                logger.warning("ML models not found — run: python ml/train.py")

            # unknown ml load error
            except Exception:
                logger.exception("ML models failed to load")

            # ≈≈≈≈ LLM service load ≈≈≈≈
            try:
                llm_service.load()
            except ValueError as e:
                logger.warning(
                    "LLM service not initialized: %s",
                    type(e).__name__,
                    extra={
                        "error_type": type(e).__name__
                    }
                )

            # unknown llm load error
            except Exception:
                logger.exception("LLM service failed to load")
    else:
        logger.info("LOAD_MODELS is off — skipping ML/LLM load (email/worker role)")


    # register the blueprints
    register_blueprints(app)

    # register the operator CLI commands (flask verify-user)
    register_cli(app)

    return app
