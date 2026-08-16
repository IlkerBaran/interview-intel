import os
import logging
from datetime import datetime, UTC

from flask import Flask, has_request_context
from flask_login import current_user
from sqlalchemy import func
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import db, migrate, login_manager, csrf, limiter
from .routes import register_blueprints
from .models import User, Message, Task, AnalysisResult, Notification, AgentRun
from .services.ml_service import ml_service
from .services.llm_service import llm_service
from .celery_app import celery_init_app
from config import config_by_name

logger = logging.getLogger(__name__)


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

    # ≈≈≈≈ Reverse proxy — must run BEFORE the limiter sees any request ≈≈≈≈
    # Rate limits key on the client IP, so the app has to observe the REAL client
    # address. Behind a proxy, request.remote_addr is the proxy's own IP and every
    # user would share a single bucket. ProxyFix rewrites remote_addr (and the
    # scheme) from the X-Forwarded-* headers.
    #
    # Left off entirely when TRUSTED_PROXY_HOPS is 0 (the default) — trusting a
    # forwarded header with no proxy in front would let any client spoof its IP.
    # See the TRUSTED_PROXY_HOPS note in config.py for how to pick the value.
    proxy_hops = app.config.get("TRUSTED_PROXY_HOPS", 0)
    if proxy_hops > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops)
        logger.info("ProxyFix enabled for %s forwarded hop(s)", proxy_hops)

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

    # TODO: replace with Flask-Talisman for production security headers
    # Requires: pip install flask-talisman
    # Requires: move all inline style="" attributes to CSS classes
    # Requires: add nonce="{{ csp_nonce }}" to all inline <script> and <style> blocks
    # See: https://github.com/GoogleCloudPlatform/flask-talisman
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        return response


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


    @app.context_processor
    def inject_utc_now():
        """
        Expose the current UTC instant so templates can mark a task overdue.

        Deliberately trivial and unguarded, unlike the two processors below: it
        touches no database and no request context, so it cannot fail the render
        or break email templates rendered inside a Celery worker.

        Timezone-AWARE, because Task.due_date is stored aware (noon UTC, see
        workflow_service.DUE_DATE_HOUR_UTC). Comparing it against a naive value
        would raise inside the template.
        """
        return {"utc_now": datetime.now(UTC)}


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

    return app
