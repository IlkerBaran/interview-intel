"""
Archive / unarchive route tests.

Archiving is a Message.status value, and every consumer of it — the inbox, the
dashboard counts and lists, and the tasks page — derives from
`status != ARCHIVED` at query time. These tests pin that round trip, including
the two correctness rules the routes enforce:

  * only terminal states may be archived (otherwise the worker later writes
    COMPLETED/FAILED over ARCHIVED and the message silently returns)
  * restoring picks the state the message can actually render, rather than
    assuming success
"""

from app.extensions import db
from app.models import AnalysisResult, Message, MessageStatus, Task, User


def _make_message(user_id, status=MessageStatus.COMPLETED, with_analysis=True):
    message = Message(
        user_id=user_id,
        raw_text="Interview invitation for the backend role.",
        status=status,
    )
    db.session.add(message)
    db.session.flush()

    if with_analysis:
        db.session.add(AnalysisResult(message_id=message.id))

    db.session.commit()
    return message


def _make_task(message_id, name="Reply to recruiter"):
    task = Task(message_id=message_id, task_name=name)
    db.session.add(task)
    db.session.commit()
    return task


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


# ── archive ────────────────────────────────────────────────────────

def test_archive_completed_message(client_logged_in):
    message = _make_message(_current_user_id(client_logged_in))

    resp = client_logged_in.post(f"/messages/{message.id}/archive")

    assert resp.status_code == 302
    assert db.session.get(Message, message.id).status == MessageStatus.ARCHIVED


def test_archive_rejects_processing_message(client_logged_in):
    """
    The worker race: workflow_service later writes COMPLETED/FAILED, which would
    silently undo the archive. The route must refuse instead.
    """
    message = _make_message(
        _current_user_id(client_logged_in),
        status=MessageStatus.PROCESSING,
        with_analysis=False,
    )

    resp = client_logged_in.post(f"/messages/{message.id}/archive")

    assert resp.status_code == 302
    assert db.session.get(Message, message.id).status == MessageStatus.PROCESSING


def test_archive_hides_message_and_its_tasks_everywhere(client_logged_in):
    """
    Archiving is not only an inbox filter — it also removes the message's tasks
    from the tasks page and from the dashboard counts.
    """
    message = _make_message(_current_user_id(client_logged_in))
    _make_task(message.id, name="Prepare for the system design round")

    assert b"Prepare for the system design round" in client_logged_in.get("/tasks/").data

    client_logged_in.post(f"/messages/{message.id}/archive")

    assert b"Prepare for the system design round" not in client_logged_in.get("/tasks/").data
    assert b"Prepare for the system design round" not in client_logged_in.get("/dashboard/").data


# ── unarchive ──────────────────────────────────────────────────────

def test_unarchive_with_analysis_restores_completed(client_logged_in):
    message = _make_message(
        _current_user_id(client_logged_in),
        status=MessageStatus.ARCHIVED,
        with_analysis=True,
    )

    resp = client_logged_in.post(f"/messages/{message.id}/unarchive")

    assert resp.status_code == 302
    assert db.session.get(Message, message.id).status == MessageStatus.COMPLETED


def test_unarchive_without_analysis_restores_failed(client_logged_in):
    """
    The mislabel fix: a message archived while FAILED must not come back
    claiming success — show_message would render an empty analysis block.
    """
    message = _make_message(
        _current_user_id(client_logged_in),
        status=MessageStatus.ARCHIVED,
        with_analysis=False,
    )

    client_logged_in.post(f"/messages/{message.id}/unarchive")

    assert db.session.get(Message, message.id).status == MessageStatus.FAILED


def test_unarchive_restores_tasks_to_list_and_dashboard(client_logged_in):
    """
    Nothing is restored explicitly — every consumer derives from status, so this
    proves the derived filters come back together.
    """
    message = _make_message(_current_user_id(client_logged_in))
    _make_task(message.id, name="Send thank-you note")

    client_logged_in.post(f"/messages/{message.id}/archive")
    assert b"Send thank-you note" not in client_logged_in.get("/tasks/").data

    client_logged_in.post(f"/messages/{message.id}/unarchive")

    assert b"Send thank-you note" in client_logged_in.get("/tasks/").data
    assert b"Send thank-you note" in client_logged_in.get("/dashboard/").data


# ── ownership ──────────────────────────────────────────────────────

def test_archive_404s_on_another_users_message(client_logged_in):
    message = _make_message(_other_user().id)

    assert client_logged_in.post(f"/messages/{message.id}/archive").status_code == 404
    assert db.session.get(Message, message.id).status == MessageStatus.COMPLETED


def test_unarchive_404s_on_another_users_message(client_logged_in):
    message = _make_message(_other_user().id, status=MessageStatus.ARCHIVED)

    assert client_logged_in.post(f"/messages/{message.id}/unarchive").status_code == 404
    assert db.session.get(Message, message.id).status == MessageStatus.ARCHIVED
