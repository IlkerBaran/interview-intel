"""
Integration Test: A live Celery worker builds and "sends" a verification email.

WHY THIS IS AN INTEGRATION TEST (not a unit test)
--------------------------------------------------------
The claim under test is owned by a separate, live worker process: the worker must
consume the id-based email task, run inside the Flask app context, RE-FETCH the
user from the database, GENERATE the verification token, RENDER the email template
inside the worker, and build an ABSOLUTE verification link using the configured
host and scheme.

This is the exact path that broke before because the worker has no request context.
A unit test, in-process call, or Celery eager mode would not prove that the real
worker can perform this flow correctly. So this test drives a real Redis broker
and a real Celery worker and asserts on what the worker reports back. There is
deliberately NO task_always_eager.

NO REAL EMAIL IS SENT
--------------------------------------------------------
The worker must run with MAIL_SUPPRESS_SEND=1. In that mode, the task does not call
the real email provider. Instead, it records a TOKEN-FREE summary to Redis for this
test to read.

The recorded summary may include:

* recipient
* subject
* URL scheme and host
* whether a token segment is present
* correlation id

It must never record the raw token or rendered HTML.

DATABASE SAFETY
--------------------------------------------------------
This test writes one throwaway user row to the configured database and deletes it
during teardown. Because it performs a real write, do NOT point this test or the
worker at a production database.
* Use a local development database or an isolated test database.

HOW TO RUN (follow word for word, from the project root)
--------------------------------------------------------
1) Start Redis in its own terminal:
        redis-server
   (install once if needed: `brew install redis`; verify: `redis-cli ping` -> PONG)

2) Start an email worker with sends suppressed and models off:
         LOAD_MODELS=0 MAIL_SUPPRESS_SEND=1 .venv/bin/python -m celery -A celery_worker.celery worker \
             --pool=solo --loglevel=info
   (The send_* tasks register via create_app's `from . import celery_tasks` import, so
   no --include is needed, unlike the session-teardown probe.)

   Confirm the worker prints "ready" before running the test.

3) Run THIS test in its own terminal:
        .venv/bin/pytest -m integration tests/integration/test_email_send.py -v

   `.venv/bin/pytest` uses pytest from your virtual environment.
   `-m integration` means run tests marked as integration.
   `tests/integration/test_email_send.py` is the file to run.
   `-v` means verbose output.

If Redis is not running, the worker is not ready, or MAIL_SUPPRESS_SEND=1 was not
set on the worker, this test skips or fails fast with a message pointing back to
these steps instead of hanging on: a raw broker timeout.

ONE-TIME PREREQUISITES (not created by this file):
  - pip install pytest
  - install and run Redis
  - register the `integration` marker in pytest.ini
  - exclude integration tests from the default fast test suite
  - add tests/**init**.py and tests/integration/**init**.py if integration tests
    need to be importable by dotted path
  """

import json
import os
import time
from uuid import uuid4

# This test's OWN create_app (used only to seed/cleanup a user) must skip the ML load.
os.environ.setdefault("LOAD_MODELS", "0")

import pytest  # noqa: E402
import redis  # noqa: E402
from celery import Celery  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

_TASK_NAME = "app.celery_tasks.send_verification_email"
_SINK_KEY = "test:email_sink"
# Exactly the token-free fields the worker is allowed to record. Anything else in the
# sink means something leaked.
_EXPECTED_KEYS = {"kind", "to", "subject", "scheme", "host", "has_token_segment", "idempotency_key"}

_SETUP_HINT = (
    "SKIPPED: this integration test needs a live Redis broker AND a Celery worker "
    "started with MAIL_SUPPRESS_SEND=1 ({reason}). See this module's docstring: "
    "redis-server  +  LOAD_MODELS=0 MAIL_SUPPRESS_SEND=1 .venv/bin/python -m celery -A "
    "celery_worker.celery worker --pool=solo"
)


@pytest.fixture(scope="module")
def seeded():
    """Seed one unverified user (and capture expected host/scheme) via our own app; clean up after."""
    load_dotenv()  # FLASK_ENV / DATABASE_URL / SERVER_NAME (won't override LOAD_MODELS=0)
    from app import create_app
    from app.extensions import db
    from app.models import User

    app = create_app()
    with app.app_context():
        user = User()
        user.email = f"stage2-email-{uuid4().hex[:8]}@example.invalid"
        user.password = "throwaway-password"
        user.is_verified = False
        db.session.add(user)
        db.session.commit()
        info = {
            "user_id": user.id,
            "email": user.email,
            "scheme": app.config["PREFERRED_URL_SCHEME"],
            "host": app.config["SERVER_NAME"],
            "broker": app.config["CELERY"]["broker_url"],
            "backend": app.config["CELERY"]["result_backend"],
        }
        try:
            yield info
        finally:
            row = db.session.get(User, info["user_id"])
            if row is not None:            # may already be gone on a re-run; don't crash cleanup
                db.session.delete(row)
                db.session.commit()


@pytest.fixture(scope="module")
def celery_client(seeded) -> Celery:
    """Bare client (no app) talking to the live worker over the configured broker/backend."""
    return Celery("email_send_test_client", broker=seeded["broker"], backend=seeded["backend"])


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
            f"task '{_TASK_NAME}' not registered on the worker — start it from the "
            "project root so create_app imports app.celery_tasks."
        )


@pytest.mark.integration
def test_worker_builds_and_records_verification_email(celery_client, seeded, require_live_worker):
    backend = redis.from_url(seeded["backend"])
    backend.delete(_SINK_KEY)  # isolate this run

    key = uuid4().hex
    celery_client.send_task(_TASK_NAME, args=[seeded["user_id"], key])

    # Poll the sink the worker writes under MAIL_SUPPRESS_SEND.
    record = None
    deadline = time.time() + 15
    while time.time() < deadline:
        raw = backend.lpop(_SINK_KEY)
        if raw:
            record = json.loads(raw)
            break
        time.sleep(0.25)

    assert record is not None, (
        "no sink record — worker not consuming, or it was not started with "
        "MAIL_SUPPRESS_SEND=1 (it would have tried a real send instead)."
    )

    # (1) EXACTLY the token-free fields, nothing extra. This is the real guarantee that no
    #     token / html / secret rode along under ANY key — it supersedes the old vacuous
    #     check (which was always true because "has_token_segment" is always a key).
    assert set(record.keys()) == _EXPECTED_KEYS, (
        f"sink record keys {set(record.keys())} != expected {_EXPECTED_KEYS} — "
        "an unexpected field leaked into the record."
    )

    # (2) The worker built an absolute link with the CONFIGURED host+scheme. A wrong or
    #     typo'd SERVER_NAME fails HERE (in testing), never in a real user's inbox.
    assert record["to"] == seeded["email"]
    assert record["scheme"] in {"http", "https"}
    assert record["scheme"] == seeded["scheme"]
    assert record["host"], "worker built a URL with an empty host (SERVER_NAME unset on the worker?)"
    assert record["host"] == seeded["host"]
    assert record["has_token_segment"] is True  # a token path segment was present in the URL
    assert record["idempotency_key"] == key

    # (3) Token-free by construction. NOTE: the test never possesses the raw token — it is
    #     generated inside the worker and only its hash is stored, so we cannot compare the
    #     literal token value here. The exact-key set in (1) is the actual proof that no
    #     token field exists; as defense-in-depth, assert no "token" marker hides inside any
    #     string VALUE. Join values only (not keys) so the "has_token_segment" KEY can't
    #     trip the check.
    joined_values = " ".join(str(v) for v in record.values()).lower()
    assert "token" not in joined_values
