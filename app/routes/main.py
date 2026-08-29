from flask import Blueprint, render_template

from app.extensions import limiter, talisman


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
# Exempt from the HTTPS redirect: compose.yaml probes
# http://127.0.0.1:8000/healthz inside the network with no X-Forwarded-Proto,
# so Talisman would 302 it to https (force_https_permanent is False), urlopen
# would follow into a TLS handshake against a plain-HTTP socket, and the probe
# would fail the container.
@talisman(force_https=False)
def healthz():
    return {"status": "ok"}, 200
