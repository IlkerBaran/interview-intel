import logging

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user

from app.forms.message_forms import MessageSubmissionForm, MessageNoteForm
from app.extensions import db
from app.models import Message, MessageStatus
from app.services.workflow_service import process_message_submission
from app.utils import verified_required

logger = logging.getLogger(__name__)

messages_bp = Blueprint("messages", __name__, url_prefix="/messages")


def get_user_message_or_404(message_id):
    """
    Authorization Security check!

    return a message only if it belongs to the current logged-in user.
    Ownership is enforced via user_id in the WHERE clause.
    Message -> User
    """
    message = db.session.execute(
            db.select(Message).where(
                Message.id == message_id,
                Message.user_id == current_user.id
            )
    ).scalar_one_or_none()

    if message is None:
        abort(404)
    return message


@messages_bp.route("/", methods=["GET"])
@login_required
@verified_required
def index():
    """
    Display all the active messages
    """
    messages = db.session.execute(
        db.select(Message).where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED
        )
        .order_by(Message.created_at.desc())
    ).scalars().all()

    return render_template("messages/index.html", messages=messages)


@messages_bp.route("/new", methods=["GET", "POST"])
@login_required
@verified_required
def new_message():
    """
    Display a message submission form and process a new message submission for the logged-in user.
    """
    form = MessageSubmissionForm()

    if form.validate_on_submit():
        try:
            message = process_message_submission(
                user_id=current_user.id,
                raw_text=form.raw_text.data,
                subject=form.subject.data or None,
                sender_email=form.sender_email.data or None
            )

            flash("Message analyzed successfully", "success")
            return redirect(url_for("messages.show_message", message_id=message.id))

        except Exception as e:
            logger.error(
                "Error while processing message submission error_type=%s",
                type(e).__name__,
                extra={
                    "error_type": type(e).__name__
                }
            )
            flash("Something went wrong while processing the message", "danger")

    return render_template("messages/new_message.html", form=form)


@messages_bp.route("/<int:message_id>", methods=["GET"])
@login_required
@verified_required
def show_message(message_id):
    """
    Display a single message along with its analysis results,
    tasks, and agent run history if it belongs to logged-in user.
    """
    message = get_user_message_or_404(message_id)

    return render_template("messages/show_message.html", message=message)


@messages_bp.route("/<int:message_id>/delete", methods=["POST"])
@login_required
@verified_required
def delete_message(message_id):
    """
    Delete a message and its related records if it belongs to logged-in user.
    Cascade behavior is handled by the model relationships.
    """
    message = get_user_message_or_404(message_id)

    try:
        db.session.delete(message)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to delete message_id=%s error_type=%s",
            message_id,
            type(e).__name__,
            extra={
                "message_id": message_id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("messages.show_message", message_id=message_id))

    flash("Message deleted successfully", "success")
    return redirect(url_for("dashboard.index"))


@messages_bp.route("/<int:message_id>/note", methods=["GET", "POST"])
@login_required
@verified_required
def edit_note(message_id):
    """
    Edits a note simply one note per message if it belongs to logged-in user.
    """
    message = get_user_message_or_404(message_id)
    form = MessageNoteForm()

    if form.validate_on_submit():
        message.note = form.note.data
        db.session.commit()

        flash("Note saved successfully.", "success")
        return redirect(url_for("messages.show_message", message_id=message.id))

    if request.method == "GET":
        form.note.data = message.note

    return render_template("messages/edit_note.html", form=form, message=message)


@messages_bp.route("/<int:message_id>/note/delete", methods=["POST"])
@login_required
@verified_required
def delete_note(message_id):
    """
    Deletes a note simply one note per message if it belongs to logged-in user.
    """
    message = get_user_message_or_404(message_id)
    message.note = None

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to delete note message_id=%s error_type=%s",
            message_id,
            type(e).__name__,
            extra={
                "message_id": message_id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("messages.show_message", message_id=message_id))

    flash("Note deleted successfully.", "success")
    return redirect(url_for("messages.show_message", message_id=message.id))


@messages_bp.route("/<int:message_id>/archive", methods=["POST"])
@login_required
@verified_required
def archive_message(message_id):
    """
    Archive a message if it belongs to logged-in user.
    """
    message = get_user_message_or_404(message_id)
    message.status = MessageStatus.ARCHIVED

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to archive message_id=%s error_type=%s",
            message_id,
            type(e).__name__,
            extra={
                "message_id": message_id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("messages.show_message", message_id=message_id))

    flash("Message archived successfully.", "success")
    return redirect(url_for("dashboard.index"))


@messages_bp.route("/<int:message_id>/unarchive", methods=["POST"])
@login_required
@verified_required
def unarchive_message(message_id):
    """
    Restore an archived message back to active status
    if it belongs to the logged-in user.
    """
    message = get_user_message_or_404(message_id)
    message.status = MessageStatus.COMPLETED

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to unarchive message_id=%s error_type=%s",
            message_id,
            type(e).__name__,
            extra={
                "message_id": message_id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("messages.show_archived_messages"))

    flash("Message restored.", "success")
    return redirect(url_for("messages.show_message", message_id=message.id))


@messages_bp.route("/archive", methods=["GET"])
@login_required
@verified_required
def show_archived_messages():
    """
    Display archived messages for the logged-in user.
    """
    messages = db.session.execute(
        db.select(Message).where(
            Message.user_id == current_user.id,
            Message.status == MessageStatus.ARCHIVED
        )
        .order_by(Message.updated_at.desc())
    ).scalars().all()

    return render_template("messages/show_archived_messages.html", messages=messages)
