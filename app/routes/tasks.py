import logging
from urllib.parse import urlsplit

from flask import Blueprint, render_template, redirect, url_for, flash, abort, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Task, Message, MessageStatus


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


def safe_redirect(default_endpoint="tasks.show_all_tasks"):
    """
    Redirect user to the page they originally wanted after login.
    If "next" is missing or unsafe, go to a default endpoint.

    Prevents open redirect vulnerabilities.
    """
    next_page = request.args.get("next")

    if not next_page or urlsplit(next_page).netloc:
        return redirect(url_for(default_endpoint))

    return redirect(url_for(next_page))


@tasks_bp.route("/", methods=["GET"])
@login_required
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
def show_single_task(task_id):
    """
    Display a single task if it belongs to the logged-in user
    """

    task = get_user_task_or_404(task_id)

    return render_template("tasks/show_single_task.html", task=task)


@tasks_bp.route("/<int:task_id>/toggle", methods=["POST"])
@login_required
def toggle_task(task_id):
    """
    Toggle task between completed/incomplete if it belongs to the logged-in user
    """

    task = get_user_task_or_404(task_id)

    try:
        task.is_completed = not task.is_completed
        db.session.commit()

        flash("Task updated successfully!", "success")

    except Exception:
        db.session.rollback()
        logger.exception("Error while toggling task status")
        flash("Something went wrong while updating the task!", "danger")

    # redirect to next if provided, otherwise task list
    next_page = request.args.get("next")
    return redirect(next_page or url_for("tasks.show_all_tasks"))


@tasks_bp.route("/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_task(task_id):
    """
    Delete a task if it belongs to the logged-in user.
    """

    task = get_user_task_or_404(task_id)

    try:
        db.session.delete(task)
        db.session.commit()

        flash("Task deleted successfully!", "success")

    except Exception:
        db.session.rollback()
        logger.exception("Error while deleting task")
        flash("Something went wrong while deleting the task!", "danger")

    return safe_redirect()

