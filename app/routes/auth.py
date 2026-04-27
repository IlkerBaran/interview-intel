from werkzeug.security import generate_password_hash, check_password_hash
import logging

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user, login_required

from app.extensions import db
from app.forms.auth_forms import RegisterForm, LoginForm, ForgotPasswordForm, ResetPasswordForm
from app.models import User
from app.services.auth_service import(
    verify_email_token,
    generate_verification_token,
    generate_password_reset_token,
    verify_password_reset_token
)
from app.services.email_service import queue_email
from app.utils import safe_redirect


logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# Module-level dummy password hash to normalize login timing
# Prevents timing-based account enumeration when user is not found
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-timing-attack-mitigation")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """
    Handle user registration, prevent duplicate accounts,
    and send an email verification link.
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = RegisterForm()

    if form.validate_on_submit():

        # check if user email already registered
        existing_user = db.session.execute(
            db.select(User).where(
                User.email == form.email.data
            )
        ).scalar_one_or_none()

        if existing_user:
            if not existing_user.is_verified:
                token = generate_verification_token(existing_user)

                try:
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    logger.error(
                        "Failed to regenerate token for unverified user_id=%s error_type=%s",
                        existing_user.id,
                        type(e).__name__,
                        extra={
                            "user_id": existing_user.id,
                            "error_type": type(e).__name__
                        }
                    )
                    flash("Something went wrong. Please try again.", "danger")
                    return redirect(url_for("auth.register"))

                confirmation_url = url_for(
                    "auth.verify_email",
                    token=token,
                    _external=True
                )

                queue_email(
                    to=existing_user.email,
                    subject="Confirm your Interview Intel account",
                    html=render_template(
                        "email/confirmation.html",
                        confirmation_url=confirmation_url
                    ),
                    plain=f"Confirm your email by visiting: {confirmation_url}"
                )

            # same message regardless of verified status
            # prevent account enumeration attack
            flash(
                "If that email is available you will receive a confirmation link shortly.",
                "info"
                )

            return redirect(url_for("auth.login"))

        # new user
        user = User()
        user.email = form.email.data
        user.password = form.password.data

        token = generate_verification_token(user)

        try:
            db.session.add(user)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(
                "Failed to commit new user registration error_type=%s",
                type(e).__name__,
                extra={
                    "error_type": type(e).__name__
                }
            )
            flash("Something went wrong. Please try again.", "danger")
            return redirect(url_for("auth.register"))

        confirmation_url = url_for(
            "auth.verify_email",
            token=token,
            _external=True
        )

        queue_email(
            to=user.email,
            subject="Confirm your Interview Intel account",
            html=render_template(
                "email/confirmation.html",
                confirmation_url=confirmation_url
            ),
            plain=f"Confirm your email by visiting: {confirmation_url}"
        )

        flash(
            "If that email is available you will receive a confirmation link shortly.",
            "info"
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/delete-account", methods=["POST"])
@login_required
def delete_account():
    """
    Delete the current user's account, remove all associated data,
    and log the user out safely.
    """
    user = current_user._get_current_object()

    try:
        db.session.delete(user)
        db.session.commit()
        logout_user()                 # logout after successful delete

        flash("Your account and all data have been deleted.", "info")
        return redirect(url_for("main.home"))

    except Exception as e:
        db.session.rollback()
        logger.error(
            "Error while deleting account for user_id=%s error_type=%s",
            user.id,
            type(e).__name__,
            extra={
                "user_id": user.id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("dashboard.index"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """
    Authenticate user credentials, protect against timing attacks,
    and redirect safely after login.
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()

    if form.validate_on_submit():

        user = db.session.execute(
            db.select(User).where(
                User.email == form.email.data
            )
        ).scalar_one_or_none()


        # Pre-computed hash used to normalize login response time when the email is not found.
        # Prevents timing-based account enumeration attack (bcrypt is intentionally slow).
        if user:
            password_ok = user.verify_password(form.password.data)
        else:
            check_password_hash(_DUMMY_PASSWORD_HASH, form.password.data)
            password_ok = False

        if user and password_ok:
            login_user(user, form.remember_me.data)
            flash("You have been logged in successfully.", "success")

            # safe_redirect() blocks external URLs and javascript: schemes — see utils.py
            return safe_redirect("dashboard.index")

        flash("Invalid email or password.", "danger")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """
    Log out the current user and clear their session.
    """
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route('/verify/<token>')
def verify_email(token):
    """
    Handle email verification link clicks.

    Verifies the token, marks user as verified on success,
    and redirects with appropriate flash message to "auth.login".
    """
    success, message = verify_email_token(token)

    if success:
        try:
            db.session.commit()
            flash(message, "success")
            logger.info("Email verification committed successfully")
        except Exception as e:
            db.session.rollback()
            logger.error(
                "Failed to commit email verification error_type=%s",
                type(e).__name__,
                extra={
                    "error_type": type(e).__name__
                }
            )
            flash("Verification failed. Please try again.", "danger")
            return redirect(url_for("auth.login"))
    else:
        flash(message, "danger")

    return redirect(url_for("auth.login"))


@auth_bp.route("/unverified")
@login_required
def unverified_email():
    """
    Landing page for users who have not yet verified their email.

    Shows verification status and resend option.
    Already verified users are redirected to dashboard.
    """
    if current_user.is_verified:
        return redirect(url_for('dashboard.index'))
    return render_template('auth/unverified.html')


@auth_bp.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    """
    Resend the email verification link to the current user.

    Only accessible to logged-in unverified users.
    Verified users are redirected to dashboard silently.
    """
    if current_user.is_verified:
        return redirect(url_for("dashboard.index"))

    token = generate_verification_token(current_user)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to regenerate verification token for user_id=%s error_type=%s",
            current_user.id,
            type(e).__name__,
            extra={
                "user_id": current_user.id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("auth.unverified_email"))

    confirmation_url = url_for(
        "auth.verify_email",
        token=token,
        _external=True
    )

    queue_email(
        to=current_user.email,
        subject="Confirm your Interview Intel account",
        html=render_template(
            "email/confirmation.html",
            confirmation_url=confirmation_url
        ),
        plain=f"Confirm your email by visiting: {confirmation_url}"
    )

    flash("Confirmation email sent. Please check your inbox.", "info")
    return redirect(url_for("auth.unverified_email"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """
    Handle password reset requests.

    GET  → show the forgot password form
    POST → generate reset token and send email
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = ForgotPasswordForm()

    if form.validate_on_submit():
        user = db.session.execute(
            db.select(User).where(
                User.email == form.email.data
            )
        ).scalar_one_or_none()

        # Always flash the same message even if email is not found
        # Prevents user enumeration attacks
        if user:
            token = generate_password_reset_token(user)

            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.error(
                    "Failed to save password reset token for user_id=%s error_type=%s",
                    user.id,
                    type(e).__name__,
                    extra={
                        "user_id": user.id,
                        "error_type": type(e).__name__
                    }
                )
                flash("Something went wrong. Please try again.", "danger")
                return redirect(url_for("auth.forgot_password"))

            reset_url = url_for(
                "auth.reset_password",
                token=token,
                _external=True
            )

            queue_email(
                to=user.email,
                subject="Reset your Interview Intel password",
                html=render_template(
                    "email/password_reset.html",
                    reset_url=reset_url
                ),
                plain=f"Reset your password by visiting: {reset_url}"
            )

        flash(
            "If that email is registered you will receive a reset link shortly.",
            "info"
        )

        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)

@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """
    Handle password reset form submission.

    GET  → show the new password form if token is valid
    POST → validate new password, save, clear token, redirect to login
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    success, message, user = verify_password_reset_token(token)

    if not success:
        flash(message, "danger")
        return redirect(url_for("auth.forgot_password"))

    form = ResetPasswordForm()

    if form.validate_on_submit():
        user.password = form.password.data
        user.password_reset_token = None
        user.password_reset_expires_at = None

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(
                "Failed to save new password for user_id=%s error_type=%s",
                user.id,
                type(e).__name__,
                extra={
                    "user_id": user.id,
                    "error_type": type(e).__name__
                }
            )
            flash("Something went wrong. Please try again.", "danger")
            return redirect(url_for("auth.reset_password", token=token))

        logger.info(
            "Password reset completed for user_id=%s",
            user.id,
            extra={
                "user_id": user.id
            }
        )
        flash("Password updated successfully. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form, token=token)
