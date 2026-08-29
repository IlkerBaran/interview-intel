"""
Notification delete / clear-read route tests.

The bell badge is kept in sync from the server: every mutation returns the
unread_count recomputed AFTER its commit, so notifications.js never has to know
whether the row it deleted happened to be unread. These tests pin that contract
as well as the deletions themselves.
"""

from app.extensions import db
from app.models import Notification, NotificationType, User


def _make_notification(user_id, is_read=False, text="Interview detected"):
    notification = Notification(
        user_id=user_id,
        message=text,
        notification_type=NotificationType.INTERVIEW_DETECTED,
        is_read=is_read,
    )
    db.session.add(notification)
    db.session.commit()
    return notification


def _other_user():
    user = User()
    user.email = "someone_else@example.com"
    user.password = "T3st-secret*"
    user.is_verified = True
    db.session.add(user)
    db.session.commit()
    return user


def _current_user_id(client):
    with client.session_transaction() as sess:
        return int(sess["_user_id"])


# ── delete one ─────────────────────────────────────────────────────

def test_delete_removes_the_row(client_logged_in):
    notification = _make_notification(_current_user_id(client_logged_in))

    resp = client_logged_in.post(f"/notifications/{notification.id}/delete")

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert db.session.get(Notification, notification.id) is None


def test_deleting_unread_decrements_returned_unread_count(client_logged_in):
    """The badge drops because the server says so, not because the JS guessed."""
    user_id = _current_user_id(client_logged_in)
    target = _make_notification(user_id, is_read=False)
    _make_notification(user_id, is_read=False, text="Offer detected")

    body = client_logged_in.post(f"/notifications/{target.id}/delete").get_json()

    assert body["unread_count"] == 1


def test_deleting_read_leaves_unread_count_unchanged(client_logged_in):
    user_id = _current_user_id(client_logged_in)
    target = _make_notification(user_id, is_read=True)
    _make_notification(user_id, is_read=False)

    body = client_logged_in.post(f"/notifications/{target.id}/delete").get_json()

    assert body["unread_count"] == 1
    assert body["read_count"] == 0


# ── clear read ─────────────────────────────────────────────────────

def test_clear_read_removes_only_read_rows(client_logged_in):
    user_id = _current_user_id(client_logged_in)
    # Ids are captured up front: the assertions below expunge the session, which
    # detaches these instances and makes attribute access raise.
    read_one_id = _make_notification(user_id, is_read=True, text="Old one").id
    read_two_id = _make_notification(user_id, is_read=True, text="Older one").id
    unread_id = _make_notification(user_id, is_read=False, text="Still unseen").id

    body = client_logged_in.post("/notifications/clear-read").get_json()

    # The route deletes in bulk without syncing the identity map, so drop the
    # test session's cached instances and re-read from the database.
    db.session.expunge_all()

    assert body["ok"] is True
    assert db.session.get(Notification, read_one_id) is None
    assert db.session.get(Notification, read_two_id) is None
    # Nothing the user has not seen may be destroyed.
    assert db.session.get(Notification, unread_id) is not None


def test_clear_read_leaves_unread_count_untouched(client_logged_in):
    user_id = _current_user_id(client_logged_in)
    _make_notification(user_id, is_read=True)
    _make_notification(user_id, is_read=False)
    _make_notification(user_id, is_read=False)

    body = client_logged_in.post("/notifications/clear-read").get_json()

    assert body["unread_count"] == 2
    assert body["read_count"] == 0


def test_clear_read_does_not_touch_other_users(client_logged_in):
    other = _other_user()
    theirs = _make_notification(other.id, is_read=True, text="Not yours")
    _make_notification(_current_user_id(client_logged_in), is_read=True)

    client_logged_in.post("/notifications/clear-read")

    assert db.session.get(Notification, theirs.id) is not None


# ── ownership ──────────────────────────────────────────────────────

def test_delete_404s_on_another_users_notification(client_logged_in):
    theirs = _make_notification(_other_user().id)

    resp = client_logged_in.post(f"/notifications/{theirs.id}/delete")

    assert resp.status_code == 404
    assert db.session.get(Notification, theirs.id) is not None
