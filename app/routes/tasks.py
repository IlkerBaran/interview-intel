import logging

from flask import Blueprint, render_template, redirect, url_for, flash, abort, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Task, Message, MessageStatus
from app.utils import verified_required, safe_redirect

logger = logging.getLogger(__name__)

tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")


def get_user_task_or_404(task_id):
    """
    Authorization Security check Helper Function!
    Return a task only if it belongs to the current logged-in user.

    Task does not have a direct user_id column, so ownership is checked
    through the related Message model:
    Task -> Message -> User
    """
    task = db.session.execute(
        db.select(Task).join(Message).where(
            Task.id == task_id,
            Message.user_id == current_user.id
        )
    ).scalar_one_or_none()

    if task is None:
        abort(404)
    return task


@tasks_bp.route("/", methods=["GET"])
@login_required
@verified_required
def show_all_tasks():
    """
    Display all tasks that belong to the logged-in user,
    ordered by newest first.
    """
    tasks = db.session.execute(
        db.select(Task).join(Message).where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED
        ).order_by(Task.created_at.desc())
    ).scalars().all()

    return render_template("tasks/show_all_tasks.html", tasks=tasks)


@tasks_bp.route("/<int:task_id>", methods=["GET"])
@login_required
@verified_required
def show_single_task(task_id):
    """
    Display a single task if it belongs to the logged-in user
    """

    task = get_user_task_or_404(task_id)

    return render_template("tasks/show_single_task.html", task=task)


@tasks_bp.route("/<int:task_id>/toggle", methods=["POST"])
@login_required
@verified_required
def toggle_task(task_id):
    """
    Toggle task between completed/incomplete if it belongs to the logged-in user
    """

    task = get_user_task_or_404(task_id)

    try:
        task.is_completed = not task.is_completed
        db.session.commit()

        flash("Task updated successfully!", "success")

    except Exception as e:
        db.session.rollback()
        logger.error(
            "Error while toggling task status error_type=%s",
            type(e).__name__,
            extra={
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong while updating the task!", "danger")

    # safe_redirect() blocks external URLs and javascript: schemes — see utils.py
    return safe_redirect("tasks.show_all_tasks")


@tasks_bp.route("/<int:task_id>/delete", methods=["POST"])
@login_required
@verified_required
def delete_task(task_id):
    """
    Delete a task if it belongs to the logged-in user.
    """

    task = get_user_task_or_404(task_id)

    try:
        db.session.delete(task)
        db.session.commit()

        flash("Task deleted successfully!", "success")

    except Exception as e:
        db.session.rollback()
        logger.error(
            "Error while deleting task error_type=%s",
            type(e).__name__,
            extra={
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong while deleting the task!", "danger")

    # safe_redirect() blocks external URLs and javascript: schemes — see utils.py
    return safe_redirect("tasks.show_all_tasks")

