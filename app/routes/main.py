from flask import Blueprint, render_template

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