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

Adopt Flask-Limiter with Redis storage on its own database, and make six decisions
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

Because there is no safe default, the proxy posture must be set explicitly in production
and the app refuses to start otherwise — matching how the other required secrets already
behave.

That reasoning survives. The hop *count* does not: it assumes the chain length is fixed,
and on Railway it is not. The next two sections replace the count for the client IP and
keep it for the scheme.

### The client IP is resolved by position, not by a hop count

`TRUSTED_PROXY_HOPS` originally fed both `ProxyFix(x_for=…)` and `ProxyFix(x_proto=…)`.
Two problems, found in that order: one count cannot serve two headers that carry different
numbers of values, and — the larger one — **a count from the right is the wrong model for
Railway at all**.

#### What the deployed service actually sends

Measured on the live service by logging the raw forwarding headers, repeated across many
requests, and compared against a request carrying a client-supplied
`X-Forwarded-For: 1.2.3.4`. The left-hand value was checked against an independently
measured public IPv4 address:

| observation | result |
|---|---|
| `X-Forwarded-For` leftmost | the real client IP, on **every** request |
| `X-Forwarded-For` right-hand entry | an intermediary, and **not stable** — different prefixes (`152.…`, `79.…`) across repeats |
| `X-Real-IP` | the real client IP on every request tested |
| `X-Forwarded-Proto` | exactly one value, `https` |
| forged `X-Forwarded-For: 1.2.3.4` | **never appeared** — stripped every time |

The chain observed was two values. The point is that the second one moves: Railway routes
through more than one path, and the entry that a right-hand count would land on is not the
same machine from request to request.

#### Why a fixed `x_for` is not appropriate here

ProxyFix's `x_for=N` takes the Nth value from the right, which is only correct when N
equals the number of trusted proxies appending to the header. That requires a **fixed**
chain length. Railway's varies: it alternates between a direct edge path and a path with
the CDN layer in front, and Railway's own support answers describe the extra hop as
something you "may see" rather than a guarantee. Their published guidance is explicit —
*"Use `X-Forwarded-For` and take the first IP. This will work consistently across both
routing paths."*

Pinning `x_for=2` would work exactly as long as the two-value shape holds, and then fail
silently in whichever direction the chain moved:

- **Chain grows to three** (`client, cdn-edge, railway-edge`) — `x_for=2` returns the CDN
  edge. Everyone behind that PoP shares one bucket, and the bucket belongs to an address
  that changes underneath the limit.
- **Chain shrinks to one** — ProxyFix finds fewer values than it trusts, takes *nothing*,
  and `remote_addr` stays the internal peer. Every user on the platform lands in a single
  bucket and the global default limit becomes a site-wide lockout.

Neither raises, neither logs, and both are only visible as a support ticket. There is no N
that is safe against a chain whose length is not promised, because raising N to cover the
longer path is the same edit as trusting one more attacker-supplied value on the shorter
one.

#### What is stable, and why the leftmost value is trustworthy *here*

Railway's edge **overwrites** a client-supplied `X-Forwarded-For` and writes the real
connecting address as the first entry. That is stated by Railway support — *"We do strip
`X-Forwarded-For` at our edge and ensure clients cannot overwrite it"* — and it is what the
forged-header test showed directly: `1.2.3.4` never reached the app on any attempt. Under
both routing paths, position 0 is the client. Position 0 does not care how long the chain
is.

So the client IP is taken from the leftmost `X-Forwarded-For` value, by a small WSGI
wrapper in `app/proxy.py`, and `ProxyFix` is left to the scheme alone with `x_for=0`.

**The trust boundary, stated plainly:** trusting the leftmost value is safe **if and only
if** the ingress replaces the client-supplied header. Where it does not, the leftmost value
is a string the attacker typed, and every IP-keyed limit becomes bypassable one request at
a time. This is a *stronger* assumption about the platform than a hop count makes — a hop
count needs only that the trusted proxies append, not that they sanitise — and it is
accepted deliberately, because on Railway the weaker assumption is not available: there is
no fixed count to use.

That condition is therefore never assumed. `CLIENT_IP_SOURCE` is opt-in per environment and
defaults to `remote-addr`:

| `CLIENT_IP_SOURCE` | client IP comes from | correct where |
|---|---|---|
| `remote-addr` (default) | the socket peer; forwarded headers ignored entirely | nothing trusted is in front — local dev, the Compose stack |
| `xff-leftmost` | the first `X-Forwarded-For` value | the ingress overwrites the header — Railway |

`TRUSTED_PROXY_HOPS` is removed rather than kept as a third mode. It is dead configuration
for both environments this app runs in, and keeping a right-count option available invites
someone to reach for the model this section just rejected. If the app ever moves behind a
fixed, self-managed chain — an nginx or ALB whose hops are known — the enum above is where
that mode gets added back, with its own measurement.

A leftmost value that does not parse as an IP address is discarded and the socket peer is
used instead. `X-Forwarded-For` is allowed to carry `unknown`, obfuscated identifiers and
`host:port` pairs, none of which should become a rate-limit key; collapsing into the shared
peer bucket is the restrictive direction to fail.

#### `X-Real-IP` is deliberately not used

It was correct on every request tested, which is exactly what makes it dangerous. Railway
populates `X-Real-IP` with the **CDN edge address** rather than the client's whenever the
CDN path is active, and has acknowledged that as a bug on their side. The measurements above
were taken on requests where it happened to agree with the client. Keying rate limits on it
would work until the routing flipped, and then silently put every user behind a PoP into one
bucket — the same failure as a too-low hop count, arriving without a deploy.

### The protocol count is separate, and stays a count

`X-Forwarded-Proto` is a different question and must not inherit its answer from the
`X-Forwarded-For` chain length. A count is the right model here, for the reason it is the
wrong one above: ProxyFix reads this header from the **right**, where the nearest trusted
TLS terminator writes, and every layer in front of this app terminates TLS. `x_proto=1` is
correct whether the header carries one value or several, so chain-length variation cannot
move it. Railway sends exactly one value, `https`.

| variable | drives | Railway | local / Compose |
|---|---|---|---|
| `CLIENT_IP_SOURCE` | `app/proxy.py` — client IP, `X-Forwarded-For` | `xff-leftmost` | `remote-addr` |
| `TRUSTED_PROXY_PROTO_HOPS` | `ProxyFix(x_proto=…)` — scheme, `X-Forwarded-Proto` | `1` | `0` |

Overshooting the protocol count is worth naming because nothing at runtime reports it. At
`x_proto=2` against a one-value header ProxyFix takes nothing and leaves `wsgi.url_scheme`
as `http`. Flask-Talisman reads `X-Forwarded-Proto` from the raw request headers rather than
from the WSGI scheme, so `force_https` and HSTS keep working exactly as before and no page
breaks — the app just stops knowing it is on TLS. `request.is_secure` reads `False` on every
HTTPS request, and Flask-WTF runs its `WTF_CSRF_SSL_STRICT` referer check only
`if request.is_secure`. A defence-in-depth check switches itself off, with no error.

Both settings are required in production for the reason the first one already was: there is
no safe guess, and a refusal to start names the missing variable while a guess hides it
behind pages that look fine. The off values stay legal explicit answers — the Compose stack
sets `remote-addr`/`0`, since it runs `FLASK_ENV=production` with no proxy in front of it.
Each wrapper is installed only when its setting asks for it, so the local posture leaves
both out of the WSGI stack rather than installing no-ops.

`x_host`, `x_port` and `x_prefix` stay at ProxyFix's `0` default. `SERVER_NAME` is
configured explicitly (ADR-0006), so no forwarded host, port or prefix needs trusting, and
each one enabled would be another header to get the count right for.

### One definition of "the client"

The rewrite happens in the WSGI environ, so `request.remote_addr` *is* the client address
wherever the middleware is installed. That is what Flask-Limiter's `get_remote_address`
returns, and it is the single path by which all three IP-keyed sites in `extensions.py`
reach the client: the default `key_func`, the `user_key()` fallback for anonymous requests,
and the `pending_email_key()` fallback when no pending address is in the session. A grep for
`remote_addr` / `get_remote_address` across the app finds those three and nothing else — no
logging, audit or access-control path resolves the client independently.

Resolving the address at each call site instead would have left two definitions of "the
client" free to drift apart, and would have silently excluded whatever reaches for
`remote_addr` next.

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

- **`xff-leftmost` is a statement about the ingress, not about the app.** It is correct
  only while something in front replaces a client-supplied `X-Forwarded-For`. That holds on
  Railway today — stated by their support and confirmed live — but it is a platform
  property that can change without a deploy on this side, and if it ever did, IP rate
  limiting would become bypassable with no visible symptom. It is the assumption to re-test
  after any platform migration, and the reason the setting is per-environment rather than a
  default. It also assumes the app cannot be reached directly around the edge; that too is
  the platform's guarantee, not this app's.
- **The measurements are Railway's, at one point in time.** `tests/unit/test_proxy_trust.py`
  pins the *rules* — leftmost wins, chain length is irrelevant, no forwarded header is
  trusted at the default posture — rather than the two-element shape that happened to be on
  the wire, precisely because that shape moved between observations. Another platform needs
  its own measurement and quite possibly its own mode.
- **IPv6 clients are keyed on the full address.** A client with a routed prefix can rotate
  through addresses within it and mint a fresh bucket per request. Bucketing IPv6 by `/64`
  would close that; it is not done here and is not a regression from the previous design,
  which had the same property.
- **Fallback counters are per-process.** During a storage outage with N workers, the
  effective limit is up to N×. Degraded, not open.
- **Longer windows are untested at their boundaries.** In a same-minute burst the
  per-minute limit dominates, so the hourly and daily ceilings never get exercised without
  minutes of wall clock. They are registered and counting; the boundary behaviour is
  unproven.
- **Rate limiting does not bound total spend.** A patient caller within the limits still
  accumulates API cost over time. That is the quota's job, not this mechanism's.