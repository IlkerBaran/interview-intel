from sqlalchemy import func

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Message, Task, MessageStatus
from app.utils import verified_required

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.route("/", methods=["GET"])
@login_required
@verified_required
def index():
    """
    Display the dashboard for the logged-in user.
    Shows user-specific messages, tasks, and summary counts.
    """

    # ── total messages (all time) ──
    total_messages = db.session.execute(
        db.select(func.count(Message.id))
        .where(Message.user_id == current_user.id)
    ).scalar() or 0

    # ── active messages (not archived) ──
    active_messages = db.session.execute(
        db.select(func.count(Message.id))
        .where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED
        )
    ).scalar() or 0

    # ── recent messages list ──
    recent_messages = db.session.execute(
        db.select(Message)
        .where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED
        )
        .order_by(Message.created_at.desc())
        .limit(5)
    ).scalars().all()

    # ── task counts (one query) ──
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

    completed_tasks  = task_counts.completed
    incomplete_tasks = task_counts.incomplete

    # ── incomplete tasks list (for display) ──
    incomplete_tasks_list = db.session.execute(
        db.select(Task)
        .join(Message)
        .where(
            Message.user_id == current_user.id,
            Message.status != MessageStatus.ARCHIVED,
            Task.is_completed.is_(False)
        )
        .order_by(Task.created_at.desc())
        .limit(8)
    ).scalars().all()

    return render_template(
        "dashboard/index.html",
        total_messages=total_messages,
        active_messages=active_messages,
        recent_messages=recent_messages,
        incomplete_tasks_list=incomplete_tasks_list,
        completed_tasks=completed_tasks,
        incomplete_tasks=incomplete_tasks,
    )