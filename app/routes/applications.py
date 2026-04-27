import logging
from sqlalchemy import func

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models import JobApplication, ApplicationStatus
from app.forms.application_forms import JobApplicationForm
from app.utils import verified_required

logger = logging.getLogger(__name__)

applications_bp = Blueprint("applications", __name__, url_prefix="/applications")


def get_user_application_or_404(application_id: int) -> JobApplication:
    """
    Authorization Security check!

    Return a JobApplication by id only if it belongs to the current logged-in user
    Aborts with 404 if not found or does not belong to current user
    """
    application = db.session.execute(
        db.select(JobApplication).where(
            JobApplication.id == application_id,
            JobApplication.user_id == current_user.id
        )
    ).scalar_one_or_none()

    if not application:
        abort(404)
    return application


@applications_bp.route("/")
@login_required
@verified_required
def index():
    """
    Pipeline view — all applications grouped by status
    with counts per stage and optional status filter.
    """

    status_filter = request.args.get("status")

    valid_statuses = {status.value for status in ApplicationStatus}

    # query 1 Aggregated data (status + count) — counts per status for all
    count_rows = db.session.execute(
        db.select(JobApplication.status, func.count().label("count")).where(
            JobApplication.user_id == current_user.id
        ).group_by(JobApplication.status)
    ).all()

    # build counts dict with zero defaults for all statuses
    count_by_status = {status.value: 0 for status in ApplicationStatus}

    # fill in the real values
    for row in count_rows:
        count_by_status[row.status] = row.count

    # query 2 — filtered applications list
    query = db.select(JobApplication).where(
        JobApplication.user_id == current_user.id
    ).order_by(JobApplication.updated_at.desc())

    if status_filter in valid_statuses:
        query = query.where(JobApplication.status == status_filter)

    applications = db.session.execute(query).scalars().all()

    # Apply optional filters (company and/or source); supports combining multiple filters
    company_filter = request.args.get("company")
    source_filter = request.args.get("source")

    if company_filter:
        query = query.where(JobApplication.company == company_filter)

    if source_filter:
        query = query.where(JobApplication.source == source_filter)

    return render_template(
        "applications/index.html",
        applications=applications,
        counts=count_by_status,
        active_filter=status_filter,
        company_filter=company_filter,
        source_filter=source_filter,
        ApplicationStatus=ApplicationStatus,
    )


@applications_bp.route("/new", methods=["GET", "POST"])
@login_required
@verified_required
def new_application():
    """Create a new job application"""
    form = JobApplicationForm()

    if form.validate_on_submit():
        application = JobApplication(
            user_id=current_user.id,
            company=form.company.data,
            role=form.role.data,
            status=form.status.data,
            source=form.source.data or None,
            posting_url=form.posting_url.data or None,
            applied_date=form.applied_date.data,
            note=form.note.data or None
        )

        try:
            db.session.add(application)
            db.session.commit()
            logger.info(
                "JobApplication created application_id=%s user_id=%s",
                application.id,
                current_user.id,
                extra={
                    "application_id": application.id,
                    "user_id": current_user.id
                }
            )
        except Exception as e:
            db.session.rollback()
            logger.error(
                "Failed to create JobApplication for user_id=%s error_type=%s",
                current_user.id,
                type(e).__name__,
                extra={
                    "user_id": current_user.id,
                    "error_type": type(e).__name__
                }
            )
            flash("Something went wrong. Please try again.", "danger")
            return redirect(url_for("applications.new_application"))

        flash(f"{application.company} — {application.role} added.", "success")
        return redirect(url_for("applications.index"))

    return render_template("applications/new.html", form=form)


@applications_bp.route("/<int:application_id>")
@login_required
@verified_required
def show_application(application_id):
    """Show a single application with full details and linked emails."""
    application = get_user_application_or_404(application_id)

    return render_template(
        "applications/show.html",
        application=application,
        ApplicationStatus=ApplicationStatus
    )


@applications_bp.route("/<int:application_id>/edit", methods=["GET", "POST"])
@login_required
@verified_required
def edit_application(application_id):
    """Edit an existing job application."""
    application = get_user_application_or_404(application_id)
    form = JobApplicationForm(obj=application)

    if form.validate_on_submit():
        application.company = form.company.data
        application.role = form.role.data
        application.status = form.status.data
        application.source = form.source.data or None
        application.posting_url = form.posting_url.data or None
        application.applied_date = form.applied_date.data
        application.note = form.note.data or None

        try:
            db.session.commit()
            logger.info(
                "JobApplication updated application_id=%s user_id=%s",
                application.id,
                current_user.id,
                extra={
                    "application_id": application.id,
                    "user_id": current_user.id,
                }
            )
        except Exception as e:
            db.session.rollback()
            logger.error(
                "Failed to update JobApplication application_id=%s user_id=%s error_type=%s",
                application.id,
                current_user.id,
                type(e).__name__,
                extra={
                    "application_id": application.id,
                    "user_id": current_user.id,
                    "error_type": type(e).__name__
                }
            )

            flash("Something went wrong. Please try again.", "danger")
            return redirect(url_for("applications.edit_application", application_id=application_id))

        flash(f"{application.company} — {application.role} updated.", "success")
        return redirect(url_for("applications.show_application", application_id=application.id))

    return render_template("applications/edit.html", form=form, application=application)


@applications_bp.route("/<int:application_id>/delete", methods=["POST"])
@login_required
@verified_required
def delete_application(application_id):
    """Delete a job application and all linked messages."""
    application = get_user_application_or_404(application_id)
    company = application.company
    role = application.role

    try:
        db.session.delete(application)
        db.session.commit()
        logger.info(
            "JobApplication deleted application_id=%s user_id=%s",
            application_id,
            current_user.id,
            extra={
                "application_id": application_id,
                "user_id": current_user.id
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to delete JobApplication application_id=%s user_id=%s error_type=%s",
            application.id,
            current_user.id,
            type(e).__name__,
            extra={
                "application_id": application.id,
                "user_id": current_user.id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("applications.show_application", application_id=application_id))

    flash(f"{company} — {role} deleted.", "success")
    return redirect(url_for("applications.index"))


@applications_bp.route("/<int:application_id>/status", methods=["POST"])
@login_required
@verified_required
def update_status(application_id):
    """
    Quick status update from pipeline view or detail view.
    Accepts new_status from form POST — no full form needed.
    """
    application = get_user_application_or_404(application_id)
    new_status = request.form.get("status")

    valid_statuses = {s.value for s in ApplicationStatus}

    if new_status not in valid_statuses:
        flash("Invalid status.", "danger")
        return redirect(url_for("applications.show_application", application_id=application_id))

    # guard — no-op if status unchanged
    if new_status == application.status:
        flash(
            f"This application status is already {ApplicationStatus(new_status).label}.",
            "info"
        )
        return redirect(url_for("applications.show_application", application_id=application_id))

    application.status = new_status

    try:
        db.session.commit()
        flash(
            f"{application.company} status updated to {ApplicationStatus(new_status).label}.",
            "success"
        )
        logger.info(
            "JobApplication status updated application_id=%s user_id=%s",
            application.id,
            current_user.id,
            extra={
                "application_id": application.id,
                "user_id": current_user.id
            }
        )
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to update status for JobApplication application_id=%s user_id=%s error_type=%s",
            application.id,
            current_user.id,
            type(e).__name__,
            extra={
                "application_id": application.id,
                "user_id": current_user.id,
                "error_type": type(e).__name__
            }
        )
        flash("Something went wrong. Please try again.", "danger")

    return redirect(url_for("applications.index"))
