# 0012. Rate Limiting: IP Keying, Fail-Open Storage, and Proxy Trust

## Status

Accepted

## Context

Every endpoint in this app was unthrottled. Four categories mattered:

- **Routes that send real mail.** `resend-verification` is public, gated only by a session
  value, and every POST triggers a Resend send. `forgot-password` is the same shape. Both
  are mail-bomb vectors, and the damage is deliverability and domain reputation — harder to
  undo than a bill.
- **Routes that cost money.** A message submission fires one extraction call plus five
  enrichments against the Anthropic API, with the full email text embedded in five of the
  six prompts.
- **Brute-force targets.** `login` has timing-attack mitigation but no attempt throttling.
- **Unbounded reads and writes.** The data export builds the user's entire history in
  memory with no pagination.

Rate limiting is the burst defence. It is deliberately *not* the cost cap — that is a
separate per-user quota enforced in the database (ADR-0013), because the limiter's storage
is a soft dependency and cost control must not be.

## Decision

Adopt Flask-Limiter with Redis storage on its own database, and make four decisions
explicitly.

### Auth limits are keyed on IP, never the submitted address

`login()` and `forgot_password()` are written to return identical responses whether or not
an account exists — the `_DUMMY_PASSWORD_HASH` timing guard, identical flash copy. Keying a
limit on the **submitted email** would undo that: an attacker submits an address four
times, and a 429 means the account exists while a 200 means it doesn't. That is an
account-existence oracle handed back through a side door.

The IP key is independent of the submitted address, so the response is identical either
way. Verified behaviourally rather than by inspection: twelve logins with twelve *different*
addresses from one client shared a bucket and tripped at the eleventh, which is only
possible if the key ignores the address.

`resend-verification` carries a second limit keyed on an HMAC of the session's pending
address — not a submitted field, so it cannot be used to probe accounts. Without it, an
attacker rotating IPs could still bomb a single inbox. The address is HMAC'd rather than
plainly hashed because a bare hash of an email is reversible in practice: hash a wordlist
and match. It is normalised through the same `normalize_email()` the auth forms use, so
`Test1@Example.com` and `test1@example.com` cannot occupy separate buckets.

### Unreachable storage falls back rather than failing

Measured against a genuinely dead Redis, five POSTs to a 2/hour route:

| configuration | result |
|---|---|
| default | `[500, 500, 500, 500, 500]` |
| `swallow_errors` | `[200, 200, 200, 200, 200]` |
| `in_memory_fallback` | `[200, 200, 429, 429, 429]` |

`swallow_errors` turns a Redis blip into an open door on precisely the mail-sending routes
the limiter exists to protect. `in_memory_fallback` keeps limits enforced against
per-process counters. It is left on, and `swallow_errors` off, so a genuine
misconfiguration surfaces rather than silently disabling protection.

Explicit socket timeouts accompany this. A *refused* connection fails immediately, but a
*hung* one blocked each request for 150 seconds against a blackholed address — long enough
to exhaust a worker pool from a dependency that is supposed to be optional.

### Proxy trust is explicit and required in production

Rate limits key on client IP, so the app must observe the real client address. Behind a
proxy, `remote_addr` is the proxy's own IP and every user shares one bucket. ProxyFix
rewrites it from `X-Forwarded-For`, trusting N values from the right.

The failure modes are asymmetric:

- **Too low** — every user shares one bucket, and the global default becomes a site-wide
  lockout.
- **Too high** — any client forges a left-hand value and mints a fresh bucket per request,
  bypassing every IP limit.

Demonstrated at hops 0, 1 and 2 against a single proxy: at the correct count, four distinct
clients each got their own bucket and a forged header was ignored; one too high, forgery
succeeded and limits vanished entirely.

Because there is no safe default, `TRUSTED_PROXY_HOPS` must be set explicitly in
production, and the app refuses to start otherwise — matching how the other required
secrets already behave.

### The polling endpoints are exempt

The analysis status endpoint is polled every three seconds for the duration of a run —
roughly twenty requests per minute per open tab. Any global default below that breaks the
async UX from ADR-0009 mid-analysis. It and the notification endpoints are exempt outright.
Their protection is authentication plus ownership scoping, not throttling.

## Consequences

Limits are enforced where abuse costs money, sends mail, or guesses credentials, and the
enumeration guarantee that `login` and `forgot-password` already provided is preserved
rather than quietly reversed.

The 429 handler content-negotiates so JSON clients receive JSON, and `Retry-After` is
preserved. Note that `RateLimitExceeded.retry_after` is `None` — the value lives in the
header, not the attribute.

Adding the limiter also exposed a latent problem in the production config guards, which
raised unconditionally at import time and so made `config.py` unimportable without a `.env`.
The test suite passed only because `.env` happened to supply five variables. Those raises
are now gated on `FLASK_ENV`, and the suite passes with no `.env` present.

### Limits of this guarantee

- **ProxyFix is unverified against a real proxy.** Header parsing and bucket separation
  were confirmed by synthesising `X-Forwarded-For` at each hop count. What that cannot show
  is how many values the actual deployment appends. That number must be confirmed
  empirically in the deployed environment by logging the real header and counting.
- **Fallback counters are per-process.** During a storage outage with N workers, the
  effective limit is up to N×. Degraded, not open.
- **Longer windows are untested at their boundaries.** In a same-minute burst the
  per-minute limit dominates, so the hourly and daily ceilings never get exercised without
  minutes of wall clock. They are registered and counting; the boundary behaviour is
  unproven.
- **Rate limiting does not bound total spend.** A patient caller within the limits still
  accumulates API cost over time. That is the quota's job, not this mechanism's.