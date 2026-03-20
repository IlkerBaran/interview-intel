from urllib.parse import urlsplit

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user, login_required

from app.extensions import db
from app.forms.auth_forms import RegisterForm, LoginForm
from app.models import User


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = RegisterForm()

    if form.validate_on_submit():

        user = User()
        user.email = form.email.data
        user.password = form.password.data

        db.session.add(user)
        db.session.commit()

        flash("Your account has been created successfully. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()

    if form.validate_on_submit():

        user = db.session.execute(
            db.select(User).where(
                User.email == form.email.data
            )
        ).scalar_one_or_none()


        if user and user.verify_password(form.password.data):
            login_user(user, form.remember_me.data)
            flash("You have been logged in successfully.", "success")


            # Redirect user to the page they originally wanted after login.
            # If "next" is missing or unsafe, go to dashboard instead.
            # Prevents open redirect vulnerabilities.
            next_page = request.args.get("next")
            if not next_page or urlsplit(next_page).netloc != "":
                return redirect(url_for("dashboard.index"))

            return redirect(next_page)


        flash("Invalid email or password.", "danger")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


