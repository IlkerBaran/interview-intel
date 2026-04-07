import os
import logging

from flask import Flask
from .extensions import db, migrate, login_manager
from .routes import register_blueprints
from .models import User, Message, Task, AnalysisResult
from .services.ml_service import ml_service
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
        }

    # ≈≈≈≈ load Ml models once at startup ≈≈≈≈
    with app.app_context():
        try:
            ml_service.load()
        except FileNotFoundError:
            logger.warning("ML models not found — run: python ml/train.py")

        # unknown error
        except Exception:
            logger.exception("ML models failed to load")

    # register the blueprints
    register_blueprints(app)

    return app