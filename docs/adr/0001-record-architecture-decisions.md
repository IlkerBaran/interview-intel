# 0001. Record Architecture Decisions

## Status 

Accepted

## Context

Until now, most technical choices in this project have been routine implementation
decisions: routes, templates, forms, models, authentication, and basic email sending.

The move from thread-based background work to a real background-job system is
the first major architectural fork in the project. It introduces real alternatives
and tradeoffs, including whether to keep using threads or adopt a more full-featured
task queue.

As the project expands, more architectural decisions will need to be made;
such decisions are easily forgotten later on. Code shows what has been built,
but it does not reveal why that path was taken.

## Decision

This project will use Architecture Decision Records (ADRs) to document significant
technical decisions from this point forward.
 
ADRs will live in `docs/adr/`, with one Markdown file per decision, numbered
sequentially:
 
```text
0001-record-architecture-decisions.md
0002-short-decision-title.md
0003-short-decision-title.md
```

Each ADR will use a lightweight structure:
 
* Title
* Status
* Context
* Decision
* Consequences

ADRs will only be written for decisions where a reasonable engineer could have chosen
differently. Routine implementation details will not be recorded.
 
If a decision is later replaced, the original ADR will not be deleted. Instead, a new
ADR will supersede it, and the original's status will be updated to point to the
replacement (e.g. "Superseded by ADR-0009"). This preserves the history of the
project's technical choices while making it clear which decision is current.

## Consequences
 
* The reasoning behind major technical decisions is preserved next to the code.
* Future maintainers can understand not only what was built, but why it was built that
  way.
* This adds a small amount of writing overhead, but only for decisions important
  enough to justify documenting.
* Older decisions will not be documented retroactively unless they become important to
  future work.