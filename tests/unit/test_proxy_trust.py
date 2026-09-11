"""
Reverse-proxy trust regression tests.

Two independent questions, tested independently because they fail
independently and both fail SILENTLY in production:

* **Which address is the client?** Read from CF-Connecting-IP, on requests that
  prove they came through Cloudflare. Get it wrong in one direction and every
  user shares a rate-limit bucket, so the global default becomes a site-wide
  lockout; wrong in the other and any client mints a fresh bucket per request
  and IP limits stop existing. Neither raises.
* **Was this request on TLS?** Read from X-Forwarded-Proto. Get it wrong and
  request.is_secure reads False on HTTPS traffic, which switches off Flask-WTF's
  WTF_CSRF_SSL_STRICT referer check while every page keeps working.

These tests pin the RULES that the live measurement through Cloudflare
established (ADR-0012), not the header shape that happened to be on the wire:
CF-Connecting-IP is the client, but only alongside the origin secret Cloudflare
sets in X-Interview-Intel-Origin; X-Forwarded-For is never consulted, at any
position, because through Cloudflare its leftmost value is a Cloudflare edge;
X-Real-IP is never consulted; the secret is compared in constant time and never
logged; the scheme does not depend on the client-IP setting; and at the default
posture no forwarded header is trusted and no secret is needed.
"""

import importlib.util
import logging
import uuid
from pathlib import Path

import pytest
from flask import request
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from app import proxy as proxy_module
from app.extensions import pending_email_key, user_key
from app.proxy import CfConnectingIp

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.py"

CLIENT_IP = "203.0.113.7"
CLIENT_IPV6 = "2001:db8:85a3::8a2e:370:7334"

# What Railway sees as its connecting client once Cloudflare is in front: a
# Cloudflare edge. This is the address that the leftmost X-Forwarded-For value
# carried live, and the reason that header is no longer read.
CF_EDGE = "198.51.100.1"

# Railway's own intermediaries, still present further right in the chain.
RAILWAY_EDGE = "192.0.2.1"
INTERNAL = "100.64.0.9"

# What gunicorn's socket sees behind a proxy: the proxy, never the client.
DIRECT_PEER = "100.64.0.2"

# A value a client puts in a forwarding header itself. Cloudflare rejected a
# client-supplied CF-Connecting-IP with a 403 at its edge — verified live — but
# the app must not be the thing relying on that: the origin secret is what
# tells it whether Cloudflare was on the path at all.
FORGED_IP = "1.2.3.4"

# The shared secret Cloudflare's transform rule sets in X-Interview-Intel-Origin.
# Nothing about its shape matters to the middleware; it only has to match
# exactly. Its length matters to ProductionConfig, which refuses anything under
# CF_ORIGIN_SECRET_MIN_LENGTH, so this one is comfortably over.
SECRET = "test-origin-secret-9f2c1d7a4b8e6c3d5f7a9b0c1d2e3f"

ORIGIN_HEADER = CfConnectingIp.ORIGIN_HEADER


# ── harness ─────────────────────────────────────────────────────────

def _app(monkeypatch, *, source="cf-connecting-ip", proto_hops=1, https=False,
         origin_secret=SECRET):
    """A fresh app at the given trust posture, with a probe route."""
    from app import create_app

    monkeypatch.setattr(config.TestingConfig, "CLIENT_IP_SOURCE", source)
    monkeypatch.setattr(config.TestingConfig, "CF_ORIGIN_SECRET", origin_secret)
    monkeypatch.setattr(config.TestingConfig, "TRUSTED_PROXY_PROTO_HOPS", proto_hops)
    monkeypatch.setattr(config.TestingConfig, "TALISMAN_HTTPS", https)
    application = create_app()

    # Registered before the first request, so Flask still allows setup. Returns a
    # dict rather than a template, so nothing touches the database.
    @application.route("/_proxy_probe")
    def _proxy_probe():
        return {
            "remote_addr": request.remote_addr,
            "scheme": request.scheme,
            "is_secure": request.is_secure,
            # The three IP-keyed paths in extensions.py, exercised for real
            # rather than asserted about by inspection. user_key and
            # pending_email_key both fall back to the client IP for an
            # anonymous request with no pending address in the session.
            "limiter_key": get_remote_address(),
            "user_key": user_key(),
            "pending_email_key": pending_email_key(),
        }

    return application


def _get(application, cf=None, origin=None, xff=None, xfp=None, xri=None):
    """
    Issue a request shaped like one arriving at the origin. With no `origin`
    it is a request that did NOT come through Cloudflare, whatever else it
    carries.
    """
    headers = {}
    if cf is not None:
        headers["CF-Connecting-IP"] = cf
    if origin is not None:
        headers[ORIGIN_HEADER] = origin
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    if xfp is not None:
        headers["X-Forwarded-Proto"] = xfp
    if xri is not None:
        headers["X-Real-IP"] = xri

    return application.test_client().get(
        "/_proxy_probe",
        headers=headers,
        environ_base={"REMOTE_ADDR": DIRECT_PEER},
    ).get_json()


def _through_cloudflare(application, **kwargs):
    """A request shaped like one Cloudflare forwarded: it carries the secret."""
    return _get(application, origin=SECRET, **kwargs)


# The X-Forwarded-For shapes that can arrive through Cloudflare → Railway. In
# none of them is the client at position 0 — the live measurement — and the app
# must not care which of them arrives, because it must not read the header at
# all.
CLOUDFLARE_XFF_CHAINS = [
    pytest.param(CF_EDGE, id="one-value-cf-edge"),
    pytest.param(f"{CF_EDGE}, {RAILWAY_EDGE}", id="cf-edge-then-railway-edge"),
    pytest.param(f"{CF_EDGE}, {RAILWAY_EDGE}, {INTERNAL}", id="three-values"),
    # Cloudflare appends the client to whatever the client sent, so a chain
    # can also carry the client at some position other than 0. Still not read.
    pytest.param(f"{FORGED_IP}, {CLIENT_IP}, {CF_EDGE}", id="client-mid-chain"),
]


# ── the client IP: CF-Connecting-IP, with the origin secret ─────────

def test_cf_connecting_ip_is_the_client_through_cloudflare(monkeypatch):
    """The core property: with the secret present, the header Cloudflare writes is the client."""
    body = _through_cloudflare(_app(monkeypatch), cf=CLIENT_IP, xfp="https")

    assert body["remote_addr"] == CLIENT_IP


def test_an_ipv6_client_is_resolved_the_same_way(monkeypatch):
    body = _through_cloudflare(_app(monkeypatch), cf=CLIENT_IPV6, xfp="https")

    assert body["remote_addr"] == CLIENT_IPV6
    assert body["limiter_key"] == CLIENT_IPV6


def test_every_ip_keyed_path_sees_the_client(monkeypatch):
    """
    Fixing the limiter's default key while leaving another security-sensitive
    path on the proxy's address would be a bug with no symptom. All three sites
    in extensions.py resolve the client through request.remote_addr, so all
    three are asserted here.
    """
    body = _through_cloudflare(_app(monkeypatch), cf=CLIENT_IP, xfp="https")

    assert body["limiter_key"] == CLIENT_IP
    assert body["user_key"] == f"ip:{CLIENT_IP}"
    assert body["pending_email_key"] == f"ip:{CLIENT_IP}"


@pytest.mark.parametrize("xff", CLOUDFLARE_XFF_CHAINS)
def test_xff_is_never_consulted_when_the_header_is_present(monkeypatch, xff):
    """
    What was observed live: through Cloudflare the leftmost X-Forwarded-For
    value is NOT the client. Whatever the chain looks like, CF-Connecting-IP
    wins and the chain is not read.
    """
    body = _through_cloudflare(_app(monkeypatch), cf=CLIENT_IP, xff=xff, xfp="https")

    assert body["remote_addr"] == CLIENT_IP
    assert body["remote_addr"] != CF_EDGE


@pytest.mark.parametrize("xff", CLOUDFLARE_XFF_CHAINS)
def test_xff_is_never_consulted_when_the_header_is_absent(monkeypatch, xff):
    """
    The stronger half of the rule. With no CF-Connecting-IP there is no fallback
    to X-Forwarded-For — not to its leftmost value, not to any position, and
    the secret being present does not change that. The request collapses into
    the peer bucket instead. A fallback to XFF here would reintroduce,
    silently, exactly the Cloudflare-edge keying that was measured.
    """
    body = _through_cloudflare(_app(monkeypatch), xff=xff, xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER


def test_the_same_client_keeps_one_bucket_whatever_xff_carries(monkeypatch):
    """
    Stated as the property that actually matters to rate limiting: a user whose
    requests arrive with different X-Forwarded-For chains must not be split
    across buckets, and must not be merged with everyone else behind an edge.
    """
    application = _app(monkeypatch)

    keys = {
        _through_cloudflare(application, cf=CLIENT_IP, xff=xff.values[0], xfp="https")["limiter_key"]
        for xff in CLOUDFLARE_XFF_CHAINS
    }

    assert keys == {CLIENT_IP}


def test_x_real_ip_is_never_consulted(monkeypatch):
    """
    Railway populates X-Real-IP with the CDN edge address whenever its CDN path
    is active — an acknowledged bug on their side — and through Cloudflare the
    "client" Railway sees is a Cloudflare edge anyway. This pins that it is not
    read at all: CF-Connecting-IP wins when both are present, and X-Real-IP is
    not a fallback when CF-Connecting-IP is absent, secret or no secret.
    """
    application = _app(monkeypatch)

    both = _through_cloudflare(application, cf=CLIENT_IP, xfp="https", xri=CF_EDGE)
    assert both["remote_addr"] == CLIENT_IP

    only_xri = _through_cloudflare(application, xfp="https", xri=CLIENT_IP)
    assert only_xri["remote_addr"] == DIRECT_PEER


def test_proxyfix_is_not_allowed_to_touch_the_client_address(monkeypatch):
    """
    ProxyFix stays in the stack for the scheme, but with x_for=0 so it cannot
    reintroduce a right-hand X-Forwarded-For count for the client address by
    accident.
    """
    application = _app(monkeypatch, source="remote-addr", proto_hops=1)

    assert isinstance(application.wsgi_app, ProxyFix)
    assert application.wsgi_app.x_for == 0

    # A chain long enough that any nonzero x_for would have rewritten something.
    body = _get(application, xff=f"{CLIENT_IP}, {CF_EDGE}, {INTERNAL}", xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["scheme"] == "https"


# ── the origin secret: proof the request came through Cloudflare ────

def test_without_the_origin_header_cf_connecting_ip_is_not_trusted(monkeypatch):
    """
    A request carrying CF-Connecting-IP but no X-Interview-Intel-Origin did not
    come through Cloudflare — or the transform rule is missing. Either way the
    address header is unproven and the request keeps the socket peer.
    """
    body = _get(_app(monkeypatch), cf=CLIENT_IP, xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER


@pytest.mark.parametrize(
    "wrong",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("not-the-secret", id="different"),
        pytest.param(SECRET[:-1], id="one-short"),
        pytest.param(SECRET + "x", id="one-long"),
        pytest.param(SECRET.upper(), id="case-changed"),
        pytest.param(SECRET[1:] + SECRET[0], id="rotated"),
    ],
)
def test_with_the_wrong_origin_secret_cf_connecting_ip_is_not_trusted(monkeypatch, wrong):
    """Near-misses are misses. Only an exact match proves passage through Cloudflare."""
    body = _get(_app(monkeypatch), cf=CLIENT_IP, origin=wrong, xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER


def test_an_attacker_reaching_the_origin_cannot_choose_a_bucket(monkeypatch):
    """
    The attack the secret exists to stop. Someone who finds a route to Railway
    that skips Cloudflare — the platform hostname answering again, a stray DNS
    record — sends CF-Connecting-IP themselves, hoping to mint a fresh
    rate-limit bucket per request. Without the secret they cannot, whether
    they omit the origin header or guess at it, and whether the forged address
    is IPv4 or IPv6.
    """
    application = _app(monkeypatch)

    for forged in (FORGED_IP, "2001:db8::bad"):
        omitted = _get(application, cf=forged, xfp="https")
        guessed = _get(application, cf=forged, origin="guess", xfp="https")

        for body in (omitted, guessed):
            assert body["remote_addr"] == DIRECT_PEER, forged
            assert body["limiter_key"] == DIRECT_PEER, forged
            assert body["remote_addr"] != forged


def test_the_origin_header_alone_does_not_change_the_client(monkeypatch):
    """
    The secret proves the path; it is not itself an address source. A request
    with a valid secret and no CF-Connecting-IP resolves to the peer, as the
    Cloudflare posture run locally would.
    """
    body = _through_cloudflare(_app(monkeypatch), xfp="https")

    assert body["remote_addr"] == DIRECT_PEER


def test_surrounding_whitespace_on_the_secret_is_tolerated(monkeypatch):
    """A trailing newline pasted into a platform UI must not fail every request."""
    application = _app(monkeypatch, origin_secret=f"  {SECRET}\n")

    body = _get(application, cf=CLIENT_IP, origin=f" {SECRET} ", xfp="https")

    assert body["remote_addr"] == CLIENT_IP


def test_the_secret_is_compared_in_constant_time(monkeypatch):
    """
    Pinned so nobody 'simplifies' the check to `==`. A plain comparison returns
    at the first differing byte, which is measurable across enough requests.
    The spy delegates to the real compare_digest so the assertion below is on
    the real answer, not a stub's.
    """
    calls = []
    real = proxy_module.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(proxy_module, "compare_digest", spy)

    application = _app(monkeypatch)
    _get(application, cf=CLIENT_IP, origin="not-the-secret", xfp="https")
    _through_cloudflare(application, cf=CLIENT_IP, xfp="https")

    assert len(calls) == 2
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)
    assert calls[1] == (SECRET.encode(), SECRET.encode())


def test_the_secret_never_appears_in_logs(monkeypatch, caplog):
    """
    The secret must not be committed or logged. Boot in the Cloudflare posture
    and exercise every branch of the check — valid, missing, wrong — at DEBUG
    level, then assert neither the secret nor the value a request presented
    made it into any record. The wrong value is checked too: a near-miss of
    the secret in a log line is most of the secret.
    """
    near_miss = SECRET[:-2] + "zz"

    with caplog.at_level(logging.DEBUG):
        application = _app(monkeypatch)
        _through_cloudflare(application, cf=CLIENT_IP, xfp="https")
        _get(application, cf=CLIENT_IP, xfp="https")
        _get(application, cf=CLIENT_IP, origin=near_miss, xfp="https")

    rendered = "\n".join(record.getMessage() for record in caplog.records)

    assert SECRET not in rendered
    assert near_miss not in rendered
    assert SECRET not in caplog.text
    assert near_miss not in caplog.text


def test_an_unproven_address_header_is_reported_without_its_values(monkeypatch, caplog):
    """
    The one thing the middleware does say: an address header arrived without
    proof, and which peer it fell back to. Enough to notice a missing transform
    rule or a route around Cloudflare, without echoing what was presented.
    """
    with caplog.at_level(logging.WARNING, logger="app.proxy"):
        _get(_app(monkeypatch), cf=FORGED_IP, origin="guess", xfp="https")

    warnings = [r for r in caplog.records if r.name == "app.proxy" and r.levelno == logging.WARNING]

    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert ORIGIN_HEADER in message
    assert DIRECT_PEER in message
    assert FORGED_IP not in message
    assert "guess" not in message


def test_a_healthcheck_with_no_headers_is_not_reported(monkeypatch, caplog):
    """The container healthcheck never crossed Cloudflare and carries nothing; that is normal."""
    with caplog.at_level(logging.WARNING, logger="app.proxy"):
        body = _get(_app(monkeypatch))

    assert body["remote_addr"] == DIRECT_PEER
    assert not [r for r in caplog.records if r.name == "app.proxy"]


def test_cloudflare_mode_refuses_to_boot_without_a_secret(monkeypatch):
    """
    A gate nothing can pass would put every user in the peer bucket with a
    healthy log. ProductionConfig requires the variable; the middleware
    refuses independently so a development environment that opts into the
    mode fails the same way.
    """
    for missing in (None, "", "   "):
        with pytest.raises(ValueError, match="CF_ORIGIN_SECRET"):
            _app(monkeypatch, origin_secret=missing)


# ── the fallback: restrictive, never permissive ─────────────────────

def test_missing_header_falls_back_to_the_peer(monkeypatch):
    """
    Running the Cloudflare posture locally, or a healthcheck reaching the
    container directly, sends no CF-Connecting-IP. The client is then the
    connection.
    """
    body = _get(_app(monkeypatch), xfp="https")

    assert body["remote_addr"] == DIRECT_PEER


@pytest.mark.parametrize(
    "junk",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("unknown", id="unknown"),
        pytest.param("not-an-ip", id="text"),
        pytest.param(f"{CLIENT_IP}:443", id="host-port"),
        pytest.param(f"{CLIENT_IP}, {CF_EDGE}", id="comma-list"),
        pytest.param("[2001:db8::1]", id="bracketed-ipv6"),
        pytest.param("fe80::1%eth0", id="ipv6-zone-id"),
        pytest.param("203.0.113.7/24", id="cidr"),
    ],
)
def test_a_value_that_is_not_one_ip_falls_back_to_the_peer(monkeypatch, junk):
    """
    Cloudflare writes exactly one bare address. Anything else did not come from
    Cloudflare and must not become a rate-limit key — even on a request that
    carries the secret. Falling back to the socket peer is the restrictive
    direction: it merges the request into the shared bucket rather than
    minting one from arbitrary text.
    """
    body = _through_cloudflare(_app(monkeypatch), cf=junk, xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER


def test_surrounding_whitespace_on_the_address_is_tolerated(monkeypatch):
    """Whitespace around an otherwise valid address is not a reason to discard it."""
    body = _through_cloudflare(_app(monkeypatch), cf=f"  {CLIENT_IP}  ", xfp="https")

    assert body["remote_addr"] == CLIENT_IP


def test_equivalent_addresses_do_not_split_into_separate_buckets(monkeypatch):
    """
    The value is parsed and normalised rather than passed through as text, so
    one IPv6 client cannot occupy two buckets by varying case.
    """
    application = _app(monkeypatch)

    upper = _through_cloudflare(application, cf="2001:DB8::1", xfp="https")["limiter_key"]
    lower = _through_cloudflare(application, cf="2001:db8::1", xfp="https")["limiter_key"]

    assert upper == lower == "2001:db8::1"


def test_the_socket_peer_is_preserved_for_debugging():
    """The pre-rewrite address is kept in the environ, as ProxyFix does."""
    seen = {}

    def probe(environ, start_response):
        seen.update(environ)
        start_response("200 OK", [])
        return [b""]

    CfConnectingIp(probe, origin_secret=SECRET)(
        {
            "REMOTE_ADDR": DIRECT_PEER,
            CfConnectingIp.HEADER_KEY: CLIENT_IP,
            CfConnectingIp.ORIGIN_HEADER_KEY: SECRET,
        },
        lambda *a, **k: None,
    )

    assert seen["REMOTE_ADDR"] == CLIENT_IP
    assert seen[CfConnectingIp.ORIG_KEY] == DIRECT_PEER


# ── the trust boundary ──────────────────────────────────────────────

def test_a_client_supplied_header_is_ignored_entirely_without_cloudflare_mode(monkeypatch):
    """
    The trust boundary, from the untrusted side. At the default posture nothing
    in front of the app is trusted, so a client that supplies its own
    CF-Connecting-IP — or X-Forwarded-For, or even the origin header with the
    right value — gets no say in its rate-limit bucket. The socket peer is the
    only address considered.
    """
    application = _app(monkeypatch, source="remote-addr", proto_hops=0, origin_secret=None)

    assert not isinstance(application.wsgi_app, ProxyFix)
    assert not isinstance(application.wsgi_app, CfConnectingIp)

    body = _get(
        application,
        cf=FORGED_IP, origin=SECRET, xff=f"{FORGED_IP}, {CLIENT_IP}", xfp="https",
    )

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER
    assert body["scheme"] == "http"


def test_cloudflare_mode_trusts_the_header_only_with_the_secret(monkeypatch):
    """
    The trust boundary, from the trusted side — pinned so nobody assumes more
    than is there. In cf-connecting-ip mode a request carrying the secret is,
    to the app, from whatever CF-Connecting-IP says; the app cannot tell a
    value Cloudflare wrote from one written by whoever else holds the secret.
    That is the residual trust: the secret's secrecy, not the network path.
    """
    application = _app(monkeypatch)

    assert _through_cloudflare(application, cf=FORGED_IP, xfp="https")["remote_addr"] == FORGED_IP
    assert _get(application, cf=FORGED_IP, xfp="https")["remote_addr"] == DIRECT_PEER


# ── the scheme, kept independent of the client IP ───────────────────

def test_https_is_recognized_from_forwarded_proto(monkeypatch):
    body = _through_cloudflare(_app(monkeypatch), cf=CLIENT_IP, xfp="https")

    assert body["scheme"] == "https"
    assert body["is_secure"] is True


@pytest.mark.parametrize("xff", CLOUDFLARE_XFF_CHAINS)
def test_scheme_does_not_depend_on_the_xff_chain(monkeypatch, xff):
    """
    X-Forwarded-Proto is read from the right, where the nearest TLS terminator
    writes, so the X-Forwarded-For chain moving underneath it must not change
    the answer. This is the regression that a single shared hop count produced.
    """
    body = _through_cloudflare(_app(monkeypatch), cf=CLIENT_IP, xff=xff, xfp="https")

    assert body["scheme"] == "https"
    assert body["is_secure"] is True


def test_scheme_does_not_depend_on_the_origin_secret(monkeypatch):
    """
    A request that fails the origin check is still known to be on TLS. The
    secret gates the client address only; the scheme has its own trust model.
    """
    body = _get(_app(monkeypatch), cf=CLIENT_IP, xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["scheme"] == "https"


def test_multiple_forwarded_proto_values_still_resolve_to_https(monkeypatch):
    """
    x_proto=1 reads the rightmost, so Cloudflare adding a hop in front of
    Railway's terminator is harmless whether Railway appends or overwrites.
    """
    body = _through_cloudflare(_app(monkeypatch), cf=CLIENT_IP, xfp="https, https")

    assert body["scheme"] == "https"


def test_scheme_and_client_ip_are_configured_independently(monkeypatch):
    """
    Trust the TLS terminator's scheme without trusting any forwarded address.
    Neither setting may imply the other.
    """
    body = _get(
        _app(monkeypatch, source="remote-addr", proto_hops=1, origin_secret=None),
        cf=CLIENT_IP,
        origin=SECRET,
        xfp="https",
    )

    assert body["remote_addr"] == DIRECT_PEER
    assert body["scheme"] == "https"


# ── local development: nothing in front of the app ──────────────────

def test_local_development_posture_installs_no_proxy_middleware(monkeypatch):
    """
    The default. With nothing in front of Flask, neither wrapper belongs in the
    WSGI stack — not even as a no-op — no secret is needed, and a request with
    no forwarded headers at all resolves to the real connection.
    """
    application = _app(monkeypatch, source="remote-addr", proto_hops=0, origin_secret=None)

    assert not isinstance(application.wsgi_app, (ProxyFix, CfConnectingIp))

    body = _get(application)

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER
    assert body["user_key"] == f"ip:{DIRECT_PEER}"
    assert body["scheme"] == "http"
    assert body["is_secure"] is False


# ── config: what the settings accept and refuse ─────────────────────

# Everything ProductionConfig demands before it reaches the proxy checks. None of
# these are connected to; they only have to be non-empty.
PROD_ENV = {
    "FLASK_ENV": "production",
    "SECRET_KEY": "not-a-real-key",
    "DATABASE_URL": "sqlite:///:memory:",
    "CELERY_BROKER_URL": "memory://",
    "CELERY_RESULT_BACKEND": "cache+memory://",
    "RATELIMIT_STORAGE_URI": "memory://",
    "WTF_CSRF_SECRET_KEY": "not-a-real-key",
    "ANTHROPIC_API_KEY": "not-a-real-key",
    "RESEND_API_KEY": "not-a-real-key",
    "MAIL_DEFAULT_SENDER": "sender@example.com",
    "SERVER_NAME": "example.com",
}

PROXY_SETTINGS = ["CLIENT_IP_SOURCE", "TRUSTED_PROXY_PROTO_HOPS", "CF_ORIGIN_SECRET"]

CLOUDFLARE_POSTURE = {
    "CLIENT_IP_SOURCE": "cf-connecting-ip",
    "TRUSTED_PROXY_PROTO_HOPS": "1",
    "CF_ORIGIN_SECRET": SECRET,
}


def _load_config(monkeypatch, env):
    """
    Execute config.py fresh under a throwaway module name with exactly `env`.

    A fresh module rather than importlib.reload: reload rewrites the real
    `config` module in place, and the raises under test happen mid-class-body,
    which would leave it half-written for every later test in the session.

    load_dotenv is stubbed out so the developer's own .env cannot supply a value
    a test is trying to prove is missing — ADR-0012 records this suite passing
    for exactly that wrong reason once already.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    for name in PROXY_SETTINGS:
        monkeypatch.delenv(name, raising=False)

    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(value))

    spec = importlib.util.spec_from_file_location(f"_cfg_{uuid.uuid4().hex}", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_requires_the_client_ip_source(monkeypatch):
    with pytest.raises(ValueError, match="CLIENT_IP_SOURCE must be set"):
        _load_config(monkeypatch, PROD_ENV | {"TRUSTED_PROXY_PROTO_HOPS": "1"})


def test_production_requires_the_protocol_count_separately(monkeypatch):
    """
    The protocol count must not be derivable from the client-IP setting.
    Conflating the two headers is what ADR-0012 records as the earlier bug.
    """
    with pytest.raises(ValueError, match="TRUSTED_PROXY_PROTO_HOPS must be set"):
        _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE | {"TRUSTED_PROXY_PROTO_HOPS": None})


def test_production_accepts_the_cloudflare_posture(monkeypatch):
    cfg = _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE)

    assert cfg.ProductionConfig.CLIENT_IP_SOURCE == "cf-connecting-ip"
    assert cfg.ProductionConfig.CF_ORIGIN_SECRET == SECRET
    assert cfg.ProductionConfig.TRUSTED_PROXY_PROTO_HOPS == 1


def test_production_requires_the_origin_secret_in_cloudflare_mode(monkeypatch):
    """Without it the mode is a gate nothing can pass; refuse to boot instead."""
    with pytest.raises(ValueError, match="CF_ORIGIN_SECRET must be set") as excinfo:
        _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE | {"CF_ORIGIN_SECRET": None})

    # The message tells the operator which mode demanded it and how to make one.
    assert "cf-connecting-ip" in str(excinfo.value)
    assert "X-Interview-Intel-Origin" in str(excinfo.value)


def test_a_blank_origin_secret_is_rejected_in_production(monkeypatch):
    """A variable created in the platform UI but left empty is not 'set'."""
    with pytest.raises(ValueError, match="CF_ORIGIN_SECRET must be set"):
        _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE | {"CF_ORIGIN_SECRET": "   "})


@pytest.mark.parametrize(
    "short",
    [
        pytest.param("x", id="one-char"),
        pytest.param("hunter2", id="password-shaped"),
        pytest.param("a" * 31, id="one-under-the-floor"),
    ],
)
def test_a_short_origin_secret_is_rejected_in_production(monkeypatch, short):
    """
    A guessable secret is a bypass waiting to happen: anyone with a route to
    the origin can try values one request at a time, and the rate limits the
    secret protects are what would otherwise slow that down. The message
    reports the count and how to generate a real one — never the value.
    """
    with pytest.raises(ValueError, match="CF_ORIGIN_SECRET must be at least 32 characters") as excinfo:
        _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE | {"CF_ORIGIN_SECRET": short})

    message = str(excinfo.value)
    assert short not in message
    assert f"got {len(short)}" in message
    assert "secrets.token_urlsafe(48)" in message


def test_a_32_character_origin_secret_is_the_floor(monkeypatch):
    """The boundary is inclusive: exactly 32 boots, 31 does not (above)."""
    exactly = "b" * 32

    cfg = _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE | {"CF_ORIGIN_SECRET": exactly})

    assert cfg.ProductionConfig.CF_ORIGIN_SECRET == exactly
    assert cfg.CF_ORIGIN_SECRET_MIN_LENGTH == 32


def test_length_is_measured_after_stripping(monkeypatch):
    """Padding whitespace is not secret material and does not count toward the floor."""
    padded = "  " + "c" * 31 + "\n"

    with pytest.raises(ValueError, match="CF_ORIGIN_SECRET must be at least 32 characters"):
        _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE | {"CF_ORIGIN_SECRET": padded})


def test_the_floor_applies_only_where_the_secret_is_required(monkeypatch):
    """
    remote-addr never reads the secret, so a stray short value in that
    environment is not a reason to refuse to boot. The Compose stack must
    keep starting whatever is left in its environment.
    """
    cfg = _load_config(
        monkeypatch,
        PROD_ENV | {"CLIENT_IP_SOURCE": "remote-addr", "TRUSTED_PROXY_PROTO_HOPS": "0", "CF_ORIGIN_SECRET": "x"},
    )

    assert cfg.ProductionConfig.CLIENT_IP_SOURCE == "remote-addr"


def test_the_origin_secret_is_stripped(monkeypatch):
    cfg = _load_config(monkeypatch, PROD_ENV | CLOUDFLARE_POSTURE | {"CF_ORIGIN_SECRET": f"  {SECRET}\n"})

    assert cfg.ProductionConfig.CF_ORIGIN_SECRET == SECRET


def test_the_no_proxy_posture_is_a_legal_explicit_answer_and_needs_no_secret(monkeypatch):
    """Compose runs FLASK_ENV=production with no proxy at all, so this must boot."""
    cfg = _load_config(
        monkeypatch,
        PROD_ENV | {"CLIENT_IP_SOURCE": "remote-addr", "TRUSTED_PROXY_PROTO_HOPS": "0"},
    )

    assert cfg.ProductionConfig.CLIENT_IP_SOURCE == "remote-addr"
    assert cfg.ProductionConfig.CF_ORIGIN_SECRET is None
    assert cfg.ProductionConfig.TRUSTED_PROXY_PROTO_HOPS == 0


def test_development_defaults_to_trusting_nothing(monkeypatch):
    """The Cloudflare mode is opt-in; no environment inherits it or its secret."""
    cfg = _load_config(monkeypatch, {"FLASK_ENV": "development"})

    assert cfg.Config.CLIENT_IP_SOURCE == "remote-addr"
    assert cfg.Config.CF_ORIGIN_SECRET is None
    assert cfg.Config.TRUSTED_PROXY_PROTO_HOPS == 0


@pytest.mark.parametrize(
    "value", ["cf_connecting_ip", "cloudflare", "cf-connecting-ipv6", "true-client-ip", "true", "2"]
)
def test_an_unrecognised_client_ip_source_is_rejected(monkeypatch, value):
    """
    A typo must not silently downgrade to the socket peer. That downgrade is the
    site-wide-lockout failure, and it would happen at boot with a healthy log.
    """
    with pytest.raises(ValueError, match="CLIENT_IP_SOURCE must be one of"):
        _load_config(monkeypatch, {"FLASK_ENV": "development", "CLIENT_IP_SOURCE": value})


def test_the_removed_xff_leftmost_mode_is_refused_with_a_pointer(monkeypatch):
    """
    The value the pre-Cloudflare Railway environment carries. Accepting it
    would key rate limits on Cloudflare's edge addresses; silently mapping it
    to anything else would hide a configuration that needs a human to change
    it. It fails at boot and says what to set instead.
    """
    with pytest.raises(ValueError, match="CLIENT_IP_SOURCE must be one of") as excinfo:
        _load_config(monkeypatch, PROD_ENV | {"CLIENT_IP_SOURCE": "xff-leftmost", "TRUSTED_PROXY_PROTO_HOPS": "1"})

    assert "removed" in str(excinfo.value)
    assert "cf-connecting-ip" in str(excinfo.value)


def test_client_ip_source_is_case_insensitive(monkeypatch):
    cfg = _load_config(
        monkeypatch,
        {"FLASK_ENV": "development", "CLIENT_IP_SOURCE": "  CF-Connecting-IP ", "CF_ORIGIN_SECRET": SECRET},
    )

    assert cfg.Config.CLIENT_IP_SOURCE == "cf-connecting-ip"


def test_negative_proto_hop_count_is_rejected(monkeypatch):
    """A negative count must not fall through as 'proxy trust off'."""
    with pytest.raises(ValueError, match="TRUSTED_PROXY_PROTO_HOPS must be zero or greater"):
        _load_config(
            monkeypatch, {"FLASK_ENV": "development", "TRUSTED_PROXY_PROTO_HOPS": "-1"}
        )


def test_non_numeric_proto_hop_count_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="TRUSTED_PROXY_PROTO_HOPS must be a whole number"):
        _load_config(
            monkeypatch, {"FLASK_ENV": "development", "TRUSTED_PROXY_PROTO_HOPS": "one"}
        )


@pytest.mark.parametrize("name", ["CLIENT_IP_SOURCE", "TRUSTED_PROXY_PROTO_HOPS"])
def test_blank_value_is_rejected_in_production(monkeypatch, name):
    """A variable created in the platform UI but left empty is not 'set'."""
    other = {"CLIENT_IP_SOURCE": "remote-addr", "TRUSTED_PROXY_PROTO_HOPS": "1"}
    other.pop(name)

    with pytest.raises(ValueError, match=f"{name} must be set"):
        _load_config(monkeypatch, PROD_ENV | other | {name: "  "})
