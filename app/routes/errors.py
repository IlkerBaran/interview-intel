from flask import Blueprint, render_template
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


@errors_bp.app_errorhandler(CSRFError)
def handle_csrf_error(error):
    return render_template("errors/csrf_error.html"), 400