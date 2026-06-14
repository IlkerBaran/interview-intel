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

| ADR                                                             | Decision                                        | Status   |
|-----------------------------------------------------------------|-------------------------------------------------| -------- |
| [0001](0001-record-architecture-decisions.md)                   | Record Architecture Decisions                   | Accepted |
| [0002](0002-use-celery-and-redis-for-background-jobs.md)        | Use Celery and Redis for Background Jobs        | Accepted |
| [0003](0003-use-json-serializer-not-pickle.md)                  | Use JSON Serializer, Not Pickle                 | Accepted |
| [0004](0004-separate-redis-databases-for-broker-and-results.md) | Separate Redis Databases for Broker and Results | Accepted |
| [0005](0005-verify-session-teardown-with-a-live-worker-test.md) | Verify Session Teardown With a Live-Worker Test | Accepted |

## Adding a new ADR

Create the next-numbered file, copy the structure of an existing record, and add a row to the index above.

Use `Proposed` if the decision is still being considered. Use `Accepted` once the decision is settled.