"""
quota_service.py

Per-user LIFETIME analysis quota.

Model
------
The `users` table stores the number of analyses a user has **used**, not
the number they have remaining. Storing a `remaining` value would require a
database default such as `server_default='3'`, which would hard-code the quota
in the schema. If `ANALYSIS_LIFETIME_QUOTA` were later changed, the database
default could become inconsistent with the application configuration.

By storing only the number of analyses used, the quota remains defined in one place:
the application config. The number of analyses remaining is calculated at read time as:
`remaining = configured allowance - analyses used`

Consuming
---------
A slot is consumed with one conditional UPDATE:
        UPDATE users SET analyses_used = analyses_used + 1
        WHERE id = :uid AND analyses_used < :allowance

rowcount == 1 IS the permission. Two concurrent submissions cannot both take the
last slot: the database serialises the two UPDATEs and the loser's WHERE clause
no longer matches, so it gets rowcount == 0. A SELECT-then-UPDATE would let both
read `used = 2` and both write `used = 3`.

The consume runs in the SAME transaction as the message INSERT (one commit), so
there is never a message without a consumed slot, nor a consumed slot without a
message.

Refunding
---------
Refunds must survive a redelivery: analyze_message runs with acks_late=True, so
a worker crash can re-run a task that already refunded. Two guards:

    1. A per-message marker (messages.quota_refunded) flipped by its own
       conditional UPDATE — rowcount == 1 is permission to refund. A second
       attempt matches zero rows and stops.
    2. A clamp on the decrement (`WHERE analyses_used > 0`), so the counter can
       never go negative and remaining can never exceed the allowance.

Both run inside one transaction, so a crash between them cannot leave the marker
set without the decrement.

Capping the "nothing to show" path
----------------------------------
Five of the six FAILED paths refund unconditionally. The sixth —
_mark_failed_nothing_to_show — still costs one billed extraction call, so
refunding it forever would let someone loop on garbage input and reset their quota
indefinitely. refund_nothing_to_show() therefore spends from a separate per-user
budget (users.nothing_to_show_refunds_used, capped by NOTHING_TO_SHOW_REFUND_CAP)
before it is allowed to refund. Kept as its own column because it counts refund
ELIGIBILITY, not usage — folding it into analyses_used would break the
remaining = allowance - used derivation.
"""

import logging

from flask import current_app

from app.extensions import db
from app.models import Message, MessageStatus, User


logger = logging.getLogger(__name__)

DEFAULT_LIFETIME_QUOTA = 3
DEFAULT_NOTHING_TO_SHOW_REFUND_CAP = 2


def get_allowance() -> int:
    """Lifetime analyses allowed per user. Config-owned, never stored in the schema."""
    return int(current_app.config.get("ANALYSIS_LIFETIME_QUOTA", DEFAULT_LIFETIME_QUOTA))


def get_nothing_to_show_cap() -> int:
    """How many nothing-to-show results a user may have refunded. Config-owned."""
    return int(current_app.config.get("NOTHING_TO_SHOW_REFUND_CAP", DEFAULT_NOTHING_TO_SHOW_REFUND_CAP))


def get_used(user_id: int) -> int:
    """Analyses consumed so far. 0 if the user is gone."""
    used = db.session.scalar(
        db.select(User.analyses_used).where(User.id == user_id)
    )
    return int(used or 0)


def get_remaining(user_id: int) -> int:
    """
    Return how many analyses the user has left.
    The result cannot be less than 0 or greater than the allowed amount.
    """
    allowance = get_allowance()
    used = get_used(user_id)
    remaining = allowance - used

    return max(0, min(allowance, remaining))


def consume_and_create_message(
        user_id: int,
        raw_text: str,
        subject: str | None = None,
        sender_email: str | None = None
) -> Message | None:
    """
    Atomically consume one analysis and save the message in a single transaction.

    Returns the Message on success, or None when the user is out of quota — the
    conditional UPDATE matched no row, so nothing was inserted. Callers branch on
    that None (see routes/messages.py) to show the quota-exhausted message.

    Database errors are raised so the route's existing error handling still works.

    The message INSERT is done here rather than via a separate save helper, because
    the insert and the counter increment must share one commit.
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("raw_text cannot be empty")

    allowance = get_allowance()

    try:
        # Check quota and increment usage in one database operation.
        result = db.session.execute(
            db.update(User).where(
                User.id == user_id,
                User.analyses_used < allowance
            )
            .values(analyses_used=User.analyses_used + 1)
        )

        # Out of quota (or user vanished). Roll back so the caller's session
        # is clean; nothing was inserted.
        if (result.rowcount or 0) != 1:
            db.session.rollback()
            logger.info(
                "quota: refused analysis for user_id=%s (allowance=%s exhausted)",
                user_id, allowance,
                extra={
                    "user_id": user_id,
                    "allowance": allowance
                }
            )
            return None

        message = Message(
            user_id=user_id,
            raw_text=raw_text,
            subject=subject,
            sender_email=sender_email,
            status=MessageStatus.PENDING,
        )
        db.session.add(message)

        # Single commit: the INSERT and the increment land together or not at all.
        db.session.commit()

        logger.info(
            "quota: consumed 1 for user_id=%s message_id=%s",
            user_id, message.id,
            extra={
                "user_id": user_id,
                "message_id": message.id
            }
        )
        return message

    except Exception as e:
        db.session.rollback()
        logger.error(
            "quota: consume+insert failed user_id=%s error_type=%s",
            user_id,
            type(e).__name__,
            extra={
                "user_id": user_id,
                "error_type": type(e).__name__
            }
        )
        raise


def refund_analysis(message_id: int) -> bool:
    """
    Refund one analysis used by a message.

    Used by paths A, B, C, D and F — the unconditional ones. Path E goes through
    refund_nothing_to_show() instead, which is capped.

    Safe to call more than once (acks_late redelivery) and safe to call from a
    failure handler: never raises, so it can never convert a handled failure into
    an unhandled one, and never prevents a message reaching its terminal state.

    Ordering note: callers run this after mark_analysis_failed() so the terminal
    state is settled before the compensating action. That is a readability choice,
    not a correctness requirement — this function commits, so there is nothing left
    for mark_analysis_failed()'s opening rollback to discard. Both orders were
    verified to behave identically, including with an uncommitted write pending.
    """
    try:
        # Rollback first for the same reason mark_analysis_failed() does: a soft
        # time limit or DB error may have left a half-open transaction that would
        # otherwise poison this write.
        db.session.rollback()

        message = db.session.get(Message, message_id)
        if message is None:
            logger.warning(
                "quota refund: message_id=%s not found",
                message_id,
                extra={
                    "message_id": message_id
                }
            )
            return False

        user_id = message.user_id

        # Guard 1 — claim the refund. rowcount == 1 is permission; a redelivered
        # task finds quota_refunded already true and matches zero rows
        claim = db.session.execute(
            db.update(Message).where(
                Message.id == message_id,
                Message.quota_refunded.is_(False)
            )
            .values(quota_refunded=True)
        )

        # Already refunded or message no longer matched.
        if (claim.rowcount or 0) != 1:
            db.session.rollback()
            logger.info(
                "quota refund: message_id=%s already refunded; skipping",
                message_id,
                extra={
                    "message_id": message_id
                }
            )
            return False

        # Guard 2 - actually return one analysis to the user.
        # Cannot go below zero, so remaining can never exceed
        # the allowance even if the marker were somehow wrong.
        decrement = db.session.execute(
            db.update(User).where(
                User.id == user_id,
                User.analyses_used > 0
            )
            .values(analyses_used=User.analyses_used - 1)
        )

        # If nothing was decremented, undo the quota_refunded marker too.
        if (decrement.rowcount or 0) != 1:
            db.session.rollback()
            logger.warning(
                "quota refund: could not decrement usage for user_id=%s message_id=%s",
                user_id, message_id,
                extra={
                    "user_id": user_id,
                    "message_id": message_id
                }
            )
            return False

        # Marker and decrement commit together.
        db.session.commit()

        logger.info(
            "quota refund: returned 1 to user_id=%s for message_id=%s",
            user_id, message_id,
            extra={
                "user_id": user_id,
                "message_id": message_id
            }
        )
        return True

    except Exception as e:
        db.session.rollback()
        logger.error(
            "quota refund: failed message_id=%s error_type=%s",
            message_id,
            type(e).__name__,
            extra={
                "message_id": message_id,
                "error_type": type(e).__name__
            }
        )
        return False


def refund_nothing_to_show(message_id: int) -> bool:
    """
    Refund a "nothing to show" analysis only if the user still has refund budget
    available.

    Returns True only when the refund is actually completed. Returns False if the
    refund cap has been reached, the message no longer exists, the message was
    already refunded, or the user's analysis counter cannot be decremented.

    This function is used only by
    workflow_service._mark_failed_nothing_to_show(). Other failure paths use
    refund_analysis() and are not affected by the nothing-to-show refund cap.

    The order of the database updates is intentional:

        1. Spend one nothing-to-show refund from the user's refund budget using a
           conditional UPDATE. rowcount == 1 means the user was still below the cap
           and this refund is allowed. Because the check and increment happen in the
           same UPDATE, concurrent requests cannot both pass the cap check using a
           stale value.

        2. Claim the message's quota_refunded marker. This makes the operation
           idempotent, so the same message cannot receive the refund more than once.

        3. Decrement analyses_used by one, but only if the counter is greater than
           zero. The decrement must affect exactly one row for the refund to succeed.

    All three updates are part of the same transaction. If any step fails or does
    not affect the expected row, the transaction is rolled back. This prevents the
    refund budget from being spent, or quota_refunded from being set, unless the
    analysis slot is actually returned.

    For example, if a task is delivered again after the message was already
    refunded, the budget update may initially succeed, but the quota_refunded claim
    will match no row. The rollback then undoes the budget increment, so duplicate
    task delivery does not consume additional refund budget.

    If the refund cap has already been reached, no refund-related state is changed
    and quota_refunded remains False. This allows the UI to correctly show that the
    failed analysis still consumed a quota slot.

    This function never raises an exception to its caller. It is used inside a
    failure-handling path, so database errors are rolled back and reported as False
    rather than turning an already handled analysis failure into another unhandled
    failure.
    """
    try:
        # Same reason as refund_analysis(): clear any half-open transaction first.
        db.session.rollback()

        message = db.session.get(Message, message_id)
        if message is None:
            logger.warning(
                "nothing-to-show refund: message_id=%s not found",
                message_id,
                extra={
                    "message_id": message_id
                }
            )
            return False

        user_id = message.user_id
        cap = get_nothing_to_show_cap()

        # Step 1: spend one nothing-to-show refund.
        budget = db.session.execute(
            db.update(User) .where(
                User.id == user_id,
                User.nothing_to_show_refunds_used < cap
            )
            .values(nothing_to_show_refunds_used=User.nothing_to_show_refunds_used + 1)
        )

        # Cap reached or user no longer exists.
        if (budget.rowcount or 0) != 1:
            db.session.rollback()
            logger.info(
                "nothing-to-show refund: cap of %s reached for user_id=%s — message_id=%s keeps its slot",
                cap,
                user_id,
                message_id,
                extra={
                    "user_id": user_id,
                    "message_id": message_id,
                    "cap": cap
                }
            )
            return False

        # Step 2: claim this message's refund.
        claim = db.session.execute(
            db.update(Message).where(
                Message.id == message_id,
                Message.quota_refunded.is_(False)
            )
            .values(quota_refunded=True)
        )

        # Already refunded (acks_late redelivery). Roll back so step 1's
        # increment is undone — budget must not be spent without a refund.
        if (claim.rowcount or 0) != 1:
            db.session.rollback()
            logger.info(
                "nothing-to-show refund: message_id=%s already refunded; cap budget not spent",
                message_id,
                extra={
                    "message_id": message_id
                }
            )
            return False

        # Step 3: actually return one analysis.
        decrement = db.session.execute(
            db.update(User).where(
                User.id == user_id,
                User.analyses_used > 0
            )
            .values(analyses_used=User.analyses_used - 1)
        )

        # If no analysis was returned, undo steps 1 and 2 as well.
        if (decrement.rowcount or 0) != 1:
            db.session.rollback()

            logger.warning(
                "nothing-to-show refund: could not decrement usage "
                "for user_id=%s message_id=%s; refund rolled back",
                user_id,
                message_id,
                extra={
                    "user_id": user_id,
                    "message_id": message_id,
                },
            )
            return False

        # All three changes commit together.
        db.session.commit()

        logger.info(
            "nothing-to-show refund: returned 1 to user_id=%s for message_id=%s",
            user_id,
            message_id,
            extra={
                "user_id": user_id,
                "message_id": message_id
            }
        )
        return True

    except Exception as e:
        db.session.rollback()
        logger.error(
            "nothing-to-show refund: failed message_id=%s error_type=%s",
            message_id,
            type(e).__name__,
            extra={
                "message_id": message_id,
                "error_type": type(e).__name__
            }
        )
        return False
