import os
import logging

from flask import Flask
from flask_login import current_user
from sqlalchemy import func

from .extensions import db, migrate, login_manager, csrf
from .routes import register_blueprints
from .models import User, Message, Task, AnalysisResult, Notification
from .services.ml_service import ml_service
from .services.llm_service import llm_service
from config import config_by_name

logger = logging.getLogger(__name__)

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


    # authentication config
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

    # Provides unread_count globally to templates for displaying the notification badge
    @app.context_processor
    def inject_unread_notification_count():
        if not current_user.is_authenticated or not current_user.is_verified:
            return {"unread_count": 0}

        count = db.session.scalar(
            db.select(func.count(Notification.id))
            .where(Notification.user_id == current_user.id)
            .where(Notification.is_read.is_(False))
        )
        return {"unread_count": count or 0}


    # ≈≈≈≈ load Ml models once at startup ≈≈≈≈
    with app.app_context():
        try:
            ml_service.load()
        except FileNotFoundError:
            logger.warning("ML models not found — run: python ml/train.py")

        # unknown ml load error
        except Exception:
            logger.exception("ML models failed to load")

        # ≈≈≈≈ load LLM service once at startup ≈≈≈≈
        try:
            llm_service.load()
        except ValueError as e:
            logger.warning("LLM service not initialized: %s", e)

        # unknown llm load error
        except Exception:
            logger.exception("LLM service failed to load")


    # register the blueprints
    register_blueprints(app)

    return app