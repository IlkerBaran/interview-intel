from flask import Blueprint, render_template

from app.extensions import limiter


main = Blueprint('main', __name__)


@main.route("/")
def home():
    return render_template("main/home.html")


@main.route("/about")
def about():
    return render_template("main/about.html")


@main.route("/privacy")
def privacy():
    return render_template("main/privacy.html")


@main.route("/terms")
def terms():
    return render_template("main/terms.html")


# Simple liveness check for the web container. It does not query the database,
# because a temporary database issue should not cause the web process to restart.
#
# Exempt this route from the default rate limit because Docker calls it regularly;
# a 429 response would make a healthy container look unhealthy.
@main.route("/healthz")
@limiter.exempt
def healthz():
    return {"status": "ok"}, 200
