# 0017. Content Security Policy and Security Headers via Flask-Talisman

## Status

Accepted

## Context

`create_app()` set three security headers by hand in an `after_request` —
`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` — with a TODO above it to
replace the block with Flask-Talisman. A hand-rolled block cannot express a Content
Security Policy or HSTS, which are the two headers that actually change the application's
exposure.

This is the last hardening pass before deployment, and the project is not expected to be
maintained afterwards. That constraint shaped every decision below: anything deferred to
"a later phase" would in practice never happen, and anything left to a library default
could move under a future upgrade with nobody watching.

The risk in a CSP is that it fails *silently*. A directive that is too tight produces no
server-side error and no failed request the user notices — just a font that falls back to
a system serif, or a filter button that stops responding. So the policy was derived by
inventorying what the templates actually reference, not from a template policy.

That inventory found more inline JavaScript than the TODO described:

| Construct | Count | Can a nonce authorize it? |
|---|---|---|
| Inline `<script>` blocks | 4 | Yes |
| Inline `on*=` handlers | 12 | **No** |
| `javascript:` URL | 1 | **No** |
| Inline `<style>` blocks | 11 | Yes |
| `style=""` attributes (served) | 100 | **Never** |
| `style=""` attributes (`templates/email/`) | 41 | n/a — not served over HTTP |

Two CSP rules govern what follows, and both are easy to get backwards:

1. **A nonce or hash in a directive makes browsers ignore `'unsafe-inline'` in that same
   directive.** So the four `<script>` blocks cannot be nonced while twelve `on*=`
   handlers still depend on `'unsafe-inline'`. It is one or the other, per directive.
2. **Nonces apply to `<style>` elements, never to `style=""` attributes.** The TODO's
   instruction to "add nonce to all inline `<script>` and `<style>` blocks" would, if
   followed, have disabled `'unsafe-inline'` and broken all 100 style attributes at once.

## Decision

Adopt Flask-Talisman, remove **all** inline JavaScript so `script-src` can be exactly
`'self'`, and keep `'unsafe-inline'` in `style-src`.

### script-src is `'self'` — no `'unsafe-inline'`, and no nonce either

All 17 inline-JS constructs were removed rather than authorized:

- Four `<script>` blocks moved to `static/js/` (`tasks-filter.js`, `messages-filter.js`,
  `new-message-form.js`, `edit-note.js`), matching the existing per-feature convention of
  `analysis-poll.js`, `notifications.js` and `hero-animation.js`.
- Four `onclick` filter arguments became `data-filter` attributes.
- Two `onchange` handlers became `change` listeners bound by element id.
- Six `onsubmit="return confirm(...)"` became `data-confirm` attributes driven by one
  delegated listener.
- `href="javascript:history.back()"` became a real `href` plus `data-history-back`.

Because nothing inline survives, **no nonce is needed**. `content_security_policy_nonce_in`
is not set at all. A nonce would authorize nothing, and would add a per-request value to a
header for no benefit.

The remaining external origins are Google Fonts, which needs two directives, not one: the
stylesheet from `fonts.googleapis.com` (`style-src`) and the font files it `@font-face`s
to from `fonts.gstatic.com` (`font-src`). Omitting the second fails only as a fallback
typeface, with nothing in the console pointing at the cause.

GSAP and ScrollTrigger are vendored under `static/js/vendor/`, not loaded from a CDN, so
`script-src 'self'` covers them. The CSS contains no `url()`, no `data:` URIs and no
`@font-face`, so `img-src 'self'` needs no `data:` and `object-src` can be `'none'`.

### style-src keeps `'unsafe-inline'`, deliberately and permanently

Removing 100 `style=""` attributes across ~20 templates would touch far more surface than
the rest of this change combined, with visual regression risk on every page, and it was
scoped out. All 100 are static — none are Jinja-computed — so the work is mechanical if
it is ever wanted.

This is a much smaller concession than the script one. `style-src 'unsafe-inline'`
permits CSS-based data exfiltration and UI redressing; it cannot execute JavaScript. With
`script-src 'self'`, an injected `<script>` or `onerror=` payload does not run.

### The delete confirmation is delegated, capture-phase, and registered first

`onsubmit="return confirm(...)"` guarded six destructive actions. Replacing it with a
direct `querySelectorAll` bind would fail **open** — deleting with no prompt — in three
ways this codebase actually exhibits: a throw anywhere earlier in `main.js` skips the
registration; `notifications.js` appends server-rendered HTML after load; and a
form-level `stopPropagation` would suppress a bubbling listener.

The handler is therefore delegated from `document`, registered in the capture phase, as
the first statement in `main.js`. The other blocks in that file keep their immediate
queries: they run at end-of-body where the DOM exists, and a dead button is visible in a
way a missing confirmation is not.

### One flag gates everything HTTPS-dependent

`TALISMAN_HTTPS` (default off, opt-in by env) gates `force_https`, HSTS, and the `Secure`
flag on the session cookie together. They all answer the same question, and splitting
them invites a deploy that redirects to HTTPS while issuing cookies that never come back.

It is not keyed on `FLASK_ENV`, because the compose stack in this repo runs
`FLASK_ENV=production` over plain HTTP (`SERVER_NAME=localhost:8000`) — deriving it would
break `docker compose up`. Only the operator knows whether TLS is terminated.

The **CSP is not gated**: it applies in every environment, so a violation surfaces in the
local console rather than on the deployed site.

### HSTS ships at its final value, not ramped

`max-age=31536000` on the first deploy, `includeSubDomains` off, `preload` off.

The usual advice — start at a day, raise after a week — protects a rollback to HTTP. This
deployment will not be reconfigured, so the ramp guards a change that will never happen
while permanently weakening the header, and it would leave behind a maintenance task
nobody is going to perform. `includeSubDomains` (the clause causing collateral damage to
sibling hostnames) and `preload` (baked into browser binaries) both stay off, so the
blast radius is exactly one hostname.

`TALISMAN_HSTS_MAX_AGE=0` is documented as the way to clear the policy, but it is a weak
escape hatch, not a rollback plan: the browser only honors it if it can still complete a
valid HTTPS handshake to receive the header. In the case one would most want it — an
expired certificate — the connection is refused before any header is read. Hence the
verification gate below, which confirms TLS *before* HSTS ever reaches a browser.

### `/healthz` is exempted from the HTTPS redirect

`compose.yaml` probes `http://127.0.0.1:8000/healthz` inside the network with no
`X-Forwarded-Proto`. Under `force_https`, Talisman returns a 302 to `https://…`;
`urllib.request.urlopen` follows redirects, then attempts a TLS handshake against a
plain-HTTP gunicorn socket and raises — the `sys.exit(0 if …)` never evaluates, and the
container is marked unhealthy.

Both defenses are in place: `TALISMAN_HTTPS` defaults off so the compose profile is safe
today, and `@talisman(force_https=False)` on the view keeps it safe when HTTPS is later
turned on behind a proxy. The decorator is the load-bearing one; the default alone would
leave a trap for whoever flips the flag.

### Talisman is instantiated per app, not as a module-level singleton

This is the one place the standard Flask extension pattern is wrong. Unlike
`SQLAlchemy()` or `CSRFProtect()`, `Talisman.init_app()` stores every option on the
**instance** and ends with `self.app = app`; `_force_https()` then reads `self.app.debug`
and *writes* `self.app.config['SESSION_COOKIE_SECURE']`.

A shared singleton initialized by two apps in one process — which the test suite does
routinely — would let the second `init_app()` overwrite the first's settings, and a
request to one app would read and mutate the other's config. `create_app()` therefore
builds its own `Talisman(app, …)`.

The module-level instance in `extensions.py` remains solely as a decorator factory, which
is safe because `Talisman.__call__` is a pure marker: it does
`setattr(view, 'talisman_view_options', kwargs)`, and `_get_local_options()` reads it back
off `current_app.view_functions`, so the marker is honored by whichever per-app instance
serves the request.

### Every option is passed explicitly

Including ones matching the library default — `force_https_permanent=False`,
`referrer_policy`, `x_content_type_options`, `session_cookie_samesite="Lax"`,
`permissions_policy={"browsing-topics": "()"}`. On an unmaintained project, an inherited
default is a behavior that can change under a version bump with nobody watching. Pinning
them means an upgrade cannot move what this application sends, and the header tests fail
loudly if it tries.

Two of these were nearly missed by reasoning from memory instead of source:
Talisman 1.1.0 defaults `permissions_policy` to `{'browsing-topics': '()'}` (so a test
asserting the header's *absence* would have failed on first run), and
`DEFAULT_SESSION_COOKIE_SAMESITE` is already `"Lax"`.

## Consequences

- **`script-src 'self'` is a standing constraint, not a one-time cleanup.** One inline
  handler added later silently disables that page's JavaScript, with nothing failing
  server-side. `test_no_inline_js_in_templates` fails the build if one reappears; it
  distinguishes `<script src>` from an inline block and reports `file:line`. A companion
  test plants known violations in a temp directory to prove the scanner still fires —
  a scanner that silently stops matching is worse than none.
- **The CSP is pinned exactly, not loosely.** `test_script_src_is_exactly_self` asserts
  the string `'self'` rather than the absence of `'unsafe-inline'`, because asserting
  what is missing cannot catch a source that was *added* — a CDN host, `'unsafe-eval'`,
  `'strict-dynamic'`. `test_full_csp_matches_expected` pins every directive by token set.
- **`style-src 'unsafe-inline'` is permanent in practice.** It should not be read as a
  temporary state; without the 100-attribute extraction it cannot be removed, and a nonce
  is not an alternative.
- **HSTS is a one-way door for a year.** Once a browser sees the header, that hostname is
  HTTPS-only for `max-age`, and a certificate failure cannot be recovered by shipping a
  fix. This is accepted in exchange for not leaving a ramp nobody will perform, and
  bounded by the pre-traffic verification gate and by keeping `includeSubDomains` and
  `preload` off.
- **Local development is unaffected, by design.** With `TALISMAN_HTTPS` off there is no
  redirect, no HSTS, and no `Secure` cookie. The last of those is the subtle one: with
  `Secure` set over plain HTTP, login appears to succeed and the next request is
  anonymous, with no error anywhere. Talisman also disables `force_https` outright when
  `app.debug`, which is a second safety net in `DevelopmentConfig`.
- **`TestingConfig` pins `TALISMAN_HTTPS = False`** rather than inheriting it, so an
  exported environment variable cannot turn every test client request into a 301 or drop
  the session in login-flow tests.
- **Talisman mutates `talisman_view_options` on the decorated view.** `_get_local_options`
  calls `setdefault` on the dict stored on the view function, baking the first-serving
  instance's CSP and frame options onto it. Only `/healthz` is decorated and no test
  asserts headers on it, so this is inert here — but a second decorated route would
  inherit whichever app answered first in the same process.
- **The hand-rolled `after_request` block is gone.** Talisman reproduces all three headers
  it set, now pinned explicitly, and `test_legacy_headers_still_set` asserts their values
  so the replacement is provably equivalent rather than assumed to be.

## Verification

```bash
# Unit suite — 14 security tests, 350 total
FLASK_ENV=testing .venv/bin/pytest -m "not integration" -q

# Dev server: CSP present, no HSTS
curl -sI http://localhost:5001/ | grep -i -e content-security -e strict-transport

# Docker: web must report healthy, and the probe's exact code path must succeed
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker ps
docker compose exec web python -c \
  "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3); print(r.status, r.read())"

# Deployment, BEFORE pointing traffic at it — the last reversible moment
curl -sI https://<host>/          # 200, valid cert
curl -sI http://<host>/           # 302 -> https
curl -sI https://<host>/ | grep -i strict-transport
openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -subject
```

Browser checks that no test can replace: the console must be clean on every page that
loads something distinct (home with GSAP, a message detail with the poller, the demo
pages, notifications); `fonts.gstatic.com` must succeed in the Network tab with computed
`font-family` resolving to Inter rather than the fallback; and all six delete
confirmations must be exercised twice each — accepting *and* cancelling, verifying the row
survives the cancel.
