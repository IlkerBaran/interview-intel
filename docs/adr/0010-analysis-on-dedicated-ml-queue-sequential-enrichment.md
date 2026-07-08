# 0010. Analysis on a Dedicated ML Queue, Enrichment Run Sequentially

## Status

Accepted

## Context

ADR-0008 gated model loading behind `LOAD_MODELS` and flagged the fast-email / slow-ML
queue split as the Stage 3 follow-up. Stage 3 moves the analysis pipeline onto Celery,
which is where that split finally happens.

Before Stage 3, the five "enrichment" LLM calls ran in a `ThreadPoolExecutor` with five
worker threads. That parallelism was chosen for latency, but it was also the root cause
of a silent bug: the enrichment threads had no Flask application context, `llm_service`
read `current_app` on every call, the call raised `RuntimeError` in each thread, and a
bare `except` swallowed it — so all five enrichments quietly saved `None` while the
message was still marked `COMPLETED`. (The real fix, making the LLM call
context-free, lives in the code; this ADR is about the concurrency decision, which is
what let the bug hide in the first place.)

Now that the pipeline is async, the reason for the threads — shaving wall-clock time off
a request the user is waiting on — no longer exists.

## Decision

Route the analysis onto its own queue and run the enrichment straight-line.

* `analyze_message` is routed to a dedicated `ml` queue (`task_routes`). ML workers run
with `LOAD_MODELS=1`; the email/default worker stays `LOAD_MODELS=0` (ADR-0008).
`--pool=solo` remains on macOS for the numpy/joblib fork-safety issue.
* Drop the `ThreadPoolExecutor`. Run the five enrichments **sequentially** in the task's
single application context and single DB session.
* Scale by worker concurrency and worker count for throughput, not by intra-task
parallelism.

The reasoning: I'm already making a large architectural change — synchronous to
Celery. Adding thread-level concurrency on top of that makes the first async version
harder to debug, and the parallelism speed is now irrelevant because the work is
background work nobody waits on. One task, one context, one session, one straight-line
pipeline is both simpler and safer — there are no thread-safety questions about the
Flask context or the SQLAlchemy session.

## Consequences

* A single analysis is slower end to end — six LLM calls in series instead of one
extraction plus five in parallel. Accepted: it is async, and no user is blocked on it.
* The `ml` queue is the second half of the split ADR-0008 set up (load-shedding was the
first half). A future `APP_ROLE` setting may subsume the `LOAD_MODELS` + `-Q` wiring
once worker roles multiply.
* Parallel enrichment can be re-introduced later as an isolated change, once the Celery
version is stable — but only under an enforced rule that any threaded function is
Flask-free and DB-free, so the original bug class (context/session accessed off the main
thread) cannot come back.
* Time limits behave differently by pool: under `--pool=solo` Celery does not enforce the
task time limits, which is one of the reasons the reliability design in ADR-0011 leans on
per-call timeouts and a reaper rather than on the Celery limits alone.
