# 0004. Separate Redis Databases and Environment Variables for Broker and Results

## Status

Accepted

## Context

Celery uses Redis for two distinct jobs:

* the broker, which holds pending tasks
* the result backend, which stores task results and their TTLs

Many tutorials point both at the same Redis database, usually:

```text id="qo2z38"
redis://localhost:6379/0
```

Putting both on one database mixes two concerns. Flushing stale results could also clear queued messages.
Inspecting the queue means sifting through result keys. There is also no clean seam to pull the two apart later.

In production, these two roles may need different lifecycles, memory limits, credentials, TLS settings, or
even separate Redis instances.

That separation is cheaper to arrange now than to retrofit later.

## Decision

The broker and result backend are configured as two separate environment variables:

```text id="n84m8j"
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
```

Locally, they share one Redis instance but use different logical databases:

* broker on `/0`
* results on `/1`

## Consequences

* Results can be flushed from `/1` without touching queued messages in `/0`.
* Broker data and result data can be inspected independently.
* Because the broker and result backend are already separate environment variables, moving to authenticated Redis,
TLS Redis through `rediss://`, or two entirely separate Redis instances is an environment-only change with
no code edits.
* Numbered Redis databases are a single-instance convenience, not a security boundary. Redis Cluster and some
managed Redis providers do not support multiple logical databases. Real isolation comes from separate instances
with separate credentials.
* The `/0` and `/1` split is the better local default, not necessarily the final production architecture.
* Redis URLs are read from the environment and never committed, so production values can include secrets such as
credentials or TLS endpoints.
