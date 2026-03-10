from flask import Flask
from .extensions import db, migrate, login_manager
from .routes import blueprints
from .models import db
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

    # register the blueprints list
    for blueprint in blueprints:
        app.register_blueprint(blueprint)

    return app