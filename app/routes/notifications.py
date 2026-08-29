import logging
from datetime import datetime, UTC

from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy import func, tuple_

from app.extensions import db, limiter
from app.models import Notification
from app.utils import verified_required


logger = logging.getLogger(__name__)

notifications_bp = Blueprint("notifications", __name__, url_prefix="/notifications")

# "Load more" page size. This app generates notifications sparingly,
# so a modest page keeps the first paint light without real cost.
PAGE_SIZE = 20


def get_user_notification_or_404(notification_id):
    """
    Authorization security check!

    Return a notification only if it belongs to the current
    logged-in user. Ownership is enforced via user_id in the
    WHERE clause, so a row owned by another user is reported
    as 404 — its existence is never disclosed.

    Notification -> User
    """
    notification = db.session.execute(
        db.select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    ).scalar_one_or_none()

    if notification is None:
        abort(404)
    return notification


def _unread_count(user_id):
    """Current unread notification count for the given user"""
    return db.session.scalar(
        db.select(func.count(Notification.id))
        .where(Notification.user_id == user_id)
        .where(Notification.is_read.is_(False))
    ) or 0


def _read_count(user_id):
    """
    Current READ notification count for the given user.

    Drives whether the "Clear read" button renders, mirroring how unread_count
    drives "Mark all as read". Covered by ix_notifications_user_id_is_read.
    """
    return db.session.scalar(
        db.select(func.count(Notification.id))
        .where(Notification.user_id == user_id)
        .where(Notification.is_read.is_(True))
    ) or 0


def _fetch_notifications_page(user_id, cursor):
    """
    Fetch one page of notifications for the user, newest first, using
    keyset/cursor pagination on the (created_at, id) tuple.

    cursor is None for the first page, or a (datetime, int) tuple for later
    pages. The id tiebreaker prevents skipped or duplicated notifications
    when multiple notifications share the same created_at timestamp.

    Fetches PAGE_SIZE + 1 notifications so the caller can tell whether more
    notifications remain without running a second query.

    Returns:
        page_notifications: notifications to show on this page
        has_more_notifications: True if another page exists
        next_cursor: bookmark the frontend sends back to load more
    """
    query = (
        db.select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(PAGE_SIZE + 1)
    )
    if cursor is not None:
        cursor_dt, cursor_id = cursor
        query = query.where(
            tuple_(Notification.created_at, Notification.id) < tuple_(cursor_dt, cursor_id)
        )

    notifications = db.session.execute(query).scalars().all()

    has_more_notifications = len(notifications) > PAGE_SIZE
    page_items = notifications[:PAGE_SIZE]

    next_cursor = None
    if has_more_notifications and page_items:
        oldest_notification = page_items[-1]
        next_cursor = f"{oldest_notification.created_at.isoformat()},{oldest_notification.id}"

    return page_items, has_more_notifications, next_cursor


def _parse_cursor(raw_cursor):
    """
    Parse a "<ISO timestamp>,<id>" cursor into a
    (tz-aware UTC datetime, int) tuple.
    """
    if not raw_cursor:
        abort(400)

    try:
        # parse the raw_cursor into two strings
        timestamp_str, id_str = raw_cursor.rsplit(",", 1)

        # convert to parsed timestamp_str, id_str to datetime object and integer
        cursor_dt = datetime.fromisoformat(timestamp_str)
        cursor_id = int(id_str)
    except (ValueError, TypeError):
        abort(400)

    if cursor_dt.tzinfo is None:
        cursor_dt = cursor_dt.replace(tzinfo=UTC)

    return cursor_dt, cursor_id


@notifications_bp.route("/", methods=["GET"])
@login_required
@verified_required
def index():
    """
    Display the first page of notifications for the logged-in
    user, newest first. Further pages load via /notifications/more.
    """
    page_items, has_more_notifications, next_cursor = (
        _fetch_notifications_page(current_user.id, None)
    )

    return render_template(
        "notifications/index.html",
        notifications=page_items,
        has_more=has_more_notifications,
        next_cursor=next_cursor,
        read_count=_read_count(current_user.id),
    )


@notifications_bp.route("/more", methods=["GET"])
@login_required
@verified_required
# EXEMPT: driven by notifications.js, so a user working through a long list
# fires these in bursts. Cheap and ownership-scoped — auth plus the user_id
# WHERE clause is the real protection here, not a rate limit.
@limiter.exempt
def load_more_notifications():
    """
    Return the next page of notifications as a rendered HTML fragment
    for AJAX loading.

    This route requires a valid cursor because it only loads pages after
    the first page. Notifications are keyset-paginated by (created_at, id)
    and scoped to the logged-in user.
    """
    cursor = _parse_cursor(request.args.get("cursor"))

    page_items, has_more_notifications, next_cursor = (
        _fetch_notifications_page(current_user.id, cursor)
    )

    html = render_template(
        "notifications/_notification_items.html",
        notifications=page_items
    )

    return jsonify(
        ok=True,
        html=html,
        has_more=has_more_notifications,
        next_cursor=next_cursor
    )


@notifications_bp.route("/<int:notification_id>/read", methods=["POST"])
@login_required
@verified_required
# EXEMPT: driven by notifications.js, so a user working through a long list
# fires these in bursts. Cheap and ownership-scoped — auth plus the user_id
# WHERE clause is the real protection here, not a rate limit.
@limiter.exempt
def mark_read(notification_id):
    """
    Mark a single notification as read (AJAX). Ownership is
    enforced by the scoped 404 lookup — another user's row is
    indistinguishable from a missing one.
    """
    notification = get_user_notification_or_404(notification_id)

    if not notification.is_read:
        notification.is_read = True
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(
                "Failed to mark notification read notification_id=%s error_type=%s",
                notification_id,
                type(e).__name__,
                extra={
                    "notification_id": notification_id,
                    "error_type": type(e).__name__
                }
            )
            return jsonify(ok=False), 500

    return jsonify(
        ok=True,
        unread_count=_unread_count(current_user.id),
        read_count=_read_count(current_user.id),
    )


@notifications_bp.route("/read-all", methods=["POST"])
@login_required
@verified_required
# EXEMPT: driven by notifications.js, so a user working through a long list
# fires these in bursts. Cheap and ownership-scoped — auth plus the user_id
# WHERE clause is the real protection here, not a rate limit.
@limiter.exempt
def mark_all_read():
    """
    Mark every unread notification for the user as read in a
    single batch UPDATE — no row loading, no per-row loop.
    """
    try:
        db.session.execute(
            db.update(Notification)
            .where(
                Notification.user_id == current_user.id,
                Notification.is_read.is_(False)
            )
            .values(is_read=True)
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to mark all notifications read user_id=%s error_type=%s",
            current_user.id,
            type(e).__name__,
            extra={"error_type": type(e).__name__}
        )
        return jsonify(ok=False), 500

    return jsonify(ok=True, unread_count=0, read_count=_read_count(current_user.id))


@notifications_bp.route("/<int:notification_id>/delete", methods=["POST"])
@login_required
@verified_required
# EXEMPT: driven by notifications.js, so a user clearing a long list fires these
# in bursts. Cheap and ownership-scoped — auth plus the user_id WHERE clause is
# the real protection here, not a rate limit. Matches mark_read.
@limiter.exempt
def delete_notification(notification_id):
    """
    Delete a single notification (AJAX). Ownership is enforced by the
    scoped 404 lookup — another user's row is indistinguishable from a
    missing one.

    Returns the unread count recomputed AFTER the commit, so the caller
    never has to know whether the deleted row was unread: notifications.js
    feeds the value straight to syncBadge().
    """
    notification = get_user_notification_or_404(notification_id)

    try:
        db.session.delete(notification)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to delete notification notification_id=%s error_type=%s",
            notification_id,
            type(e).__name__,
            extra={
                "notification_id": notification_id,
                "error_type": type(e).__name__
            }
        )
        return jsonify(ok=False), 500

    return jsonify(
        ok=True,
        unread_count=_unread_count(current_user.id),
        read_count=_read_count(current_user.id),
    )


@notifications_bp.route("/clear-read", methods=["POST"])
@login_required
@verified_required
# EXEMPT: same reasoning as mark_all_read — one batch statement, ownership
# scoped, and only ever destroys the caller's own rows.
@limiter.exempt
def clear_read():
    """
    Delete every ALREADY-READ notification for the user in a single batch
    DELETE — no row loading, no per-row loop.

    Read-only by design: unread notifications are never touched, so nothing
    the user has not yet seen can be destroyed, and the unread count is
    unchanged by construction. It is still recomputed rather than assumed,
    so the response shape matches the other mutations exactly.
    """
    try:
        db.session.execute(
            db.delete(Notification)
            .where(
                Notification.user_id == current_user.id,
                Notification.is_read.is_(True)
            )
            # Nothing reads these objects again before the response is returned,
            # so skip the identity-map sync. Stated explicitly because a bulk
            # DELETE removes rows: unlike mark_all_read's UPDATE, leaving the
            # default to evaluate criteria against loaded instances would only
            # cost work here.
            .execution_options(synchronize_session=False)
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to clear read notifications user_id=%s error_type=%s",
            current_user.id,
            type(e).__name__,
            extra={"error_type": type(e).__name__}
        )
        return jsonify(ok=False), 500

    return jsonify(
        ok=True,
        unread_count=_unread_count(current_user.id),
        read_count=_read_count(current_user.id),
    )
