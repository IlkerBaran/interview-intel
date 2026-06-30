# 0007. At-Least-Once Email Delivery: Latest-Token-Wins, No Send-Level Dedup

## Status

Accepted

## Context

Email tasks run with `acks_late=True`: a task is acknowledged only after it finishes, so a
worker that crashes mid-send redelivers the message instead of silently dropping a real
user's verification or reset email. Combined with retries on transient failures, this means
a task can run more than once — the classic at-least-once delivery question. A crash
between sending and acking, or a successful send whose response was lost, can resend an
email.

The obvious answer was a stable idempotency key sent to Resend (its API supports an
`Idempotency-Key` header) so the provider dedupes the duplicate. We started down that path
and found it would *break* delivery under the ADR-0006 design. Because the worker generates
the token on each attempt and only the hash is stored, every attempt produces a different
valid token. If a redelivery reuses the same key, Resend suppresses the second email — but
that second email is the one carrying the token now stored in the database. The user is
left holding the first email, whose token was overwritten and is no longer valid: a dead
link. That is strictly worse than having no key at all.

## Decision

Do not use send-level deduplication. Adopt *latest-token-wins*:

* `acks_late=True` plus bounded autoretry on transient errors — redeliver, don't drop.
* No idempotency key is passed to Resend. Each attempt sends its own internally-consistent
email: the link matches the token that attempt just committed.
* Earlier attempts' links go stale and fail closed into the existing "request a new link"
flow. The worst case is a harmless duplicate email whose older link is dead.
* The `uuid4` key is kept only as a correlation/tracing id across attempts, never for dedup.

A deterministic business key (e.g. derived from user and purpose) was also rejected: it
would dedup a *legitimately* requested second reset, silently denying a user who clicks
"resend".

## Consequences

* There is a committed-token-but-no-email case: if all retries are exhausted, the last
attempt's token is committed to the database but no email was delivered. This is harmless —
the row holds a hash with no live link, overwritten by the next request — and the user
simply requests another link.
* Retry classification is explicit: transient failures (network and transport errors,
which the Resend SDK wraps as a base `ResendError`, plus 429/5xx) retry with backoff;
permanent failures (validation, missing or invalid API key) are caught in-body and fail
fast with no retry storm. A regression test guards this split so a future SDK change cannot
silently turn a retryable error into a lost one.
* Exactly-once delivery is explicitly not attempted; the crash-between-send-and-ack window
cannot be closed without a shared dedup store, and the cost — a rare duplicate email — does
not justify it.
* `worker_prefetch_multiplier` and the broker `visibility_timeout` become relevant once
email workers scale horizontally under late-acks. These are deferred pre-production tuning,
noted here, not set now.
