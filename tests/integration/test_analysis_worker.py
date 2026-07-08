"""
Integration test: a live ML worker consumes the analysis task end-to-end and writes
a populated AnalysisResult.

WHY THIS IS AN INTEGRATION TEST
--------------------------------------------------------
The claim under test is owned by a separate, live worker process: the worker must
consume the id-based analyze_message task, run inside the Flask app context, RE-FETCH
the message, run the REAL ML classifier + the SEQUENTIAL enrichment pipeline, and
commit an AnalysisResult with the 5 enrichment fields populated (non-None). A unit
test or Celery eager mode would not prove the real worker does this. There is
deliberately NO task_always_eager.

TWO FAKE MODES (LLM_FAKE=1), env-selected with LLM_FAKE_MODE
--------------------------------------------------------
The fake is intentionally simple, not shape-aware. Only the extraction call is parsed
(via _safe_json_load); the five enrichment methods return _call's output verbatim.

  * degrade mode (default): _call echoes a non-JSON string. Extraction can't parse it →
    structured fields (company_name, role_title, …) are None, and _safe_json_load logs
    "JSON parsing failed: [fake-llm] You are a strict…". This WARNING is the designed
    graceful-degradation path, NOT a bug. Enrichments still populate (raw string).

  * success mode (LLM_FAKE_MODE=success): _call returns valid 7-key JSON for extraction
    (parses → structured fields populate) and canned prose for each enrichment.

This test observes BOTH paths live through a real worker. Each mode is one worker run;
the two test functions are gated by LLM_FAKE_MODE (set it the same in the worker shell
and the test shell), so exactly one runs per invocation and the other skips. The ML
worker must have trained models (run `python ml/train.py` if needed) — the success test
asserts the real classifier ran end to end.

DATABASE SAFETY
--------------------------------------------------------
This test writes one throwaway message row to the configured database and deletes it
during teardown. Do NOT point the test or the worker at a production database — use a
local development or isolated test database.

HOW TO RUN (from the project root)
--------------------------------------------------------
1) Start Redis:            redis-server

2) Success path — start the worker AND the test in success mode:
       --------------------------------------------------------------------------
                    |CELERY WORKER FILE|:
       --------------------------------------------------------------------------
       LOAD_MODELS=1 LLM_FAKE=1 LLM_FAKE_MODE=success .venv/bin/python -m celery \
           -A celery_worker.celery worker -Q ml --pool=solo --loglevel=info
       --------------------------------------------------------------------------
                        |TEST FiLE|:
       --------------------------------------------------------------------------
       LLM_FAKE_MODE=success .venv/bin/pytest -m integration \
           tests/integration/test_analysis_worker.py -v

3) Degradation path — repeat with both in degrade mode (the default):
       --------------------------------------------------------------------------
                    |CELERY WORKER FILE|:
       --------------------------------------------------------------------------
       LOAD_MODELS=1 LLM_FAKE=1 .venv/bin/python -m celery -A celery_worker.celery \
           worker -Q ml --pool=solo --loglevel=info
       --------------------------------------------------------------------------
                        |TEST FiLE|:
       --------------------------------------------------------------------------
       .venv/bin/pytest -m integration tests/integration/test_analysis_worker.py -v

If Redis is down, the worker is not ready, or it was not started with LLM_FAKE=1 on the
ml queue, the tests skip or fail fast with a message pointing back here.
"""

import os
import time
from uuid import uuid4

# This test's OWN create_app (used only to seed/read a message) skips the ML load.
os.environ.setdefault("LOAD_MODELS", "0")

import pytest  # noqa: E402
import redis  # noqa: E402
from celery import Celery  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

_TASK_NAME = "app.celery_tasks.analyze_message"

_SETUP_HINT = (
    "SKIPPED: this integration test needs a live Redis broker AND an ML worker on the "
    "'ml' queue started with LLM_FAKE=1 ({reason}). See this module's docstring: "
    "redis-server  +  LOAD_MODELS=1 LLM_FAKE=1 .venv/bin/python -m celery -A "
    "celery_worker.celery worker -Q ml --pool=solo"
)


@pytest.fixture(scope="module")
def seeded():
    """Seed one PENDING message via our own app; clean it up (and its rows) after."""
    load_dotenv()
    from app import create_app
    from app.extensions import db
    from app.models import User, Message, MessageStatus

    app = create_app()
    with app.app_context():
        user = User()
        user.email = f"analysis-worker-{uuid4().hex[:8]}@example.invalid"
        user.password = "throwaway-password"
        user.is_verified = True
        db.session.add(user)
        db.session.commit()

        message = Message(
            user_id=user.id,
            raw_text=(
                "Hi, we would like to invite you to a final onsite interview for the "
                "Software Engineer role at Google next Tuesday at 10am."
            ),
            status=MessageStatus.PENDING,
        )
        db.session.add(message)
        db.session.commit()

        info = {
            "message_id": message.id,
            "user_id": user.id,
            "broker": app.config["CELERY"]["broker_url"],
            "backend": app.config["CELERY"]["result_backend"],
        }
        try:
            yield info
        finally:
            row = db.session.get(User, info["user_id"])   # cascade deletes message/result
            if row is not None:
                db.session.delete(row)
                db.session.commit()


@pytest.fixture(scope="module")
def celery_client(seeded) -> Celery:
    """Bare client (no app) talking to the live worker over the configured broker."""
    return Celery("analysis_test_client", broker=seeded["broker"], backend=seeded["backend"])


@pytest.fixture
def require_live_worker(celery_client, seeded):
    """Pre-flight: skip/fail cleanly so a bad setup never hangs."""
    try:
        redis.from_url(celery_client.conf.broker_url, socket_connect_timeout=1).ping()
    except Exception:
        pytest.skip(_SETUP_HINT.format(reason="cannot reach the Redis broker"))

    if not celery_client.control.ping(timeout=2.0):
        pytest.skip(_SETUP_HINT.format(reason="Redis is up but no Celery worker replied"))

    registered = celery_client.control.inspect(timeout=3.0).registered() or {}
    known = {name for tasks in registered.values() for name in tasks}
    if registered and _TASK_NAME not in known:
        pytest.fail(
            f"task '{_TASK_NAME}' not registered on the worker — start it on the 'ml' "
            "queue from the project root so create_app imports app.celery_tasks."
        )


_FAKE_MODE = os.getenv("LLM_FAKE_MODE", "degrade").strip().lower()


def _enqueue_and_wait(celery_client, message_id):
    """
    Enqueue on the ml queue and poll until the message hits a terminal state.
    Returns (app, terminal_status); the app is reused for the assertion context.
    """
    from app import create_app
    from app.extensions import db
    from app.models import Message, MessageStatus

    celery_client.send_task(_TASK_NAME, args=[message_id], queue="ml")

    app = create_app()
    status = None
    deadline = time.time() + 30
    while time.time() < deadline:
        with app.app_context():
            msg = db.session.get(Message, message_id)
            status = msg.status if msg else None
        if status in (MessageStatus.COMPLETED, MessageStatus.FAILED):
            break
        time.sleep(0.5)
    return app, status


@pytest.mark.integration
@pytest.mark.skipif(_FAKE_MODE != "success",
                    reason="start BOTH the worker and this test with LLM_FAKE_MODE=success")
def test_worker_success_mode_populates_analysis(celery_client, seeded, require_live_worker):
    from app.extensions import db
    from app.models import Message, AnalysisResult, MessageStatus
    from app.services.workflow_service import TASK_RULES

    app, status = _enqueue_and_wait(celery_client, seeded["message_id"])
    assert status == MessageStatus.COMPLETED, (
        f"expected COMPLETED, got {status} — worker not on the 'ml' queue, or not "
        "started with LLM_FAKE=1 LLM_FAKE_MODE=success."
    )

    with app.app_context():
        msg = db.session.get(Message, seeded["message_id"])
        analysis = db.session.execute(
            db.select(AnalysisResult).where(
                AnalysisResult.message_id == seeded["message_id"])
        ).scalar_one_or_none()

        assert analysis is not None, "no AnalysisResult written"

        # Extraction SUCCESS: the valid-JSON success fake parsed → structured fields
        # populate (they have no other source; ML does not set them).
        assert analysis.company_name is not None
        assert analysis.role_title is not None

        # Enrichment SUCCESS: all five populated.
        for field in ("preparation_guidance", "suggested_questions",
                      "reply_suggestions", "role_summary", "archive_summary"):
            assert getattr(analysis, field), f"enrichment '{field}' not populated"

        # The REAL ML classifier ran end to end; task-generation wiring fires for a
        # task-generating category.
        assert analysis.message_category is not None
        if analysis.message_category in TASK_RULES:
            assert msg.tasks, "task-generating category but no tasks created"


@pytest.mark.integration
@pytest.mark.skipif(_FAKE_MODE != "degrade",
                    reason="start BOTH the worker and this test with LLM_FAKE_MODE=degrade (default)")
def test_worker_degrade_mode_handles_gracefully(celery_client, seeded, require_live_worker):
    from app.extensions import db
    from app.models import AnalysisResult, MessageStatus

    app, status = _enqueue_and_wait(celery_client, seeded["message_id"])

    # Terminal state reached, no crash — the pipeline handled the bad response.
    assert status == MessageStatus.COMPLETED, (
        f"expected COMPLETED, got {status} — worker not on the 'ml' queue, or not "
        "started with LLM_FAKE=1 (degrade mode)."
    )

    with app.app_context():
        analysis = db.session.execute(
            db.select(AnalysisResult).where(
                AnalysisResult.message_id == seeded["message_id"])
        ).scalar_one_or_none()

        assert analysis is not None, "no AnalysisResult written"

        # Graceful degradation: the non-JSON fake fails the extraction JSON parse, so the
        # structured fields are None (this is the source of the "JSON parsing failed"
        # WARNING — expected). The message still reaches COMPLETED.
        assert analysis.company_name is None
        assert analysis.role_title is None
