from sqlalchemy import func

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Message, Task, MessageStatus, ApplicationStatus, JobApplication
from app.utils import verified_required

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.route("/", methods=["GET"])
@login_required
@verified_required
def index():
    """
    Display the dashboard for the logged-in user.
    Shows user-specific messages, tasks, application metrics and summary counts.
    """

    # ── message counts (one query) ──
    _message_counts = db.session.execute(
        db.select(
            func.count(Message.id).label("total"),
            func.count(Message.id).filter(Message.status != MessageStatus.ARCHIVED).label("active")
        )
        .where(Message.user_id == current_user.id)
    ).one()

    total_messages = _message_counts.total
    active_messages = _message_counts.active

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

    # ── application counts (one GROUP BY query) ──
    app_count_rows = db.session.execute(
        db.select(JobApplication.status, func.count().label("count"))
        .where(JobApplication.user_id == current_user.id)
        .group_by(JobApplication.status)
    ).all()

    # build counts dict with zero defaults for all statuses
    app_counts = {status.value: 0 for status in ApplicationStatus}

    # fill in the real values
    for row in app_count_rows:
        app_counts[row.status] += row.count

    total_applications = sum(app_counts.values())
    applied_count = app_counts[ApplicationStatus.APPLIED.value]
    interviewing_count = app_counts[ApplicationStatus.INTERVIEWING.value]
    rejected_count = app_counts[ApplicationStatus.REJECTED.value]
    offered_count = app_counts[ApplicationStatus.OFFERED.value]

    # ── response rate ──
    responded = interviewing_count + offered_count + rejected_count
    response_rate = round(responded / applied_count * 100) if applied_count > 0 else 0

    return render_template(
        "dashboard/index.html",
        # messages
        total_messages=total_messages,
        active_messages=active_messages,
        recent_messages=recent_messages,
        # tasks
        incomplete_tasks_list=incomplete_tasks_list,
        completed_tasks=completed_tasks,
        incomplete_tasks=incomplete_tasks,
        # applications
        total_applications=total_applications,
        applied_count=applied_count,
        interviewing_count=interviewing_count,
        rejected_count=rejected_count,
        offered_count=offered_count,
        response_rate=response_rate,
    )