# 0002. Use Celery and Redis for Background Jobs

## Status

Accepted

## Context

Background work in this project currently runs on Python threads — for example,
sending email on a daemon thread so the web request can return immediately.

This is adequate for occasional, fire-and-forget notifications, but it breaks down
for the work that is coming:

* **Inbound email ingestion** — a webhook must respond within seconds or the email may be
retried or dropped. Heavy processing should not run inside the request; it should be handed
to a background worker.
* **Scheduled jobs** — task reminders and re-engagement emails need to run on a schedule,
with no user request present. Threads are not a durable scheduler.
* **Reliability** — threads have no built-in retries, do not survive a restart, and give
no visibility into what is pending or what failed.

This is the point where threads stop being good enough, so a real background-job system is needed.
The main alternatives considered were RQ, Dramatiq, and Celery.

## Decision

This project will use **Celery** as the task queue, with **Redis** as the message broker and
result backend.

Celery was chosen over simpler alternatives such as RQ and Dramatiq for three reasons,
in order of weight:

1. **Python 3.14 support.** The project runs on Python 3.14. Celery 5.6 has official Python 3.14 support;
RQ and Dramatiq do not advertise first-class Python 3.14 support yet. "Probably works" is not acceptable
for the queue layer.
2. **The roadmap needs scheduled jobs and queue routing built in.** Scheduled jobs through Celery Beat and
a later split between a fast email queue and a slower ML/analysis queue are already planned. Celery provides
both natively; RQ would require add-ons such as `rq-scheduler` and is weaker on routing.
3. **It is widely used in backend systems.** Celery is common in Python backend projects, so the experience
is transferable.

## Consequences

* The application now runs as multiple cooperating processes: the Flask app, Redis, the Celery worker,
and later Celery Beat. This adds operational complexity.
* Background work gains retries, persistence across restarts, and visibility into pending and failed jobs —
the things threads could not provide.
* Celery's configuration is heavier than RQ's. This cost is accepted deliberately and mitigated by rolling
the system out in stages, proving the wiring in isolation before any feature depends on it.
* **Honest tradeoff:** for a greenfield project without a Python 3.14 constraint and without the Beat/routing
roadmap, RQ would have been the simpler and reasonable choice. Those constraints are what make Celery the right
call here, not a blanket belief that Celery is always best.
* New dependency: `celery[redis]` pinned as `>=5.5,<6.0`, plus a running Redis instance.
