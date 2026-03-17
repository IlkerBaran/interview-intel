from flask import Flask
from .extensions import db, migrate, login_manager
from .routes import register_blueprints
from .models import User
from config import Config


def create_app(config_class=Config):
    """
    Application factory function.

    Creates and configures the Flask application instance,
    loads configuration settings, initializes Flask extensions,
    and returns the fully configured app.
    """
    app = Flask(__name__) # app object
    app.config.from_object(config_class)

    db.init_app(app)             # db connection to app
    migrate.init_app(app, db)    # migrate connection to app and db
    login_manager.init_app(app)  # login_manager connection to app

    # authentication config
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id):
        if user_id is None:
            return None
        return db.session.get(User, int(user_id))

    # register the blueprints list
    register_blueprints(app)

    return app