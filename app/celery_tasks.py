"""
celery_tasks.py

This file contains Celery background tasks.

--> Stage 2 migrates transactional auth emails off daemon threads onto Celery.
These tasks follow the queue system like celery, recorded in ADR-0006: the route
passes an id; the WORKER re-fetches the user, re-authorizes, GENERATES the token,
renders the link, and sends. A raw, usable token therefore never enters the broker.
The only args on the wire are (user_id, idempotency_key), both non-secret.

Delivery semantics (ADR-0007): acks_late=True (a worker crash redelivers a real
user's email rather than dropping it) + limited autoretry on transient send errors.
We do NOT use a send-level idempotency/dedup key: each attempt regenerates the token
(only the hash is stored, so a retry cannot reuse the prior raw token), so suppressing
the retry's email would strand the only valid link. Posture is therefore
"latest-token-wins": each attempt's email is internally consistent, earlier links go
stale and fail closed into the existing "request a new link" UX. The uuid4 key is kept
purely as a correlation/tracing id across attempts.

--> Stage 3 runs message analysis in the background through Celery. The route saves
a Message as PENDING and enqueues analyze_message(message_id). The worker
re-fetches the message, skips it if analysis already finished, marks it
PROCESSING, runs the workflow_service pipeline, retries transient LLM failures,
and makes sure handled failures end in a terminal FAILED state.

The task functions in this file should stay thin. They coordinate Celery,
database state, retries, and logging. Business logic should stay in service
modules such as auth_service, email_service, llm_service, and workflow_service.
"""

import logging
import os
import random
from datetime import datetime, UTC, timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from flask import current_app, render_template, url_for
from resend.exceptions import (
      InvalidApiKeyError,
      MissingApiKeyError,
      MissingRequiredFieldsError,
      ResendError,
      ValidationError
)

from app.extensions import db
from app.models import User, Message, MessageStatus
from app.services.auth_service import (
    generate_verification_token,
    generate_password_reset_token
)
from app.services.email_service import _send_via_resend
from app.services.llm_service import TRANSIENT_LLM_ERRORS


logger = logging.getLogger(__name__)


# Test-support sink for MAIL_SUPPRESS_SEND=True.
#
# This is not a Python list. It is the Redis key/name for a Redis list.
# When the worker runs an email task in test mode, _record_suppressed_send()
# pushes a small safe record here instead of sending a real email.
# The live-worker integration test reads this Redis list to confirm the
# worker completed the task end-to-end.
_EMAIL_SINK_KEY = "test:email_sink"

# Resend errors that should NOT be retried.
#
# These usually mean our email data or API key/config is wrong.
# Retrying the Celery task will not fix them, so we log the error and stop.
# Other ResendError types are treated as temporary and can be retried by Celery.
_PERMANENT_SEND_ERRORS = (
    ValidationError,
    MissingRequiredFieldsError,
    MissingApiKeyError,
    InvalidApiKeyError,
)

# Shared Celery options for both email tasks.
# These prevent silent job loss, avoid storing task results, and retry temporary
# Resend/network failures with safe backoff instead of retrying forever.
_EMAIL_TASK_OPTS = dict(
    bind=True,
    acks_late=True,                 # mark done only after worker finishes
    ignore_result=True,             # do not store email task results in Redis /1
    autoretry_for=(ResendError,),   # retry temporary Resend/network errors
    retry_backoff=True,             # wait longer between retries
    retry_backoff_max=300,          # max retry wait: 5 minutes
    retry_jitter=True,              # spread retries out randomly
    max_retries=3,                  # original try + up to 3 retries
)


@shared_task(name="app.celery_tasks.ping")
def ping() -> str:
    """
    Dummy task: log, return a tiny non-sensitive result. No args, no DB.
    """
    logger.info("pong - ping task executed successfully by worker")

    return "pong"


def _record_suppressed_send(
        kind: str,
        recipient: str,
        subject: str,
        url: str,
        idempotency_key: str
) -> None:
    """
    Test-support ONLY. When MAIL_SUPPRESS_SEND is set, email tasks record a
    TOKEN-FREE summary to Redis instead of calling Resend, so the live-worker
    integration test can assert the worker executed the task end-to-end (rendered
    in-worker, built an absolute URL with the configured host/scheme) without
    sending a real email. Inert in normal operation (MAIL_SUPPRESS_SEND=False).

    The record never contains the raw token or the rendered HTML — only the
    recipient, subject, the URL's scheme+host, a boolean that a token path segment
    exists, and the correlation id.
    """
    import json
    from urllib.parse import urlsplit

    import redis as redis_lib

    parts = urlsplit(url)
    record = {
        "kind": kind,
        "to": recipient,
        "subject": subject,
        "scheme": parts.scheme,
        "host": parts.netloc,
        # last path segment is the token; store only whether it is present, not its value
        "has_token_segment": bool(parts.path.rsplit("/", 1)[-1]),
        "idempotency_key": idempotency_key,
    }

    client = redis_lib.from_url(current_app.config["CELERY"]["result_backend"])
    client.rpush(_EMAIL_SINK_KEY, json.dumps(record))
    client.expire(_EMAIL_SINK_KEY, 300)


@shared_task(name="app.celery_tasks.send_verification_email", **_EMAIL_TASK_OPTS)
def send_verification_email(self, user_id: int, idempotency_key: str) -> None:
    """
    Send an email-verification link from inside the Celery worker.

    The route passes only user_id + idempotency_key. This task re-fetches the user,
    checks the user still needs verification, generates and saves a fresh token,
    builds the confirmation URL, and sends the email.

    If MAIL_SUPPRESS_SEND=True, it records a safe test entry in Redis instead of
    sending through Resend.
    """
    user = db.session.get(User, user_id)
    if user is None:
        logger.warning(
            "send_verification_email: user_id=%s not found; skipping",
            user_id,
            extra={
                "user_id": user_id,
                "idempotency_key": idempotency_key
            }
        )
        return
    if user.is_verified:
        logger.info(
            "send_verification_email: user_id=%s already verified; skipping",
            user_id,
            extra={
                "user_id": user_id,
                "idempotency_key": idempotency_key
            }
        )
        return

    # Generate token (hashed at rest) + expiry, then commit before building the link.
    try:
        raw_token = generate_verification_token(user)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "send_verification_email: failed to persist token user_id=%s error_type=%s",
            user_id, type(e).__name__,
            extra={
                "user_id": user_id,
                "error_type": type(e).__name__
            }
        )
        return

    confirmation_url = url_for("auth.verify_email", token=raw_token, _external=True)
    params = {
        "to": [user.email],
        "subject": "Confirm your Interview Intel account",
        "html": render_template("email/confirmation.html", confirmation_url=confirmation_url),
        "text": f"Confirm your email by visiting: {confirmation_url}",
    }

    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        _record_suppressed_send("verification", user.email, params["subject"],
                                confirmation_url, idempotency_key)
        return

    try:
        _send_via_resend(params)
    except _PERMANENT_SEND_ERRORS as exc:
        logger.error(
            "send_verification_email: permanent send failure user_id=%s error_type=%s "
            "code=%s; not retrying",
            user_id, type(exc).__name__, getattr(exc, "code", None),
            extra={
                "user_id": user_id,
                "idempotency_key": idempotency_key
            }
        )
        return
    # Transient ResendError (incl. wrapped network errors) propagates → autoretry_for.
    logger.info(
        "Verification email sent user_id=%s idempotency_key=%s",
        user_id, idempotency_key,
        extra={
            "user_id": user_id,
            "idempotency_key": idempotency_key
        }
    )


@shared_task(name="app.celery_tasks.send_password_reset_email", **_EMAIL_TASK_OPTS)
def send_password_reset_email(self, user_id: int, idempotency_key: str) -> None:
    """
    Build and send a password-reset link. Token is generated HERE, in the worker.
    Re-authorize: skip if the user no longer exists. (Matches the existing
    forgot-password flow, which sends to any found account regardless of verified.)
    """
    user = db.session.get(User, user_id)
    if user is None:
        logger.warning(
            "send_password_reset_email: user_id=%s not found; skipping",
            user_id,
            extra={
                "user_id": user_id,
                "idempotency_key": idempotency_key
            }
        )
        return

    try:
        raw_token = generate_password_reset_token(user)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "send_password_reset_email: failed to persist token user_id=%s error_type=%s",
            user_id, type(e).__name__,
            extra={
                "user_id": user_id,
                "error_type": type(e).__name__
            }
        )
        return

    reset_url = url_for("auth.reset_password", token=raw_token, _external=True)
    params = {
        "to": [user.email],
        "subject": "Reset your Interview Intel password",
        "html": render_template("email/password_reset.html", reset_url=reset_url),
        "text": f"Reset your password by visiting: {reset_url}",
    }

    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        _record_suppressed_send("password_reset", user.email, params["subject"],
                                reset_url, idempotency_key)
        return

    try:
        _send_via_resend(params)
    except _PERMANENT_SEND_ERRORS as exc:
        logger.error(
            "send_password_reset_email: permanent send failure user_id=%s error_type=%s "
            "code=%s; not retrying",
            user_id, type(exc).__name__, getattr(exc, "code", None),
            extra={
                "user_id": user_id,
                "idempotency_key": idempotency_key
            }
        )
        return
    logger.info(
        "Password reset email sent user_id=%s idempotency_key=%s",
        user_id, idempotency_key,
        extra={
            "user_id": user_id,
            "idempotency_key": idempotency_key
        }
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3 — message analysis on the dedicated ML queue
# ═══════════════════════════════════════════════════════════════════════════════

# Time limits are enforced on the prefork pool (production Linux). Under --pool=solo
# (macOS dev) they are NOT enforced — the per-call Anthropic timeouts (llm_service)
# are the pool-independent hang guard; these are the prod backstop.
_ANALYSIS_SOFT_TIME_LIMIT = int(os.getenv("ANALYSIS_SOFT_TIME_LIMIT", "240"))  # 4 min
_ANALYSIS_HARD_TIME_LIMIT = int(os.getenv("ANALYSIS_HARD_TIME_LIMIT", "300"))  # 5 min

# How long a message may remain PENDING/PROCESSING before the reaper marks it FAILED.
#
# This must be longer than a legitimate analysis run can take:
#   hard time limit + retry delays + safety margin
#
# That prevents the sweeper from marking real in-progress work as FAILED.
_ANALYSIS_STUCK_AFTER_SECONDS = int(os.getenv("ANALYSIS_STUCK_AFTER_SECONDS", "900"))  # 15 min

# Retry delay for temporary LLM failures.
#
# TRANSIENT_LLM_ERRORS covers cases like provider 529/Overloaded or 429/rate-limit.
# Retrying immediately would make those problems worse, so retries wait and back off.
_ANALYSIS_RETRY_BASE_SECONDS = 15
_ANALYSIS_RETRY_MAX_SECONDS = 300

# No autoretry_for: retries are driven manually so EXHAUSTION marks the message
# FAILED (not a bare Celery FAILURE that leaves it stuck PROCESSING). The transient
# CLASSIFICATION (TRANSIENT_LLM_ERRORS, incl. 529/OverloadedError) is unchanged.
_ANALYSIS_TASK_OPTS = dict(
    bind=True,
    acks_late=True,                     # worker crash → redeliver, not drop
    ignore_result=True,                 # do not store task return values in Redis
    max_retries=3,                      # 1 original + 3 retries, then terminal FAILED
    soft_time_limit=_ANALYSIS_SOFT_TIME_LIMIT,
    time_limit=_ANALYSIS_HARD_TIME_LIMIT,
)


def _analysis_retry_countdown(retries: int) -> int:
    """
    Return the delay before the next transient-error retry.

    Retry delays are approximately:
        15s, 30s, 60s

    A small 0–20% jitter is added so many failed tasks do not retry at the exact
    same moment. The countdown is handled by the broker between attempts, so this
    waiting time does not count against the task's execution time limit.
    """
    base = min(_ANALYSIS_RETRY_BASE_SECONDS * (2 ** retries), _ANALYSIS_RETRY_MAX_SECONDS)
    return int(base + random.uniform(0, base * 0.2))


@shared_task(name="app.celery_tasks.analyze_message", **_ANALYSIS_TASK_OPTS)
def analyze_message(self, message_id: int) -> None:
    """
    Run the analysis pipeline on the dedicated ML queue (LOAD_MODELS=1, --pool=solo
    on macOS). Re-fetch + re-authorize by id, skip-if-done guard, mark PROCESSING,
    then run the sequential pipeline. Every failure path reaches a terminal state.
    """
    from app.services.workflow_service import (
        run_message_analysis, analysis_already_done, mark_analysis_failed,
    )

    # Pass IDs through Celery, then re-fetch ORM objects inside the worker.
    # This avoids sending database objects through Redis.
    message = db.session.get(Message, message_id)
    if message is None:
        logger.warning(
            "analyze_message: message_id=%s not found; skipping", message_id,
            extra={"message_id": message_id}
        )
        return

    # Idempotency guard.
    #
    # If Redis redelivers the task after a worker crash, or the task is accidentally
    # enqueued twice, do not analyze the same message again if it is already done.
    if analysis_already_done(message):
        logger.info(
            "analyze_message: message_id=%s already analyzed; skipping", message_id,
            extra={"message_id": message_id}
        )
        return

    # Async UX:
    # The route saves the message as PENDING.
    # The worker flips it to PROCESSING when actual background work begins.
    if message.status != MessageStatus.PROCESSING:
        message.status = MessageStatus.PROCESSING
        db.session.commit()

    try:
        run_message_analysis(message)

    except SoftTimeLimitExceeded:
        # Soft limit → Mark the database row FAILED instead of leaving it stuck PROCESSING.
        logger.error(
            "analyze_message: soft time limit (%ss) exceeded message_id=%s — marking FAILED",
            _ANALYSIS_SOFT_TIME_LIMIT, message_id,
            extra={"message_id": message_id},
        )
        mark_analysis_failed(message_id)
        return

    except TRANSIENT_LLM_ERRORS as exc:
        # Temporary provider problem, such as rate limit or overload.
        # Retry with backoff while retries remain.
        if self.request.retries >= self.max_retries:
            # Retries exhausted → End in a database terminal state.
            logger.error(
                "analyze_message: transient retries exhausted message_id=%s error_type=%s — marking FAILED",
                message_id, type(exc).__name__,
                extra={
                    "message_id": message_id,
                    "error_type": type(exc).__name__
                },
            )
            mark_analysis_failed(message_id)
            return
        countdown = _analysis_retry_countdown(self.request.retries)   # ~15s / ~30s / ~60s
        logger.warning(
            "analyze_message: transient error message_id=%s error_type=%s — retry %s/%s in ~%ss",
            message_id, type(exc).__name__, self.request.retries + 1, self.max_retries, countdown,
            extra={
                "message_id": message_id,
                "error_type": type(exc).__name__
            },
        )
        raise self.retry(exc=exc, countdown=countdown)

    except Exception as e:
        # Permanent / unexpected → terminal FAILED, no retry.
        logger.error(
            "analyze_message failed permanently message_id=%s error_type=%s",
            message_id, type(e).__name__,
            extra={
                "message_id": message_id,
                "error_type": type(e).__name__
            },
        )
        mark_analysis_failed(message_id)
        return


@shared_task(name="app.celery_tasks.sweep_stuck_analyses", acks_late=True, ignore_result=True)
def sweep_stuck_analyses() -> int:
    """
    Backstop reaper (Celery beat): marks any message stuck in a non-terminal state
    (PENDING/PROCESSING) longer than ANALYSIS_STUCK_AFTER_SECONDS as FAILED. Covers
    failures that in-task cleanup cannot reliably handle: hard-kill SIGKILL, OOM,
    machine death, or messages enqueued when no worker is available.

    Race-safe: uses one atomic UPDATE gated on BOTH status (still non-terminal) and
    staleness, so a task that just flipped to COMPLETED is never clobbered. DB-only →
    default queue (served by the LOAD_MODELS=0 email worker). Returns the count reaped.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=_ANALYSIS_STUCK_AFTER_SECONDS)

    try:
        result = db.session.execute(
            db.update(Message)
            .where(
                Message.status.in_([MessageStatus.PENDING, MessageStatus.PROCESSING]),
                Message.updated_at < cutoff,
            )
            .values(status=MessageStatus.FAILED)
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(
            "sweep_stuck_analyses: sweep failed error_type=%s", type(e).__name__,
            extra={"error_type": type(e).__name__},
        )
        return 0

    count = result.rowcount or 0
    if count:
        logger.warning(
            "sweep_stuck_analyses: marked %d stuck message(s) FAILED (older than %ss)",
            count, _ANALYSIS_STUCK_AFTER_SECONDS,
            extra={
                "count": count,
                "stuck_after_seconds": _ANALYSIS_STUCK_AFTER_SECONDS
            },
        )
    return count
