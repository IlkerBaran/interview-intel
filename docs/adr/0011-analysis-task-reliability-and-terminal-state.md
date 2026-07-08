# 0011. Analysis Task Reliability: Atomic Writes, Idempotency, Terminal State

## Status

Accepted

## Context

The analysis task runs under `acks_late=True` (ADR-0007 established late-acks for the
email tasks): a task is acknowledged only after it finishes, so a worker crash redelivers
it, and transient failures retry. Both mean the task can run more than once.

Unlike an email send, re-running the analysis *mutates the database*. A naive re-run
would create a duplicate `AnalysisResult`, duplicate `Task` rows, another `AgentRun` and
`Notification`, and re-charge every LLM call. Separately, an async pipeline must never
strand a message in a non-terminal state: the polling UX (ADR-0009) depends on every
message eventually reaching `COMPLETED` or `FAILED`, and several failure modes — a
hard-killed worker, an OOM, a message enqueued with no worker — kill the process before
any in-task cleanup can run.

## Decision

Make re-runs safe, classify retries deliberately, and guarantee a terminal state.

**Idempotency and atomicity**

* All result writes are atomic: `AnalysisResult`, the `Task` rows, `AgentRun`, and the
`Notification` commit together in one transaction, so a crash leaves nothing partially
written. (This moved notification creation out of its own separate commit into the
pipeline transaction.)
* A guard at task start skips the work if it already completed — status `COMPLETED`, or
an `AnalysisResult` already exists for the message — so a redundant redelivery does not
re-run.

**Retry classification**

* Retry only transient failures (timeouts, 429, 5xx, and 529 overloaded) with a bounded
count; permanent failures (validation, bad input) are not retried. Transient error types,
which subclass the generic Anthropic API error, are caught and re-raised *before* the
generic handler. 529 Overloaded is a distinct class — not a subclass of the 5xx internal
error, and not re-exported at the SDK's top level — so it is listed explicitly and cannot
be misclassified as permanent.
* Only the critical extraction call triggers a pipeline retry. The five enrichments are
best-effort: a failure degrades that field to `None`, it never re-runs the pipeline.
* Retries are driven by a manual `self.retry` with a 15s / 30s / 60s backoff, not by
`autoretry_for`. This is deliberate: it lets retry *exhaustion* mark the message `FAILED`
rather than ending in a bare Celery `FAILURE` that would leave it stuck `PROCESSING`.

**Terminal-state guarantees**

* Enqueue failure (broker unreachable) → the route marks the message `FAILED` instead of
leaving it `PENDING` with no task queued.
* Permanent error, exhausted retries, or a soft time limit → the task marks it `FAILED`.
The marking rolls back any half-open transaction, then commits, completing before the
Flask app-context teardown runs `session.remove()` so the two do not fight.
* Explicit timeouts at both layers: per-call Anthropic HTTP timeouts so a hung call fails
instead of stalling the task, and a Celery `soft_time_limit` (240s) that routes into the
FAILED path, with a `time_limit` (300s) hard backstop.
* A periodic reaper (Celery beat) marks any message stuck in `PENDING`/`PROCESSING` past
a safe age (900s, above the hard limit plus all retry backoffs) as `FAILED` — the
backstop for the cases in-task cleanup structurally cannot reach: hard-kill SIGKILL, OOM,
machine death, or a message enqueued with no worker. The sweep is a single atomic
`UPDATE` gated on both status and staleness, so a task that just flipped to `COMPLETED`
is never clobbered.

## Consequences

* A mid-flight crash re-charges the LLM calls on retry. Correctness is preserved — the
guard plus atomic writes mean no duplicate rows — but the re-charge is a small, rare cost
that cannot be fully eliminated: exactly-once across an external API and a local database
is impossible, the same limit noted for email in ADR-0007. An Anthropic `Idempotency-Key`
is passed on the critical extraction call as a cheap best-effort reducer, but the SDK does
not document Messages-API cost-dedup as a guarantee, so correctness never depends on it.
* Partial-failure semantics are explicit: `FAILED` only if neither ML nor LLM produced
anything (nothing to show); otherwise `COMPLETED` even when some enrichments are `None`
(degraded). A translation failure is `COMPLETED`-degraded — the raw email and detected
language are still worth showing — not `FAILED`.
* Time limits are enforced on the prefork pool (production Linux). Under `--pool=solo`
(macOS dev, per ADR-0010) they are not enforced, which is exactly why the per-call
Anthropic timeouts are the pool-independent hang guard and the reaper is the ultimate
backstop rather than the Celery limits alone.
* Residual holes, flagged not fixed: a full database outage means no terminal state is
reachable at all, since marking `FAILED` needs the DB (late-acks preserve the work until
it recovers); the reaper depends on the beat scheduler being alive; and a queue backlog
longer than the reaper age would reap messages a worker would eventually have served —
raise the age threshold for bursty load.
* The reaper adds one new always-on process (Celery beat) and one new tunable
(`ANALYSIS_STUCK_AFTER_SECONDS`) to operate correctly.
