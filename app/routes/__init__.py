from .main import main
from .messages import messages_bp
from .dashboard import dashboard_bp
from .tasks import tasks_bp
from .auth import auth_bp
from .errors import errors_bp
from .applications import applications_bp

def register_blueprints(app):
    """
    Register blueprints

    Gathers and connects to app all the blueprints
    to import in app/__init__.py
    """
    app.register_blueprint(main)
    app.register_blueprint(messages_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(errors_bp)
    app.register_blueprint(applications_bp)
