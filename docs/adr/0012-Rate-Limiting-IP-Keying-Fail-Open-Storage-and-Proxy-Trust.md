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
and on Railway it is not. The next sections replace the count for the client IP — first by
position, on direct Railway, and then by a dedicated header once Cloudflare became the
public ingress — and keep it for the scheme.

### The client IP is not resolved by a hop count

`TRUSTED_PROXY_HOPS` originally fed both `ProxyFix(x_for=…)` and `ProxyFix(x_proto=…)`.
Two problems, found in that order: one count cannot serve two headers that carry different
numbers of values, and — the larger one — **a count from the right is the wrong model for
Railway at all**.

#### What direct Railway sent

Measured on the live service while it was reached on its platform-issued
`*.up.railway.app` hostname, with nothing in front of Railway, by logging the raw
forwarding headers, repeated across many requests, and compared against a request carrying
a client-supplied `X-Forwarded-For: 1.2.3.4`. The left-hand value was checked against an
independently measured public IPv4 address:

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

#### The interim answer: the leftmost value, on direct Railway

Railway's edge **overwrites** a client-supplied `X-Forwarded-For` and writes the address
that connected to it as the first entry — stated by Railway support (*"We do strip
`X-Forwarded-For` at our edge and ensure clients cannot overwrite it"*) and shown directly
by the forged-header test above. With nothing in front of Railway, the address that
connected to it *was* the client, under both routing paths, at position 0. That shipped as
`CLIENT_IP_SOURCE=xff-leftmost`, with its trust boundary stated as: safe if and only if the
ingress replaces the client-supplied header.

That condition was met by Railway's edge, and it stopped being *sufficient* the moment a
second ingress went in front of it. Position 0 is "whoever connected to Railway". Once
that is Cloudflare, position 0 is Cloudflare.

### The client IP is read from `CF-Connecting-IP` behind Cloudflare

Cloudflare now fronts the custom domain, in front of Railway. That changes what Railway
sees as its client, and so what every `X-Forwarded-For` position means, so the measurement
was repeated through the public ingress.

#### What the deployed service sends through Cloudflare

Measured live through `https://interview-intel.com`, repeated across requests, with the
same public-IP cross-check as before:

| observation | result |
|---|---|
| `CF-Connecting-IP` | the real client IP, on **every** request |
| `X-Forwarded-For` leftmost | **not** the real client IP — Railway now sees Cloudflare as its connecting client |
| forged `CF-Connecting-IP: 1.2.3.4`, sent through the custom domain | **rejected by Cloudflare with HTTP 403**; the request never reached the app |
| the platform-issued `*.up.railway.app` hostname, requested directly | Railway `404 Application not found` — no route to the app around Cloudflare |

#### Why `X-Forwarded-For` is not read at all

The leftmost value was the client only because Railway wrote the connecting address there
and nothing sat in front of Railway. With Cloudflare in front, the connecting address *is*
a Cloudflare edge. Keying on it would put every user behind a Cloudflare PoP into one
bucket, on an address that changes as Cloudflare routes — the too-low failure from the
first section, arriving without a deploy and with a healthy log.

No other position helps. Cloudflare documents that it *appends* the connecting address to
whatever `X-Forwarded-For` the client sent rather than replacing it, so counting from the
left trusts an attacker-typed prefix, and counting from the right is the fixed-chain-length
model already rejected. The header carries nothing this app can safely use. In
`cf-connecting-ip` mode it is therefore not consulted at any position — and there is
deliberately **no fallback to it** when `CF-Connecting-IP` is absent, because that fallback
would silently reintroduce exactly the edge-keying that was measured.

#### Why `CF-Connecting-IP` is trustworthy *here*, and where the trust ends

Cloudflare writes exactly one value — the address that connected to Cloudflare — and
refuses a request that arrives at its edge already carrying the header, which the
forged-header test showed directly as a 403. Under that ingress the header *is* the
client: `app/proxy.py` rewrites `REMOTE_ADDR` from it, and `ProxyFix` is left to the
scheme alone with `x_for=0`.

**The trust boundary, stated plainly:** `CF-Connecting-IP` is meaningful **only on a
request that actually came through Cloudflare** — Cloudflare's own guidance says as much.
On a request that reached Railway some other way, the header is whatever the sender typed.
Everything that stops such a request from existing happens *before* the app: Cloudflare's
403 at its edge, and the absence of any route to Railway that skips Cloudflare. The
platform hostname answering 404 is what makes the second condition true today, and it is
evidence about the platform as it is now, not a guarantee about the platform as it will
be: a re-enabled hostname or a stray DNS record would create the route with no deploy and
no symptom. So the app does not take passage through Cloudflare on faith. It verifies it
per request, as the next section describes, and a request that cannot prove it keeps the
socket peer.

The mode is still opt-in and never a default. `CLIENT_IP_SOURCE` is set per environment
and defaults to `remote-addr`:

| `CLIENT_IP_SOURCE` | client IP comes from | correct where |
|---|---|---|
| `remote-addr` (default) | the socket peer; forwarded headers ignored entirely | nothing trusted is in front — local dev, the Compose stack |
| `cf-connecting-ip` | the `CF-Connecting-IP` header, on requests carrying the origin secret | Cloudflare is the public ingress and sets the secret — production |

A value that is missing, or does not parse as exactly one bare IP address — a
comma-separated list, `host:port`, `unknown`, an IPv6 zone ID — is discarded and the
socket peer is used instead. Cloudflare produces none of those, so none should become a
rate-limit key; collapsing into the shared peer bucket is the restrictive direction to
fail. A missing header is the expected shape for a request that never crossed Cloudflare,
such as the container healthcheck. The value is parsed and normalised rather than passed
through as text, so `2001:DB8::1` and `2001:db8::1` cannot be two buckets.

#### Origin authentication: the request proves it came through Cloudflare

A shared secret, `CF_ORIGIN_SECRET`, is known to exactly two parties. Cloudflare presents
it on every request it forwards, by a Request Header Transform rule that *sets* the
private header `X-Interview-Intel-Origin` to the secret — *set*, not add-if-missing, so a
value a client put there is overwritten rather than passed through. The app holds the same
value from the environment. In `cf-connecting-ip` mode the middleware trusts
`CF-Connecting-IP` only when that header is present and equal to the secret; otherwise the
request is treated as having reached the origin around Cloudflare, `REMOTE_ADDR` stays the
socket peer, and a warning names the fallback so a missing rule or an unexpected route is
noticed rather than discovered as a rate-limit ticket. The presented values are not in
that warning: one is attacker-chosen text, the other may be most of the secret.

The comparison is `hmac.compare_digest` over bytes, never `==`. A plain comparison returns
at the first differing byte, and the timing difference is measurable across enough
requests to recover the secret one byte at a time — and recovering the secret is
precisely the attack this check has to survive, since the rate limits it protects are the
thing that would otherwise slow the guessing down. The header name is deliberately
app-specific rather than a generic `X-Origin-Verify`, so a rule copied from another zone
cannot satisfy it by accident.

What this changes about the boundary: the residual trust moves from the *network path* to
the *secret*. The app no longer relies on there being no route to Railway that skips
Cloudflare; it relies on the secret being known to Cloudflare and itself alone. That is a
much better thing to rely on. It is under this project's control — the value is generated
here and placed in two places by hand — where the absence of a route is a property of two
platforms' configurations that can change independently of anything in this repository.
The direct-hostname 404 becomes defence in depth rather than the load-bearing assumption.

What it does not change: a request that *does* carry the secret is trusted as delivered.
The app cannot tell a value Cloudflare wrote from one written by whoever else holds the
secret, and does not try. `tests/unit/test_proxy_trust.py` pins both sides of that line —
without the secret a forged `CF-Connecting-IP` is ignored; with it, it is the client — so
no one reads more into the check than is there. The secret is therefore handled like the
other production secrets: required at boot in the mode that needs it — and at least 32
characters there, since a guessable secret is the missing one with a quieter failure —
never committed
(`.env*` is ignored; the examples ship the variable commented out with no value), never
logged, and never included in an error message. Rotation is safe in either order: while
the two copies disagree, every forwarded request fails the check and falls back to the
peer — the restrictive direction, and the warning above says so on every request until
they agree again.

Two remaining properties are worth stating. The check is a *gate*, not a source: a valid
secret on a request with no `CF-Connecting-IP` resolves to the peer, exactly as before.
And the restrictive fallback has a cost when it is the *rule* that is wrong rather than
the request — if the transform rule is removed or mis-set, every user collapses into the
peer bucket and the global default limit becomes a site-wide lockout. That is the too-low
failure from the first section, chosen deliberately over the too-high one, and it is why
the fallback logs rather than staying silent.

#### `xff-leftmost` is removed, not kept as a mode

For the reason `TRUSTED_PROXY_HOPS` was: it is dead configuration for both environments
this app runs in, and keeping it available invites someone to reach for the model this
section just rejected. Behind Cloudflare it is not merely dead but wrong — it keys limits
on Cloudflare's edges. A pre-Cloudflare environment still carrying the value fails at boot
with a message that names `cf-connecting-ip`, rather than being mapped to anything
silently. If Cloudflare is ever removed and the app is again reached on Railway directly,
the enum is where the position-0 mode gets added back, with the direct-Railway
measurement above re-run first, since it is Railway's edge behaviour that made it correct.
The implementation is in this repository's history.

#### `X-Real-IP` is deliberately not used

On direct Railway it was correct on every request tested, which is exactly what made it
dangerous: Railway populates `X-Real-IP` with the **CDN edge address** rather than the
client's whenever the CDN path is active, and has acknowledged that as a bug on their
side. Keying rate limits on it would work until the routing flipped, and then silently put
every user behind a PoP into one bucket — the same failure as a too-low hop count,
arriving without a deploy. It was not re-measured through Cloudflare, and does not need to
be: it can only carry whatever Railway writes for *its* connecting client — a Cloudflare
edge, or Railway's own CDN edge — and neither is the user. It is not read in any mode, and
it is not a fallback when `CF-Connecting-IP` is absent.

### The protocol count is separate, and stays a count

`X-Forwarded-Proto` is a different question and must not inherit its answer from the
client-IP setting. A count is the right model here, for the reason it is the wrong one
above: ProxyFix reads this header from the **right**, where the nearest trusted TLS
terminator writes, and every layer in front of this app terminates TLS. `x_proto=1` is
correct whether the header carries one value or several, so neither Railway's chain-length
variation nor Cloudflare adding a hop in front of Railway's terminator can move it. Direct
Railway sent exactly one value, `https`; whether Railway appends to or overwrites what
Cloudflare sends, the rightmost value is still Railway's, so the count is unchanged at 1.

| variable | drives | production (Cloudflare → Railway) | local / Compose |
|---|---|---|---|
| `CLIENT_IP_SOURCE` | `app/proxy.py` — client IP, `CF-Connecting-IP` | `cf-connecting-ip` | `remote-addr` |
| `CF_ORIGIN_SECRET` | `app/proxy.py` — proof of passage, `X-Interview-Intel-Origin` | the value the Cloudflare rule sets | unset; never read |
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
behind pages that look fine. `CF_ORIGIN_SECRET` is required only when `CLIENT_IP_SOURCE`
is `cf-connecting-ip` — the mode is unusable without it, and `remote-addr` never reads it.
The off values stay legal explicit answers — the Compose stack sets `remote-addr`/`0` and
no secret, since it runs `FLASK_ENV=production` with no proxy in front of it. Each wrapper
is installed only when its setting asks for it, so the local posture leaves both out of the
WSGI stack rather than installing no-ops.

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

- **`cf-connecting-ip` is a statement about the secret, not about the app.** It is correct
  while `CF_ORIGIN_SECRET` is known to Cloudflare and this app alone. Anyone else who
  holds it can reach the origin around Cloudflare — if such a route ever exists — and
  choose their own rate-limit bucket per request, with no visible symptom. The secret
  lives in two places, the Railway environment and the Cloudflare rule, and both are
  readable by anyone with dashboard access to either; treat access to those as access to
  the secret, and rotate it on any suspicion. The direct-hostname 404 and Cloudflare's
  403 on a client-supplied `CF-Connecting-IP` are still worth re-testing after any change
  to DNS, Cloudflare, or Railway domains — they are the defence in depth — but they are no
  longer what the guarantee rests on.
- **The transform rule is the other half of the check, and it lives outside this repo.**
  If it is removed, renamed, or set to a stale value, nothing here fails at boot: every
  forwarded request fails the check, every user shares the peer bucket, and the global
  default limit becomes a site-wide lockout. The middleware warns on every such request,
  which is the signal to look for. This is the deliberate failure direction, chosen over
  the one where a broken rule makes forgery possible.
- **The measurements are one point in time.** The direct-Railway table and the
  through-Cloudflare table were each taken once, on the platforms as they were then.
  `tests/unit/test_proxy_trust.py` pins the *rules* — `CF-Connecting-IP` is the client
  only alongside the origin secret, the secret is compared in constant time and appears in
  no log, `X-Forwarded-For` is read at no position, `X-Real-IP` is read never, the scheme
  is independent of the client-IP setting, nothing is trusted and no secret is needed at
  the default posture — rather than any header shape that happened to be on the wire,
  because those shapes moved between observations. Another ingress needs its own
  measurement and quite possibly its own mode.
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