"""
Integration Test: The Celery worker removes its DB session after every task.

WHY THIS IS AN INTEGRATION TEST (not a unit test)
-------------------------------------------------
The claim under test is a property of a (separate, live worker process): every
task runs inside `with app.app_context()` (see app/celery_app.py FlaskTask), and
popping that context fires Flask-SQLAlchemy's teardown -> db.session.remove(),
which returns the connection to the pool and resets the session before the next
task runs. That cleanup happens inside the worker, ACROSS task boundaries — it
cannot be observed in-process, and it cannot be observed in Celery eager mode.
Eager mode does not test how the real worker creates and destroys the Flask app context,
for each task. So this test drives a real Redis broker and a real worker and asserts on
what the worker reports back. There is deliberately
NO task_always_eager.

NON-DESTRUCTIVE — SAFE AGAINST ANY NON-PRODUCTION DATABASE
----------------------------------------------------------
The probe never flushes and never commits: it runs `SELECT 1` and adds one ORM
object that is left pending and discarded on teardown. No rows are written today,
so this test is safe against any non-production database; no isolated test DB is
required. Do NOT point a test worker at the production database — it is one edit
past the ordering invariant below from becoming a real INSERT.

HOW TO RUN (follow word for word, from the project root)
--------------------------------------------------------
1) Start Redis in its own terminal:
        redis-server
   (install once if needed: `brew install redis`; verify: `redis-cli ping` -> PONG)

2) Start a worker that registers THIS module's probe task, in its own terminal:
        .venv/bin/celery -A celery_worker.celery worker \
            --pool=solo --include=tests.integration.test_session_teardown --loglevel=info
   The probe task lives in this test file, so the worker must --include it.
   --pool=solo isolates the per-task lifecycle and avoids the macOS prefork
   fork-safety crash from the eager scikit-learn/numpy import in create_app().
   Confirm the banner lists task "tests.integration.session_teardown_probe" and
   prints "ready".

3) Run THIS test in its own terminal:
        .venv/bin/pytest -m integration tests/integration/test_session_teardown.py -v

  `.venv/bin/pytest` uses pytest from your virtual environment.
  `-m integration` means run tests marked as integration.
  `tests/integration/test_session_teardown.py` is the file to run.
  `-v` means verbose output.

If Redis or the worker is not up, this test SKIPS with a message that points back
to these steps — it will not hang on a raw broker timeout. The normal fast suite
never selects it (addopts excludes the integration marker; see pytest.ini below).

ONE-TIME PREREQUISITES (not created by this file):
  - pip install pytest
  - register the `integration` marker + exclude it from the default run
    (pytest.ini block is provided alongside this file)
  - add tests/__init__.py AND tests/integration/__init__.py so
    These make Python treat those folders as importable packages. And
    `tests.integration.test_session_teardown` is importable — required because the
    worker resolves `--include` by dotted path, and you must start the worker from
    the project root.
"""

import os

import pytest
import redis
from celery import Celery, shared_task
from dotenv import load_dotenv


# Task name is shared between the worker (registers it via --include) and the test
# client (sends it by name). Keep them identical.
_PROBE_NAME = "tests.integration.session_teardown_probe"

_SETUP_HINT = (
    "SKIPPED: this integration test needs a live Redis broker AND a running Celery "
    "worker ({reason}). See this module's docstring for the exact start-up steps:  "
    "redis-server  +  .venv/bin/celery -A celery_worker.celery worker --pool=solo "
    "--include=tests.integration.test_session_teardown"
)


@shared_task(name=_PROBE_NAME)
def _session_teardown_probe() -> dict:
    """
    Runs INSIDE the worker. Reports session/pool state at task entry, plants a
    marker to detect session reuse, then dirties the session so the next task can
    prove it was reset. Never flushes or commits — see module docstring.
    """
    # Local imports keep the pytest client process from importing the whole app
    # package (and its sklearn/anthropic imports); the worker already has them.

    from app.extensions import db
    from app.models import Notification

    sess = db.session

    # --- entry measurements: pure state reads, none of these emit SQL ---
    # Checks: [db connection leak], [pending ORM objects leak], [same session object reused]
    entry_checkedout = db.engine.pool.checkedout()
    entry_new = len(db.session.new)
    entry_session_reused = sess.info.get("_probe_marker", False)
    sess.info["_probe_marker"] = True  # survives into the next task ONLY if this Session is reused

    # --- the one real query: borrow a pooled connection so that asserting
    #     "checked back in to 0 next task" is meaningful and not trivially true.
    #     no_autoflush is belt-and-suspenders: nothing is pending here today, but
    #     it guarantees a query can never flush a pending add even after edits.
    with db.session.no_autoflush:
        db.session.execute(db.text("SELECT 1")).first()

    # === LOAD-BEARING ORDERING INVARIANT =========================================
    # add() MUST be the last DB-touching statement. Do NOT add any query, flush, or
    # commit after this line: SQLAlchemy autoflush would turn this pending object
    # into a real (uncommitted) INSERT, breaking the "no rows ever written"
    # guarantee. Reading db.session.new below is a pure-Python set length, NOT a
    # query, so it is safe.
    # =============================================================================
    db.session.add(Notification())
    exit_new = len(db.session.new)

    return {
        "entry_checkedout": entry_checkedout,
        "entry_new": entry_new,
        "entry_session_reused": entry_session_reused,
        "exit_new": exit_new,
    }


@pytest.fixture(scope="module")
def celery_client() -> Celery:
    """A bare client (no create_app, so no ML load) that talks to the live worker."""
    load_dotenv()
    return Celery(
        "session_teardown_test_client",
        broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
    )


@pytest.fixture
def require_live_worker(celery_client: Celery) -> None:
    """Pre-flight: skip/fail cleanly so a bad setup never hangs to the task timeout."""
    # 1) Is the Redis broker reachable at all?
    try:
        redis.from_url(celery_client.conf.broker_url, socket_connect_timeout=1).ping()
    except Exception:
        pytest.skip(_SETUP_HINT.format(reason="cannot reach the Redis broker"))

    # 2) Redis is up — is a Celery worker actually consuming? (broadcast ping, collect pongs)
    if not celery_client.control.ping(timeout=2.0):
        pytest.skip(_SETUP_HINT.format(reason="Redis is up but no Celery worker replied"))

    # 3) Worker is up — is THIS probe task actually registered on it? If --include /
    #    the tests package __init__.py files / cwd is off, the worker boots WITHOUT
    #    the task and send_task would hang to the 15s timeout. Fail fast with a clear
    #    cause instead. (Only assert when we got a task listing back; an empty reply
    #    is a rare race and ping already confirmed liveness, so we let it proceed.)
    registered = celery_client.control.inspect(timeout=3.0).registered() or {}
    known_tasks = {name for tasks in registered.values() for name in tasks}
    if registered and _PROBE_NAME not in known_tasks:
        pytest.fail(
            f"probe task '{_PROBE_NAME}' not registered on the worker — check "
            "--include=tests.integration.test_session_teardown, that "
            "tests/__init__.py and tests/integration/__init__.py exist, and that the "
            "worker was started from the project root."
        )


@pytest.mark.integration
def test_worker_removes_session_per_task(celery_client: Celery, require_live_worker) -> None:
    """
    Two sequential tasks; assert the worker reset its session between them.

    `.get()` on run-1 returns only after run-1's `with app.app_context()` has
    exited (teardown done), so run-2 genuinely observes the post-cleanup state.
    """
    run1 = celery_client.send_task(_PROBE_NAME).get(timeout=15)
    run2 = celery_client.send_task(_PROBE_NAME).get(timeout=15)

    # Guard against a vacuous pass: run-1 must actually have dirtied its session.
    assert run1["exit_new"] >= 1, (
        "probe did not dirty the session (run-1 exit_new == 0); the cleanliness "
        "assertions below would pass for the wrong reason."
    )

    # (1) connection returned to the pool by per-task teardown
    assert run2["entry_checkedout"] == 0, (
        f"run-2 started with {run2['entry_checkedout']} connection(s) still checked "
        "out — the worker did not return run-1's connection (session.remove() not "
        "firing per task)."
    )
    # (2) ORM state reset between tasks
    assert run2["entry_new"] == 0, (
        f"run-2 started with {run2['entry_new']} pending object(s) — ORM state from "
        "run-1 leaked across tasks (session not reset)."
    )
    # (3) fresh Session per task — the marker planted in run-1 did NOT survive
    assert run2["entry_session_reused"] is False, (
        "run-2 reused run-1's Session (the planted marker survived) — the scoped "
        "session was not removed and recreated per task."
    )
