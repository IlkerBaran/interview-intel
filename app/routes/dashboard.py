from flask import Blueprint, render_template
from flask_login import login_required, current_user
from sqlalchemy import func

from app.models import Message, Task, MessageStatus
from app.extensions import db


dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.route("/", methods=["GET"])
@login_required
def index():
    """
    Display the dashboard for the logged-in user.
    Shows user-specific messages, tasks, and summary counts.
    """

    recent_messages = db.session.execute(
        db.select(Message).where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED
        )
        .order_by(Message.created_at.desc())
        .limit(5)
    ).scalars().all()


    recent_tasks = db.session.execute(
        db.select(Task).join(Message).where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED
        )
        .order_by(Task.created_at.desc())
        .limit(5)
    ).scalars().all()


    task_counts = db.session.execute(
        db.select(
            func.count().filter(Task.is_completed.is_(True)).label("completed"),
            func.count().filter(Task.is_completed.is_(False)).label("incomplete")
        )
        .select_from(Task)
        .join(Message)
        .where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED
        )
    ).one()


    completed_task_counts = task_counts.completed
    incomplete_task_counts = task_counts.incomplete
    total_tasks = task_counts.completed + task_counts.incomplete


    incomplete_tasks = db.session.execute(
        db.select(Task).join(Message).where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED,
            Task.is_completed.is_(False)
        )
        .order_by(Task.created_at.desc())
        .limit(10)
    ).scalars().all()


    return render_template(
        "dashboard/index.html",
        recent_messages=recent_messages,
        recent_tasks=recent_tasks,
        total_tasks=total_tasks,
        completed_task_count=completed_task_counts,
        incomplete_task_count=incomplete_task_counts,
        incomplete_tasks=incomplete_tasks
    )