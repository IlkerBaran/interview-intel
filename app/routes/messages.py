import logging

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user

from app.forms.message_forms import MessageSubmissionForm, MessageNoteForm
from app.extensions import db, limiter, user_key
from app.models import Message, MessageStatus
from app.services.workflow_service import (
    queue_message_analysis,
    mark_analysis_failed,
)
from app.services.quota_service import (
    consume_and_create_message,
    refund_analysis,
    get_allowance,
)
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
# Prevent expensive bursts of analysis requests. Each submission can trigger
# 6 paid LLM calls and may be retried up to 3 times. Limit each user to
# 10 POST submissions per hour and 30 per day. A separate lifetime quota
# controls the user's total usage and overall cost.
@limiter.limit("10 per hour", key_func=user_key, methods=["POST"])
@limiter.limit("30 per day", key_func=user_key, methods=["POST"])
def new_message():
    """
    Display a message submission form and process a new message submission for the logged-in user.
    """
    form = MessageSubmissionForm()

    if form.validate_on_submit():
        # Stage 3: save the message as PENDING and return immediately; the heavy
        # ML/LLM pipeline runs in the Celery task. The show_message page then polls
        # for status and reloads when the analysis finishes.
        try:
            # Consume one lifetime analysis and INSERT the message in a single
            # transaction (quota_service). Returns None — no message written —
            # when the conditional UPDATE found no slot left. The gate is that
            # UPDATE's rowcount, not a prior read, so two concurrent submissions
            # cannot both take the last slot.
            message = consume_and_create_message(
                user_id=current_user.id,
                raw_text=form.raw_text.data,
                subject=form.subject.data or None,
                sender_email=form.sender_email.data or None
            )
            if message is None:
                flash(
                    f"You've used all {get_allowance()} of your analyses. "
                    "Analyses that don't produce a result are refunded automatically.",
                    "warning",
                )
                return render_template("messages/new_message.html", form=form)
        except Exception as e:
            logger.error(
                "Error saving message submission error_type=%s",
                type(e).__name__,
                extra={
                    "error_type": type(e).__name__
                }
            )
            flash("Something went wrong while saving the message", "danger")
            return render_template("messages/new_message.html", form=form)

        # If the broker is unreachable, don't leave the message PENDING forever with
        # no task queued — drive it to terminal FAILED so the user sees a clear
        # failure instead of an eternal "Analyzing…" spinner.
        if not queue_message_analysis(message.id):
            mark_analysis_failed(message.id)
            # The broker is unreachable, so no result will ever arrive — return the
            # slot, same as every other FAILED path. Marking first then refunding
            # settles the terminal state before the compensating action; the order
            # is a readability choice, not a correctness requirement, since
            # refund_analysis() commits on its own.
            refund_analysis(message.id)
            flash(
                "We couldn't start the analysis right now. Please try again in a moment. "
                "This didn't use one of your analyses.",
                "danger",
            )
            return redirect(url_for("messages.show_message", message_id=message.id))

        flash("Analysis started — results will appear shortly.", "success")
        return redirect(url_for("messages.show_message", message_id=message.id))

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


@messages_bp.route("/<int:message_id>/status", methods=["GET"])
@login_required
@verified_required
# EXEMPT: the analysis page polls this every 3s for up to ~5 minutes (POLL_MS in
# show_message.html), which is ~20 req/min per open tab. Any default limit here
# would break the async UX. Safe to exempt: one ownership-scoped SELECT, no
# writes, no template render, no external calls.
@limiter.exempt
def message_status(message_id):
    """
    Cheap JSON status for the async poller: one ownership-scoped DB lookup, no
    template render. Returns the exact MessageStatus VALUE string ("pending" /
    "processing" / "completed" / "failed") — never str(enum), which would be
    'MessageStatus.COMPLETED' and break the JS compare.
    """
    message = get_user_message_or_404(message_id)
    status = message.status.value if isinstance(message.status, MessageStatus) else message.status
    return {"status": status}


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

    Only terminal states may be archived. Archiving a PENDING/PROCESSING message
    races the worker: workflow_service later writes COMPLETED/FAILED over the
    ARCHIVED status and the message silently returns to the inbox.
    """
    message = get_user_message_or_404(message_id)

    if message.status not in (MessageStatus.COMPLETED, MessageStatus.FAILED):
        flash("This email is still being analyzed. Try again once it finishes.", "info")
        return redirect(url_for("messages.show_message", message_id=message_id))

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

    Restores the state the message can actually render rather than assuming
    success: a FAILED message restored as COMPLETED would show an empty analysis
    block on the detail page. AnalysisResult presence is the same signal
    workflow_service.analysis_already_done() uses to decide the work finished.

    Restoring status is all that is needed. Every consumer of archiving —
    the inbox (index), the dashboard counts and lists, and the tasks page —
    derives from `status != ARCHIVED` at query time, so there is no
    denormalized counter or cached aggregate to update alongside this.
    """
    message = get_user_message_or_404(message_id)
    message.status = (
        MessageStatus.COMPLETED if message.analysis_result else MessageStatus.FAILED
    )

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
