from werkzeug.security import generate_password_hash, check_password_hash
import json
import logging
from datetime import datetime, UTC

from flask import Blueprint, render_template, redirect, url_for, flash, session, Response
from flask_login import login_user, logout_user, current_user, login_required

from app.extensions import db, limiter, user_key, pending_email_key
from app.forms.auth_forms import RegisterForm, LoginForm, ForgotPasswordForm, ResetPasswordForm, ChangePasswordForm
from app.models import User
from app.services.auth_service import verify_email_token, verify_password_reset_token
from app.services.email_service import (
    queue_verification_email,
    queue_password_reset_email,
    queue_password_changed_email,
)
from app.utils import safe_redirect, mask_email


logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# Module-level dummy password hash to normalize login timing
# Prevents timing-based account enumeration when user is not found
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-timing-attack-mitigation")


@auth_bp.route("/register", methods=["GET", "POST"])
# Creates User rows unbounded and re-queues a verification email for an existing
# unverified address. methods=["POST"] so viewing the form is never limited.
@limiter.limit("5 per hour", methods=["POST"])
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
                # Token generated + email rendered inside the Celery task (pass id,
                # build in the worker - ADR-0006). Nothing to commit here.
                queue_verification_email(existing_user.id)

            # same response for all existing emails — prevent enumeration
            flash(
                "If that email is available you will receive a confirmation link shortly.",
                "info",
            )
            return redirect(url_for("auth.login"))

        # new user
        user = User()
        user.email = form.email.data
        user.password = form.password.data

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

        # User is committed (id assigned) → enqueue by id; the worker generates the
        # token and renders the email (ADR-0006).
        queue_verification_email(user.id)

        flash(
            "If that email is available you will receive a confirmation link shortly.",
            "info"
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/delete-account", methods=["POST"])
@login_required
# Strict per-user limit because this is a destructive, rarely used action.
@limiter.limit("3 per hour", key_func=user_key)
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


@auth_bp.route("/account", methods=["GET"])
@login_required
def account():
    """Account page — change password, account info, data export, delete account."""
    return render_template("auth/account.html", change_password_form=ChangePasswordForm())


@auth_bp.route("/account/password", methods=["POST"])
@login_required
# Verifies the current password, so it is a brute-force target on a hijacked
# session. Below @login_required so anonymous hits redirect without consuming
# anyone's budget.
@limiter.limit("5 per hour", key_func=user_key)
def change_password():
    """
    Change the logged-in user's password.
    The current password must verify against the stored hash before the new one is set.
    """
    form = ChangePasswordForm()

    if form.validate_on_submit():
        user = current_user._get_current_object()

        # Reuse User.verify_password() → werkzeug check_password_hash
        if not user.verify_password(form.current_password.data):
            form.current_password.errors.append("Current password is incorrect.")
            return render_template("auth/account.html", change_password_form=form)

        # Reuse the User.password setter → werkzeug generate_password_hash
        user.password = form.password.data

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(
                "Failed to change password for user_id=%s error_type=%s",
                user.id, type(e).__name__,
                extra={"user_id": user.id, "error_type": type(e).__name__},
            )
            flash("Something went wrong. Please try again.", "danger")
            return render_template("auth/account.html", change_password_form=form)

        logger.info("Password changed for user_id=%s", user.id, extra={"user_id": user.id})
        queue_password_changed_email(user.id)   # background security notification; don't block the response
        flash("Password updated successfully.", "success")
        return redirect(url_for("auth.account"))

    # Validation errors (empty / too short / mismatch) → re-render with the section open
    return render_template("auth/account.html", change_password_form=form)


@auth_bp.route("/account/export", methods=["GET"])
@login_required
# Walks every message + analysis + task and builds the whole JSON in memory with
# no pagination — expensive to repeat.
@limiter.limit("5 per hour", key_func=user_key)
def export_data():
    """
    Export all of the current user's data (messages + analyses + tasks) as JSON.
    Strictly scoped to current_user: data is read only through the user's own
    relationships, so no other user's records can be included.
    """
    user = current_user._get_current_object()

    messages_data = []
    for message in user.messages:                      # user-scoped relationship
        entry = message.to_dict()
        entry["analysis_result"] = (
            message.analysis_result.to_dict() if message.analysis_result else None
        )
        entry["tasks"] = [
            {
                "task_name": t.task_name,
                "description": t.description,
                "priority": t.priority,
                "is_completed": t.is_completed,
                "due_text": t.due_text,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in message.tasks
        ]
        messages_data.append(entry)

    export = {
        "account": {
            "email": user.email,
            "is_verified": user.is_verified,
            "joined": user.created_at.isoformat() if user.created_at else None,
        },
        "exported_at": datetime.now(UTC).isoformat(),
        "messages": messages_data,
    }

    payload = json.dumps(export, indent=2, ensure_ascii=False)
    filename = f"interview-intel-export-{datetime.now(UTC).strftime('%Y%m%d')}.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@auth_bp.route("/login", methods=["GET", "POST"])
# Brute-force protection. Keyed on IP, NOT the submitted email — see the note on
# limiter in extensions.py: an email-keyed limit would turn 429-vs-200 into an
# account-existence oracle and undo the _DUMMY_PASSWORD_HASH timing guard below.
@limiter.limit("10 per minute", methods=["POST"])
@limiter.limit("40 per hour", methods=["POST"])
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
            if not user.is_verified:
                session["pending_verification_email"] = user.email
                session["verification_context"] = "login_attempt"
                return redirect(url_for("auth.unverified"))

            login_user(user, form.remember_me.data)

            # safe_redirect() blocks external URLs and JavaScript: schemes — see utils.py
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
def unverified():
    """
    Landing page for users who have not yet verified their email.

    Shows verification status and resend option.
    Already verified users are redirected to dashboard.
    """
    if current_user.is_authenticated:
        if current_user.is_verified:
            return redirect(url_for('dashboard.index'))
        session["pending_verification_email"] = current_user.email
        logout_user()
    context = session.pop("verification_context", None)
    email = mask_email(session.get("pending_verification_email"))
    return render_template('auth/unverified.html', verification_context=context, email=email)


@auth_bp.route("/resend-verification", methods=["POST"])
# Sends real mail and is NOT @login_required — gated only by a session value, so
# this is the sharpest mail-bomb surface in the app. Two limits: per IP, and per
# target address so rotating IPs still can't flood one inbox.
@limiter.limit("3 per hour")
@limiter.limit("3 per hour", key_func=pending_email_key)
def resend_verification():
    """
    Resend the email verification link to the current user.

    Only accessible to logged-in unverified users.
    Verified users are redirected to dashboard silently.
    """
    if current_user.is_authenticated:
        if current_user.is_verified:
            return redirect(url_for("dashboard.index"))
        logout_user()

    email = session.get("pending_verification_email")
    if not email:
        flash("Session expired. Please log in again.", "warning")
        return redirect(url_for("auth.login"))

    user = db.session.execute(
        db.select(User).where(User.email == email)
    ).scalar_one_or_none()

    if not user or user.is_verified:
        session.pop("pending_verification_email", None)
        return redirect(url_for("auth.login"))

    # Token generated + email rendered inside the Celery task (pass id, build in the
    # worker — ADR-0006).
    queue_verification_email(user.id)

    flash("Confirmation email sent. Please check your inbox.", "info")
    return redirect(url_for("auth.unverified"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
# Sends real mail. IP-keyed for the same enumeration reason as login: this view
# deliberately flashes the same message whether or not the address is registered.
@limiter.limit("3 per hour", methods=["POST"])
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
            # Token generated + email rendered inside the Celery task (pass id, build
            # in the worker — ADR-0006).
            queue_password_reset_email(user.id)

        flash(
            "If that email is registered you will receive a reset link shortly.",
            "info"
        )

        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)

@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
# Limit password-reset submissions to reduce token-guessing and repeated
# password-change attempts. Only POST submissions count; GET displays the form.
@limiter.limit("5 per hour", methods=["POST"])
def reset_password(token):
    """
    Handle password reset form submission.

    GET  → show the new password form if token is valid
    POST → validate new password, save, clear token, redirect to log in
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
