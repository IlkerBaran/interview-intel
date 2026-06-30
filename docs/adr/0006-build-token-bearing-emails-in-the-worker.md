# 0006. Build Token-Bearing Emails in the Worker, Not From a Pre-Rendered Payload

## Status

Accepted

## Context

Stage 1 left email sending on a daemon thread. That thread ran in-process, inside the
request, so the natural interface was `queue_email(to, subject, html, plain)`: the route
rendered the whole email — including the verification or reset link with its raw token —
and handed the finished payload to the background work. The token never left the process.

Migrating that same signature onto Celery breaks the assumption it was built on. Celery
arguments are serialized through Redis. Passing the rendered email would place a usable
auth token in the broker, where it sits until the message is consumed. The token design
exists precisely to prevent this: only the SHA-256 hash is stored in the database, so a
database leak never yields a usable token. A raw token in Redis silently undoes that —
anyone who can read the broker (Flower, monitoring, a second consumer, `redis-cli
MONITOR`) could use the link before the user does.

The old signature, then, was a fossil of the threads model, not a deliberate interface.
Two shapes were weighed:

* **(a)/(c)** Keep passing rendered content (or the raw token) as a JSON string and
protect the broker with `ignore_result`, TTLs, auth, and TLS. This *manages* the exposure.
* **(b)** Pass only a `user_id`; the worker re-fetches, re-authorizes, generates the
token, renders the link, and sends. No usable token ever enters the broker. This *removes*
the exposure.

## Decision

Use option (b). Routes enqueue `(user_id, idempotency_key)`; everything that touches the
token happens inside the worker.

This is not a security tax bolted onto the migration. Moving to a separate process behind
a broker is the natural moment to reshape the interface from "hand over a finished email"
to "pass an id, build it in the worker" — the queue-native shape. The secure property
falls out of that reshaping rather than being added to defend a leak.

Option (a)/(c) was rejected because its exposure is real and *grows* with the system:
every future queue reader re-exposes the token, and the mitigations protecting it must all
stay true over time, maintained by people who may not know why. "Nothing to protect" beats
"a secret plus a chain of mitigations."

## Consequences

* The four auth call sites change to pass an id instead of rendering email. This was an
accepted one-time edit, not a constraint to preserve.
* The worker now builds external URLs outside a request, so `SERVER_NAME` and
`PREFERRED_URL_SCHEME` must be configured per environment and are required in production. A
wrong host fails loudly in testing, never silently in an inbox.
* Building links in the worker surfaced a latent bug: the unread-count context processor
ran on every `render_template` and touched `current_user`, which is `None` outside a
request and raised `AttributeError`. Fixed with a `has_request_context()` guard.
* Tokens are now created at send-time, not request-time. The user-facing flashes were
already future-tense ("you will receive a link shortly") and never depended on the token
existing when the request returned.
* This applies specifically to transactional, token-bearing email. It is the deliberate
counterpart to the general "pass ids, not objects" rule for later work: there the worker
re-fetches and re-authorizes a record; here there is additionally no reusable secret to
leak.
