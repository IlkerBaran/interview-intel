# 0013. Per-User Lifetime Analysis Quota

## Status

Accepted

## Context

Rate limiting (ADR-0012) caps how *fast* a user submits, not how *much* they consume. A
caller who stays politely within 10 submissions an hour still accumulates unbounded API
cost over days. Each submission fires one extraction call plus five enrichments against
the Anthropic API, with the full email body embedded in four of the six prompts, so the
per-submission cost is real and roughly fixed.

The limiter structurally cannot close this. Its storage is Redis, and ADR-0012 chose
`in_memory_fallback` precisely so an unreachable Redis degrades to per-process counters
rather than failing requests. That is the right posture for a burst defence and the wrong
one for a cost ceiling: a spend cap must not be built on a dependency deliberately designed
to keep serving when it breaks.

## Decision

Enforce a per-user lifetime allowance in the database, defaulting to 3 analyses
(`ANALYSIS_LIFETIME_QUOTA`).

### The slot is consumed at enqueue, in the message INSERT's transaction

Decrementing on completion would let a user submit many messages before any finished and
walk straight past the ceiling. The spend is committed the moment the task is queued, so
that is where the accounting belongs.

The check and the increment are one conditional `UPDATE ... WHERE analyses_used <
:allowance`, with `rowcount == 1` as the permission to proceed. This is race-safe without
locking: two concurrent submissions for the last slot are serialised by the database and
the loser's `WHERE` no longer matches. A read-then-write would let both observe `used = 2`
and both write `3`.

The INSERT and the increment share a single commit, so there is never a message without a
consumed slot nor a consumed slot without a message. Achieving that required the route to
stop calling `save_message_for_analysis()`, which committed internally and therefore could
not participate in another transaction.

### The column stores usage, not remaining

A `remaining` column would need `server_default='3'` in the migration, baking the allowance
into the schema. The env var and the column default then drift on the first change, and
raising the limit later would only help accounts created afterwards. `analyses_used` with a
default of `0` is correct for every pre-existing row at any allowance. Remaining is derived
at read time and clamped to `[0, allowance]`, so lowering the allowance below a user's
existing usage renders `0` rather than a negative number.

### Every failed path refunds, with one capped exception

A user who received no result should not lose a slot, and cannot manufacture a broker
outage, a worker crash, retry exhaustion, a soft time limit, or a reaper sweep (ADR-0011).
Those five refund unconditionally.

The exception is a pipeline that ran and produced nothing usable. That path is reached only
when neither the ML classifier nor the LLM extraction produced output, so no analysis was
delivered — but unlike the other five, a user can trigger it at will by submitting garbage.
Unconditional refunds there would make the ceiling unbounded for anyone willing to keep
pasting nonsense. It is therefore capped at 2 refunds per user
(`NOTHING_TO_SHOW_REFUND_CAP`); beyond that the analysis still reaches `FAILED`, but the
slot is spent. Rate limiting already throttles the loop, so this is defence in depth rather
than the only barrier.

### Refunds are idempotent and clamped

`acks_late` means a crashed task's failure handler can run more than once (ADR-0011), and a
naive refund would credit the user each time. A per-message marker (`messages.quota_refunded`)
is flipped by its own conditional `UPDATE`, and again `rowcount == 1` is the permission, so a
slot returns exactly once no matter how many redeliveries occur. The decrement is separately
guarded by `WHERE analyses_used > 0` as a backstop against a marker that is somehow wrong.

Marker and decrement are bound into one transaction: if the decrement matches no row the
marker rolls back too, so "marked refunded" and "slot actually returned" cannot drift apart.
The capped path spends its refund budget inside the same transaction for the same reason —
a redelivery that finds the marker already set rolls the budget increment back, so budget is
only ever consumed when a refund genuinely lands.

## Consequences

Total spend per user is bounded independently of Redis, of the limiter, and of the worker
fleet. The ceiling holds when Redis is down, when the limiter has degraded to per-process
counters, and when workers are being restarted.

The interface shows remaining allowance, never the raw usage figure and never above the
ceiling. An in-flight analysis displays the decremented number because the slot is genuinely
spoken for; it returns only if that analysis fails. At the limit the submission form is
replaced by an explanation rather than left to fail on submit, though the server-side
conditional `UPDATE` remains the real gate — the form is a courtesy, not the enforcement.

The failure banner reads the per-message marker rather than assuming, so it states whether
*this particular* failure cost a slot. Without that, a counter moving 3 → 2 → 3 with no
explanation reads as a bug.

`save_message_for_analysis()` and its private helper `_save_message()` were deleted rather
than left unused. Both took the same arguments as the quota-aware path and committed on
their own, so a future caller reaching for the obviously-named function would have written a
message with no slot consumed — a silent bypass, one rename away from looking correct.

### Limits of this guarantee

- **The quota is per account, not per person.** A determined user registers a second account
  and receives another allowance. Email verification raises the cost slightly; throwaway
  addresses defeat it. This bounds casual overuse, not a motivated adversary.
- **The refund rule was specified against the wrong model and corrected.** The original
  intent was to withhold refunds wherever spend had already occurred, and the
  nothing-to-show path was assumed to be that case. Inspecting the pipeline showed the
  opposite: that path is reachable only when *no* LLM call succeeded, while the generic
  exception handler is the one that can fire after all six calls completed — for instance
  when the results transaction fails to commit. Both refund now. Withholding on the generic
  handler would penalise ordinary post-spend bugs landing there, and the nothing-to-show cap
  addresses the loop rather than the spend.
- **A message deleted while `PENDING` keeps its slot consumed.** The reaper never sees the
  row, so no refund fires. This is correct for a spend cap — the task may already be running
  — and it fails closed.
- **Messages predating the migration carry an unset marker.** Their `quota_refunded` is
  `false` by server default although they never consumed a slot, so a failure banner on one
  will claim it used an analysis. Affects only rows created before rollout.
- **The cap is a ceiling, not a budget.** It counts analyses, not tokens or dollars. A user
  submitting three 20,000-character emails costs materially more than one submitting three
  short ones. Bounding actual spend would require metering token usage, which this does not
  do.
