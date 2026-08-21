# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for this project.

ADRs document significant technical decisions: the context, the choice made, and the consequences. They are used
only for decisions where a reasonable engineer could have chosen differently.

Routine implementation details are not recorded here.

## Conventions

* One decision per file, numbered sequentially: `0001-...`, `0002-...`, etc.
* Each record follows a lightweight structure: Title, Status, Context, Decision, and Consequences.
* Status is one of: `Proposed`, `Accepted`, `Superseded`, or `Deprecated`.
* If a decision is later replaced, the old ADR is not deleted. A new ADR supersedes it, and the old ADR's status is
updated to point to the replacement.

See [ADR-0001](0001-record-architecture-decisions.md) for why this practice exists and when it started.

## Index

| ADR                                                                       | Decision                                      | Status   |
|---------------------------------------------------------------------------|-----------------------------------------------| -------- |
| [0001](0001-record-architecture-decisions.md)                             | Record Architecture Decisions                 | Accepted |
| [0002](0002-use-celery-and-redis-for-background-jobs.md)                  | Use Celery and Redis for Background Jobs      | Accepted |
| [0003](0003-use-json-serializer-not-pickle.md)                            | Use JSON Serializer, Not Pickle               | Accepted |
| [0004](0004-separate-redis-databases-for-broker-and-results.md)           | Separate Redis Databases for Broker and Results | Accepted |
| [0005](0005-verify-session-teardown-with-a-live-worker-test.md)           | Verify Session Teardown With a Live-Worker Test | Accepted |
| [0006](0006-build-token-bearing-emails-in-the-worker.md)                  | Build Token-Bearing Emails in the Worker      | Accepted |
| [0007](0007-at-least-once-email-delivery.md)                              | At-Least-Once Email Delivery, Latest-Token-Wins | Accepted |
| [0008](0008-gate-eager-ml-loading-behind-load-models.md)                  | Gate Eager ML/LLM Loading Behind LOAD_MODELS  | Accepted |
| [0009](0009-asynchronous-analysis-ux-via-status-polling.md)               | Asynchronous Analysis UX via Status Polling   | Accepted |
| [0010](0010-analysis-on-dedicated-ml-queue-sequential-enrichment.md)      | Analysis on a Dedicated ML Queue, Enrichment Sequential | Accepted |
| [0011](0011-analysis-task-reliability-and-terminal-state.md)              | Analysis Task Reliability and Terminal State  | Accepted |
| [0012](0012-Rate-Limiting-IP-Keying-Fail-Open-Storage-and-Proxy-Trust.md) | Rate Limiting: IP Keying, Fail-Open Storage, and Proxy Trust  | Accepted |
| [0013](0013-per-user-lifetime-analysis-quota.md)                          | Per-User Lifetime Analysis Quota              | Accepted |
| [0014](0014-classifier-retrain-and-label-redesign.md)                     | Classifier Retrain and Label Redesign         | Accepted |
| [0015](0015-public-demo-via-committed-fixture.md)                         | Public Demo via Committed Fixture          | Accepted |

## Adding a new ADR

Create the next-numbered file, copy the structure of an existing record, and add a row to the index above.

Use `Proposed` if the decision is still being considered. Use `Accepted` once the decision is settled.