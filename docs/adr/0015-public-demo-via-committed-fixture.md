# 0015. Public Demo via a Committed Fixture and the Real UI

## Status

Accepted

## Context

Before seeing any output, a visitor had to register, verify an email, and paste a
message. Most will not. That cost falls hardest on exactly the audience the project is
meant to reach — a recruiter or an engineer following a link, who will spend a minute
deciding whether the work is worth more of their time.

The obvious shapes for a demo each have a problem:

- **Run the pipeline live for anonymous visitors.** Every submission is one extraction
  call plus five enrichments against the Anthropic API (ADR-0012). A public
  unauthenticated route onto that is an unbounded bill and a queue anyone can flood. The
  per-user lifetime quota (ADR-0013) is keyed on a user, and there is no user here.
- **Seed a demo account.** Requires a seeding step at deploy, leaves a discoverable
  account with a password, and puts the samples in the database where they drift from
  the code that renders them.
- **Build a static mock of the output.** Cheapest, and demonstrates something that is
  not the app. A mock diverges from the real template the first time either changes,
  and the divergence is invisible until someone compares them.

There is also a credibility problem specific to this feature. A page that shows output
in five seconds, on an app that truthfully takes 10-15, is making a claim about
performance it cannot support. An engineer who notices will discount everything else on
the page.

## Decision

Serve three pre-computed analyses from a committed JSON fixture, replayed through the
real message UI, and say plainly on the page what is and is not happening.

### The samples live in the repo, not the database

`app/data/demo_samples.json` is produced by `scripts/dump_demo_fixture.py` from real
analyses and committed. No seeding at deploy, no demo account to find, and the samples
version with the code that renders them. The dump script parses the template's Jinja AST
on every run and refuses to write a fixture missing any attribute the template reads, so
a template change cannot silently produce a fixture that raises `AttributeError` at
request time.

### The loader returns plain dataclasses, never ORM models

`demo_service` builds frozen dataclasses. Detached `Message` / `AnalysisResult` / `Task`
instances would have been more convenient — they carry `Task.is_overdue` for free — but
they leave the demo one accidental `db.session.add()` or one relationship cascade away
from writing a row, on a page that promises in writing that it writes nothing. The
promise is structural rather than intended: there is no mapper and no session involved.
`is_overdue` is reimplemented, with a test asserting it agrees with the model.

### The read-only half of the message page is shared as macros

`messages/_analysis.html` holds four macros covering the classification, extracted
details, LLM sections, task list and raw email. `messages/show_message.html` keeps
everything that is an owner ACTION — Back, Archive, Add/Delete note, Delete email.

Macros rather than an `{% if demo %}` flag on `show_message.html`: the demo needs the
whole read-only half and none of the other, so a flag would put eight demo-shaped
branches inside the template that renders paid results, and every later edit to that
template would have to reason about both callers. The split line is "read-only output"
versus "write action", which is also exactly the line the demo needs.

The styles moved with them, verbatim, to `static/css/message-detail.css`, which both
pages link.

### One poller, not two

`static/js/analysis-poll.js` is the poller extracted from `show_message.html`, driven by
data attributes on `#analysis-status`. The real page passes `data-status-url` and reloads
in place, because the server will then render the finished result from the database. The
demo passes `data-poll-ms="1000"` and a `data-reload-url`, because its result lives at a
different URL and its replay is five seconds rather than fifteen to thirty.

Sharing the file is the point: a separate demo poller would be demonstrating something
that is not the app. Extraction also removes an inline script from both pages, which the
CSP TODO in `app/__init__.py` needs.

### The replay is stateless

`GET /demo/<slug>` stamps the current time into the status URL. `GET /demo/<slug>/status`
compares it against `REPLAY_SECONDS` and returns the same literal `MessageStatus` value
strings the real endpoint returns (ADR-0009). No session, no row, no cache entry. The
timestamp is client-supplied and deliberately unvalidated: nothing here is protected, and
a visitor who would rather skip the wait should be able to.

### Rate limits are explicit, and the status endpoint is not exempt

`60 per hour` per IP on the two page routes; `120 per minute` on the status endpoint.
`messages.message_status` is `@limiter.exempt`, but that exemption is justified by
`@login_required` gating it. This endpoint has no such gate, so it takes a high but
bounded limit instead.

### The page explains itself before the visitor starts

Above the picker, in ordinary prose rather than a banner or a footnote: these are saved
examples; the real pipeline is asynchronous and takes 15-30 seconds; this replays a
completed run so the wait is short; and nothing here runs the pipeline, writes to the
database, or triggers a paid API call. A separate short section states what the endpoint
does and does not do, because a public unauthenticated route on an app that costs money
per request is the first thing a reviewer will wonder about.

The wording is asserted in tests. It is a requirement of the feature, not decoration, and
should fail loudly if it is ever edited away.

## Consequences

- The demo cannot show anything the samples do not contain. Adding a case means running a
  real email through the pipeline and re-running the dump — deliberately, since the point
  is that these are real outputs.
- `show_message.html` went from 713 lines to 118. Its rendered body was proved
  byte-identical after the extraction (CSRF token normalised out) and its screenshots
  unchanged at 1280px and 480px, so the sharing cost the real page nothing.
- Two templates now depend on `messages/_analysis.html`. A change to a shared macro
  changes both pages, which is the intended trade: the demo is worth having only if it
  stays the real interface.
- The samples carry fixed timestamps. They will read as older over time. Accepted: they
  are saved runs, and a page arguing against pretending should not pretend they are
  fresh.
- The fixture is read once and cached at module level, so editing it requires a restart.
  Acceptable for a committed file.
