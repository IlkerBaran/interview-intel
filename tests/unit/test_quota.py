"""
Unit tests for the lifetime analysis quota (quota_service).

Covers the properties the feature has to hold:
  * consumption is atomic with the message INSERT and race-safe,
  * every FAILED path refunds — A/B/C/D/F unconditionally,
  * path E (nothing-to-show) refunds only within its per-user cap, and the
    cap budget is never spent without a refund actually landing,
  * refunds are idempotent under acks_late redelivery and clamped,
  * remaining never renders above the allowance or below zero.

No external services: in-memory SQLite, no Redis, no Celery worker.
"""

import pytest

from app.extensions import db
from app.models import User, Message, MessageStatus
from app.services.quota_service import (
    consume_and_create_message,
    refund_analysis,
    refund_nothing_to_show,
    get_allowance,
    get_nothing_to_show_cap,
    get_remaining,
    get_used,
)


@pytest.fixture
def user(app):
    u = User()
    u.email = "quota_user@example.com"
    u.password = "T3st-secret*"
    u.is_verified = True
    db.session.add(u)
    db.session.commit()
    return u


# ── allowance / derivation ────────────────────────────────────────────────────

def test_default_allowance_is_three(app):
    assert get_allowance() == 3


def test_allowance_reads_config_not_schema(app):
    """Changing the allowance takes effect immediately — no migration involved."""
    app.config["ANALYSIS_LIFETIME_QUOTA"] = 7
    assert get_allowance() == 7
    assert get_remaining.__module__  # sanity: same module, no cached constant


def test_new_user_starts_with_zero_used(app, user):
    assert get_used(user.id) == 0
    assert get_remaining(user.id) == 3


def test_remaining_is_derived_from_used(app, user):
    consume_and_create_message(user.id, "first email")
    assert get_used(user.id) == 1
    assert get_remaining(user.id) == 2


# ── consumption ───────────────────────────────────────────────────────────────

def test_consume_creates_message_and_increments(app, user):
    message = consume_and_create_message(user.id, "hello", subject="Interview")
    assert message is not None
    assert message.status == MessageStatus.PENDING
    assert message.user_id == user.id
    assert get_used(user.id) == 1


def test_consume_and_insert_share_one_transaction(app, user):
    """A consumed slot always has a message, and vice versa."""
    consume_and_create_message(user.id, "one")
    message_count = db.session.scalar(
        db.select(db.func.count(Message.id)).where(Message.user_id == user.id)
    )
    assert message_count == get_used(user.id) == 1


def test_consume_refused_when_exhausted(app, user):
    for i in range(3):
        assert consume_and_create_message(user.id, f"msg {i}") is not None

    assert get_remaining(user.id) == 0

    refused = consume_and_create_message(user.id, "one too many")
    assert refused is None


def test_refused_consume_writes_no_message(app, user):
    """The gate must not leave an orphan row behind."""
    for i in range(3):
        consume_and_create_message(user.id, f"msg {i}")

    before = db.session.scalar(
        db.select(db.func.count(Message.id)).where(Message.user_id == user.id)
    )
    consume_and_create_message(user.id, "refused")
    after = db.session.scalar(
        db.select(db.func.count(Message.id)).where(Message.user_id == user.id)
    )
    assert before == after == 3
    assert get_used(user.id) == 3


def test_consume_never_exceeds_allowance(app, user):
    for i in range(10):
        consume_and_create_message(user.id, f"msg {i}")
    assert get_used(user.id) == 3
    assert get_remaining(user.id) == 0


def test_last_slot_taken_only_once(app, user):
    """
    The conditional UPDATE is the gate. Simulates two submissions contending for
    the final slot: whichever runs second must find WHERE analyses_used < 3 no
    longer matching, so exactly one message is created.
    """
    consume_and_create_message(user.id, "a")
    consume_and_create_message(user.id, "b")
    assert get_remaining(user.id) == 1

    first = consume_and_create_message(user.id, "contender one")
    second = consume_and_create_message(user.id, "contender two")

    assert (first is not None) != (second is not None)   # exactly one won
    assert get_used(user.id) == 3


def test_consume_rejects_empty_text(app, user):
    with pytest.raises(ValueError):
        consume_and_create_message(user.id, "   ")
    assert get_used(user.id) == 0


def test_quota_is_per_user(app, user):
    other = User()
    other.email = "other@example.com"
    other.password = "T3st-secret*"
    other.is_verified = True
    db.session.add(other)
    db.session.commit()

    for i in range(3):
        consume_and_create_message(user.id, f"msg {i}")

    assert get_remaining(user.id) == 0
    assert get_remaining(other.id) == 3
    assert consume_and_create_message(other.id, "mine") is not None


# ── refunds ───────────────────────────────────────────────────────────────────

def test_refund_returns_the_slot(app, user):
    message = consume_and_create_message(user.id, "will fail")
    assert get_remaining(user.id) == 2

    assert refund_analysis(message.id) is True
    assert get_remaining(user.id) == 3
    assert get_used(user.id) == 0


def test_refund_sets_the_marker(app, user):
    message = consume_and_create_message(user.id, "will fail")
    assert message.quota_refunded is False

    refund_analysis(message.id)
    db.session.refresh(message)
    assert message.quota_refunded is True


def test_refund_is_idempotent_under_redelivery(app, user):
    """acks_late means a crashed task can re-run its failure handler."""
    message = consume_and_create_message(user.id, "will fail")

    assert refund_analysis(message.id) is True
    assert refund_analysis(message.id) is False
    assert refund_analysis(message.id) is False

    assert get_used(user.id) == 0
    assert get_remaining(user.id) == 3


def test_refund_never_pushes_remaining_above_allowance(app, user):
    """The 3 → 2 → 3 contract: never 4."""
    message = consume_and_create_message(user.id, "will fail")
    for _ in range(5):
        refund_analysis(message.id)
    assert get_remaining(user.id) <= get_allowance()
    assert get_remaining(user.id) == 3


def test_refund_clamps_at_zero_used(app, user):
    """A pre-quota message refunding must not drive the counter negative."""
    legacy = Message(user_id=user.id, raw_text="pre-quota message")
    legacy.status = MessageStatus.FAILED
    db.session.add(legacy)
    db.session.commit()

    assert get_used(user.id) == 0
    refund_analysis(legacy.id)
    assert get_used(user.id) == 0
    assert get_remaining(user.id) == get_allowance()


def test_refund_on_missing_message_is_safe(app, user):
    assert refund_analysis(999999) is False


def test_refund_frees_capacity_for_a_new_submission(app, user):
    for i in range(3):
        consume_and_create_message(user.id, f"msg {i}")
    failed = db.session.scalars(db.select(Message).where(Message.user_id == user.id)).first()

    assert consume_and_create_message(user.id, "blocked") is None
    refund_analysis(failed.id)
    assert consume_and_create_message(user.id, "now allowed") is not None


# ── which failure paths refund ────────────────────────────────────────────────

def test_infrastructure_failure_refunds_via_route(app, user):
    """Enqueue failure: mark FAILED, refund, and say so in the flash."""
    from app.services import workflow_service

    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(user.id)

    original = workflow_service.queue_message_analysis
    try:
        import app.routes.messages as messages_route
        messages_route.queue_message_analysis = lambda mid: False   # broker down

        response = client.post(
            "/messages/new",
            data={"raw_text": "an interview email body long enough to pass validation"},
            follow_redirects=True,
        )
        assert response.status_code == 200
    finally:
        messages_route.queue_message_analysis = original

    message = db.session.scalars(db.select(Message).where(Message.user_id == user.id)).first()
    assert message is not None
    assert message.status == MessageStatus.FAILED
    assert message.quota_refunded is True
    assert get_remaining(user.id) == 3
    assert b"didn&#39;t use one of your analyses" in response.data or \
           b"didn't use one of your analyses" in response.data


def test_nothing_to_show_refunds_within_the_cap(app, user):
    """
    First nothing-to-show result refunds normally — the pipeline produced nothing
    usable, so no result was delivered and the slot goes back.
    """
    from app.services.workflow_service import _mark_failed_nothing_to_show

    message = consume_and_create_message(user.id, "unusable input")
    assert get_remaining(user.id) == 2

    _mark_failed_nothing_to_show(message, "en")

    db.session.refresh(message)
    assert message.status == MessageStatus.FAILED
    assert message.quota_refunded is True
    assert get_remaining(user.id) == 3          # slot returned


# ── nothing-to-show refund cap ────────────────────────────────────────────────

def _nothing_to_show_cycle(user_id, label):
    """Consume a slot, then fail it as nothing-to-show. Returns the Message."""
    from app.services.workflow_service import _mark_failed_nothing_to_show
    message = consume_and_create_message(user_id, f"garbage input {label}")
    _mark_failed_nothing_to_show(message, "en")
    db.session.refresh(message)
    return message


def test_default_nothing_to_show_cap_is_two(app):
    assert get_nothing_to_show_cap() == 2


def test_first_two_nothing_to_show_refund_third_does_not(app, user):
    """The core of the cap: refund, refund, then stop."""
    first = _nothing_to_show_cycle(user.id, 1)
    assert first.quota_refunded is True
    assert get_remaining(user.id) == 3

    second = _nothing_to_show_cycle(user.id, 2)
    assert second.quota_refunded is True
    assert get_remaining(user.id) == 3

    third = _nothing_to_show_cycle(user.id, 3)
    assert third.quota_refunded is False        # cap spent
    assert get_remaining(user.id) == 2          # slot consumed


def test_every_nothing_to_show_still_reaches_failed(app, user):
    """The cap decides refunds only — never the terminal state."""
    for i in range(1, 4):
        message = _nothing_to_show_cycle(user.id, i)
        assert message.status == MessageStatus.FAILED


def test_nothing_to_show_stays_capped_after_the_third(app, user):
    _nothing_to_show_cycle(user.id, 1)
    _nothing_to_show_cycle(user.id, 2)
    _nothing_to_show_cycle(user.id, 3)
    assert get_remaining(user.id) == 2

    fourth = _nothing_to_show_cycle(user.id, 4)
    assert fourth.quota_refunded is False
    assert get_remaining(user.id) == 1          # consumed again, no refund


def test_cap_counter_only_counts_granted_refunds(app, user):
    _nothing_to_show_cycle(user.id, 1)
    assert db.session.scalar(
        db.select(User.nothing_to_show_refunds_used).where(User.id == user.id)) == 1

    _nothing_to_show_cycle(user.id, 2)
    assert db.session.scalar(
        db.select(User.nothing_to_show_refunds_used).where(User.id == user.id)) == 2

    _nothing_to_show_cycle(user.id, 3)           # refused — must not increment
    assert db.session.scalar(
        db.select(User.nothing_to_show_refunds_used).where(User.id == user.id)) == 2


def test_redelivery_does_not_leak_cap_budget(app, user):
    """
    A redelivered task re-runs the handler. The budget UPDATE succeeds but the
    marker claim fails, and the shared transaction rolls both back — so budget is
    only ever spent when a refund actually lands.
    """
    message = _nothing_to_show_cycle(user.id, 1)
    assert message.quota_refunded is True

    for _ in range(3):
        assert refund_nothing_to_show(message.id) is False   # already refunded

    assert db.session.scalar(
        db.select(User.nothing_to_show_refunds_used).where(User.id == user.id)) == 1
    assert get_remaining(user.id) == 3           # never 4


def test_cap_is_per_user(app, user):
    other = User()
    other.email = "capped_other@example.com"
    other.password = "T3st-secret*"
    other.is_verified = True
    db.session.add(other)
    db.session.commit()

    for i in range(3):
        _nothing_to_show_cycle(user.id, i)
    assert get_remaining(user.id) == 2           # first user is capped out

    fresh = _nothing_to_show_cycle(other.id, 1)
    assert fresh.quota_refunded is True          # second user unaffected
    assert get_remaining(other.id) == 3


def test_cap_is_configurable(app, user):
    app.config["NOTHING_TO_SHOW_REFUND_CAP"] = 1
    first = _nothing_to_show_cycle(user.id, 1)
    second = _nothing_to_show_cycle(user.id, 2)

    assert first.quota_refunded is True
    assert second.quota_refunded is False
    assert get_remaining(user.id) == 2


def test_cap_of_zero_refunds_nothing(app, user):
    app.config["NOTHING_TO_SHOW_REFUND_CAP"] = 0
    message = _nothing_to_show_cycle(user.id, 1)
    assert message.status == MessageStatus.FAILED
    assert message.quota_refunded is False
    assert get_remaining(user.id) == 2


def test_other_paths_ignore_the_cap(app, user):
    """
    A, B, C, D, F stay unconditional. Even with the nothing-to-show cap fully spent,
    an infrastructure failure still refunds.
    """
    for i in range(3):
        _nothing_to_show_cycle(user.id, i)       # burn the cap
    assert db.session.scalar(
        db.select(User.nothing_to_show_refunds_used).where(User.id == user.id)) == 2
    used_before = get_used(user.id)

    infra = consume_and_create_message(user.id, "will die to infrastructure")
    assert get_used(user.id) == used_before + 1

    assert refund_analysis(infra.id) is True     # uncapped path
    assert get_used(user.id) == used_before
    db.session.refresh(infra)
    assert infra.quota_refunded is True


def test_banner_copy_differs_for_refunded_and_capped(app, user):
    """The failed banner must report what actually happened, not the usual case."""
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(user.id)

    refunded = _nothing_to_show_cycle(user.id, 1)
    page = client.get(f"/messages/{refunded.id}")
    body = page.get_data(as_text=True)
    assert "didn't use one of your analyses" in body
    assert "used one of your analyses." not in body.replace("didn't use one of your analyses", "")

    _nothing_to_show_cycle(user.id, 2)
    capped = _nothing_to_show_cycle(user.id, 3)
    assert capped.quota_refunded is False
    page = client.get(f"/messages/{capped.id}")
    body = page.get_data(as_text=True)
    assert "didn&#39;t produce a result and used one of your analyses" in body \
        or "didn't produce a result and used one of your analyses" in body


def test_nothing_to_show_refund_is_idempotent(app, user):
    """
    If the helper's commit fails it re-raises and analyze_message's generic handler
    marks FAILED and refunds. The per-message marker means the slot can only ever
    come back once, whichever path got there first.
    """
    from app.services.workflow_service import _mark_failed_nothing_to_show

    message = consume_and_create_message(user.id, "unusable input")
    _mark_failed_nothing_to_show(message, "en")
    refund_analysis(message.id)                 # simulate the outer handler too

    assert get_remaining(user.id) == 3          # never 4
    assert get_used(user.id) == 0


def test_reaper_refunds_swept_messages(app, user):
    """Stuck PENDING past the cutoff → reaped → refunded."""
    from datetime import datetime, UTC, timedelta
    from app.celery_tasks import sweep_stuck_analyses, _ANALYSIS_STUCK_AFTER_SECONDS

    message = consume_and_create_message(user.id, "will get stuck")
    stale = datetime.now(UTC) - timedelta(seconds=_ANALYSIS_STUCK_AFTER_SECONDS + 60)
    db.session.execute(
        db.update(Message).where(Message.id == message.id).values(updated_at=stale)
    )
    db.session.commit()

    reaped = sweep_stuck_analyses()
    assert reaped == 1

    db.session.refresh(message)
    assert message.status == MessageStatus.FAILED
    assert message.quota_refunded is True
    assert get_remaining(user.id) == 3


def test_reaper_does_not_refund_completed_messages(app, user):
    """A message that finished before the sweep keeps its consumed slot."""
    from app.celery_tasks import sweep_stuck_analyses

    message = consume_and_create_message(user.id, "finishes fine")
    message.status = MessageStatus.COMPLETED
    db.session.commit()

    sweep_stuck_analyses()

    db.session.refresh(message)
    assert message.quota_refunded is False
    assert get_remaining(user.id) == 2


# ── display contract ──────────────────────────────────────────────────────────

def test_in_flight_analysis_shows_decremented_number(app, user):
    """During PENDING/PROCESSING the slot is already spent — show 2, not 3."""
    message = consume_and_create_message(user.id, "in flight")
    assert message.status == MessageStatus.PENDING
    assert get_remaining(user.id) == 2

    message.status = MessageStatus.PROCESSING
    db.session.commit()
    assert get_remaining(user.id) == 2


def test_remaining_never_negative_if_allowance_lowered(app, user):
    """Lowering the env var below a user's used-count must not render a negative."""
    for i in range(3):
        consume_and_create_message(user.id, f"msg {i}")
    app.config["ANALYSIS_LIFETIME_QUOTA"] = 1
    assert get_remaining(user.id) == 0
