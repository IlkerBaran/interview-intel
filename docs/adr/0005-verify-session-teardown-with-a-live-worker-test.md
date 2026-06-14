# 0005. Verify Per-Task Session Teardown With a Live-Worker Integration Test

## Status

Accepted

## Context

The Celery integration wraps every task in `with app.app_context()` through the `FlaskTask` base in
`app/celery_app.py`.

Popping that context is what fires Flask-SQLAlchemy's teardown, which calls `db.session.remove()`.
That should return the database connection to the pool and reset the session before the next task runs.

The next stage, migrating real database-touching email tasks onto Celery, depends on this being true.
If the session is not removed per task, a long-running worker could leak sessions and connections across tasks
until it can no longer get a connection.

The mechanism was confirmed by reading the Flask-SQLAlchemy source, but reading the code that says something
works is not the same as observing it work in this project. This property needed to be verified before real database
tasks were built on top of it.

The hard part is how to verify it. The behavior is a property of a separate, live worker process crossing
task boundaries. It does not exist in-process, and it does not exist in Celery's eager mode, `task_always_eager`,
which runs the task body in the caller and never exercises the worker's per-task context lifecycle.

## Decision

Verify the behavior with a live-worker integration test: a real Redis broker and a real Celery worker, with
the test asserting on what the worker reports back across two sequential tasks.

The test is deliberately not run in eager mode. It is marked `integration` so it is excluded from the default
fast test run.

This is the intentional exception to the general rule that tests should avoid live external services, because
here the live worker is the only thing that can prove the claim.

Key design points:

* **Three assertions on the second task:** the connection was returned to the pool, no ORM state leaked
across tasks, and the session was recreated. The session recreation check uses a marker planted on `session.info`
in task one and confirms it is absent in task two.
* **Non-destructive:** the probe queries and adds a pending ORM object but never flushes or commits, so no rows
are written. An explicit ordering invariant and `no_autoflush` keep it that way after future edits.
* **Vacuous-pass guard:** task one must actually dirty its session, or the cleanliness assertions could pass for
the wrong reason.
* **Fail-fast pre-flight:** the test checks that Redis is reachable, that a worker is consuming, and that the probe
task is registered. Bad setup skips or fails with a clear message instead of hanging until timeout.

## Consequences

* Per-task session cleanup is now an observed fact, not only a source-level deduction, before any real database task
depends on it.
* The test requires Redis and a live Celery worker, so it is slower and kept out of the fast suite. This cost is
accepted because, for this specific claim, the live worker is the proof.
* Several weaker approaches were considered and rejected because they could pass for the wrong reason:

  * **Eager mode** skips the worker's context lifecycle entirely. It could pass while a real worker leaks.
  * **A row-count rollback assertion** would not prove rollback because the probe never flushes. The row count
  would remain unchanged even if teardown did nothing.
  * **An `id(session)` identity check** could be flaky because CPython can recycle an object ID after
  garbage collection. A deterministic `session.info` marker is more direct.
* General principle carried forward: a test is only worth keeping if it passes for the reason it is supposed to pass.
A convenient test that does not prove the real claim creates false confidence.
