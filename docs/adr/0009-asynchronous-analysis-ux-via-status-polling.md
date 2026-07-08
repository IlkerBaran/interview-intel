# 0009. Asynchronous Analysis UX via Status Polling

## Status

Accepted

## Context

The message-analysis pipeline — language normalization, an ML classifier, and roughly
six LLM calls — ran synchronously inside the web request. The user's browser blocked
until the whole thing finished, seconds to tens of seconds, and a slow or hung LLM call
held the request open the entire time.

Stage 3 moves the pipeline onto Celery (ADR-0002) on a dedicated ML queue (ADR-0010), so
the request now has to return *before* the work is done. That forces a UX question: the
message page must render while there is nothing to show yet, and then update itself once
the analysis lands. Two options deliver a finished result to an already-loaded page —
poll a cheap status endpoint and reload when it flips to done, or open a second channel
that injects the finished HTML fragment at the completion moment.

## Decision

Return immediately and let the page poll for a terminal status, then full-reload.

* The route saves the message as `PENDING` and returns. The `show_message` page shows an
"Analyzing…" state for `PENDING`/`PROCESSING`, reusing the existing `MessageStatus` enum
— no new state was added.
* A cheap `GET /messages/<id>/status` returns JSON `{"status": ...}`: one
ownership-scoped DB lookup, no template render.
* Client JS polls every few seconds. On `COMPLETED` or `FAILED` it does a single
`location.reload()`, so the normal server template renders the finished results (or the
FAILED banner). A max-attempts cap (~5 minutes) stops a stuck page from polling forever.
* Full reload was chosen over HTML-fragment injection. The scalability win comes from
polling a cheap status endpoint, not from how the result is delivered; injection only
buys a smoother completion moment at the cost of a second endpoint and more JS.

The status endpoint, the template comparisons, and the polling JS must all agree on the
exact `MessageStatus` *value* string. `str(MessageStatus.COMPLETED)` returns
`'MessageStatus.COMPLETED'`, not `'completed'`; the endpoint normalizes to `.value`, and
a regression test asserts the literal string, because a mismatch silently breaks the
whole flow — the page would poll forever and never update.

## Consequences

* The repeated request is a single-row status lookup, not a full analysis or a full page
render, so an open "Analyzing…" page is cheap to keep alive.
* Full reload re-fetches the whole page once at completion, versus injection's in-place
update. Accepted as the simpler design: one small endpoint and a few lines of JS, no
fragment-rendering path to maintain.
* Polling has a floor cost — one request per few seconds per open page. Acceptable at
current scale; a push mechanism (SSE or websockets) is deferred, not adopted now.
* The ~5-minute cap means a message still non-terminal after that window shows a
"refresh to check" hint rather than spinning indefinitely. The reliability backstops in
ADR-0011 guarantee the message itself still reaches `COMPLETED` or `FAILED`, so the UI
and the backend converge even in the worst case.
