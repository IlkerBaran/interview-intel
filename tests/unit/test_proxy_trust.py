"""
Reverse-proxy trust regression tests.

Two independent questions, tested independently because they fail
independently and both fail SILENTLY in production:

* **Which address is the client?** Read from X-Forwarded-For. Get it wrong in
  one direction and every user shares a rate-limit bucket, so the global
  default becomes a site-wide lockout; wrong in the other and any client mints
  a fresh bucket per request and IP limits stop existing. Neither raises.
* **Was this request on TLS?** Read from X-Forwarded-Proto. Get it wrong and
  request.is_secure reads False on HTTPS traffic, which switches off Flask-WTF's
  WTF_CSRF_SSL_STRICT referer check while every page keeps working.

These tests pin the RULES, not the header shape that happened to be on the wire
when Railway was measured (ADR-0012). The measured chain had two values, but the
right-hand entry was observed changing between requests, so a test that asserted
"two values, client is second from the right" would be pinning an accident.
What is asserted instead: the leftmost value wins whatever the chain length is,
the scheme does not depend on the chain length at all, and at the default
posture no forwarded header is trusted for anything.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from flask import request
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from app.extensions import pending_email_key, user_key
from app.proxy import ForwardedForLeftmost

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.py"

CLIENT_IP = "203.0.113.7"

# The right-hand entries are intermediaries. Railway was observed returning
# different ones across identical requests — the two prefixes below stand in for
# the two routing paths — which is the whole reason a right-hand count is not
# used. Any test that depends on which of these is present is testing the wrong
# thing.
EDGE_A = "152.0.113.1"
EDGE_B = "79.0.113.1"
INTERNAL = "100.64.0.9"

# What gunicorn's socket sees behind a proxy: the proxy, never the client.
DIRECT_PEER = "100.64.0.2"

# A value a client puts in X-Forwarded-For itself. Railway strips it before the
# app sees it — verified live — but the app must not be the thing relying on that
# anywhere it has not been told the ingress sanitises.
FORGED_IP = "1.2.3.4"


# ── harness ─────────────────────────────────────────────────────────

def _app(monkeypatch, *, source="xff-leftmost", proto_hops=1, https=False):
    """A fresh app at the given trust posture, with a probe route."""
    from app import create_app

    monkeypatch.setattr(config.TestingConfig, "CLIENT_IP_SOURCE", source)
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


def _get(application, xff=None, xfp=None, xri=None):
    """Issue a request shaped like one arriving through a proxy."""
    headers = {}
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


# ── the client IP: leftmost, whatever the chain length ──────────────

# Every one of these was a plausible Railway chain at some point. The measured
# one had two values; the app must not care which of these arrives.
VARIABLE_CHAINS = [
    pytest.param(CLIENT_IP, id="one-value"),
    pytest.param(f"{CLIENT_IP}, {EDGE_A}", id="two-values-path-a"),
    pytest.param(f"{CLIENT_IP}, {EDGE_B}", id="two-values-path-b"),
    pytest.param(f"{CLIENT_IP}, {EDGE_A}, {INTERNAL}", id="three-values-cdn-path"),
    pytest.param(f"{CLIENT_IP}, {EDGE_B}, {EDGE_A}, {INTERNAL}", id="four-values"),
]


@pytest.mark.parametrize("xff", VARIABLE_CHAINS)
def test_leftmost_is_the_client_at_every_chain_length(monkeypatch, xff):
    """
    The core property. Railway alternates routing paths and the number of
    X-Forwarded-For values moves with it, so the client's position from the
    RIGHT is not knowable — but it is always position 0.
    """
    body = _get(_app(monkeypatch), xff=xff, xfp="https")

    assert body["remote_addr"] == CLIENT_IP


@pytest.mark.parametrize("xff", VARIABLE_CHAINS)
def test_every_ip_keyed_path_sees_the_client_at_every_chain_length(monkeypatch, xff):
    """
    Fixing the limiter's default key while leaving another security-sensitive
    path on the proxy's address would be a bug with no symptom. All three sites
    in extensions.py resolve the client through request.remote_addr, so all
    three are asserted here against the same varying chain.
    """
    body = _get(_app(monkeypatch), xff=xff, xfp="https")

    assert body["limiter_key"] == CLIENT_IP
    assert body["user_key"] == f"ip:{CLIENT_IP}"
    assert body["pending_email_key"] == f"ip:{CLIENT_IP}"


def test_the_same_client_keeps_one_bucket_across_routing_paths(monkeypatch):
    """
    Stated as the property that actually matters to rate limiting: a user whose
    requests take different Railway paths must not be split across buckets, and
    must not be merged with everyone else behind an edge.
    """
    application = _app(monkeypatch)

    keys = {
        _get(application, xff=xff.values[0], xfp="https")["limiter_key"]
        for xff in VARIABLE_CHAINS
    }

    assert keys == {CLIENT_IP}


# ── the counter-model: why the fixed hop count was abandoned ────────

@pytest.mark.parametrize(
    "xff, wrong_answer",
    [
        # Chain shorter than the count: ProxyFix finds fewer values than it
        # trusts, takes NOTHING, and remote_addr stays the internal peer — one
        # bucket for the entire platform.
        pytest.param(CLIENT_IP, DIRECT_PEER, id="short-chain-no-rewrite-at-all"),
        # Chain longer than the count: ProxyFix returns an intermediary, so
        # everyone behind that edge PoP shares a bucket.
        pytest.param(f"{CLIENT_IP}, {EDGE_A}, {INTERNAL}", EDGE_A, id="long-chain-picks-the-edge"),
    ],
)
def test_a_fixed_right_hand_count_breaks_when_the_chain_moves(xff, wrong_answer):
    """
    The rejected design, pinned so the reason survives (ADR-0012).

    x_for=2 was correct for the two-value chain that was measured. This shows
    what it does at the chain lengths Railway can also produce: in both cases it
    silently keys rate limits on something that is not the client, which is
    exactly what a fixed count cannot protect against when the length is not
    promised.
    """
    seen = {}

    def probe(environ, start_response):
        seen["remote_addr"] = environ["REMOTE_ADDR"]
        start_response("200 OK", [])
        return [b""]

    ProxyFix(probe, x_for=2, x_proto=1)(
        {
            "REMOTE_ADDR": DIRECT_PEER,
            "HTTP_X_FORWARDED_FOR": xff,
            "wsgi.url_scheme": "http",
        },
        lambda *a, **k: None,
    )

    assert seen["remote_addr"] == wrong_answer
    assert seen["remote_addr"] != CLIENT_IP


def test_proxyfix_is_not_allowed_to_touch_the_client_address(monkeypatch):
    """
    ProxyFix stays in the stack for the scheme, but with x_for=0 so it cannot
    reintroduce a right-hand count for the client address by accident.
    """
    application = _app(monkeypatch, source="remote-addr", proto_hops=1)

    assert isinstance(application.wsgi_app, ProxyFix)
    assert application.wsgi_app.x_for == 0

    # A chain long enough that any nonzero x_for would have rewritten something.
    body = _get(application, xff=f"{CLIENT_IP}, {EDGE_A}, {INTERNAL}", xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["scheme"] == "https"


# ── forged headers and the trust boundary ───────────────────────────

def test_forged_xff_is_ignored_entirely_without_a_trusted_ingress(monkeypatch):
    """
    The trust boundary, from the untrusted side. At the default posture nothing
    in front of the app is trusted, so a client that supplies its own
    X-Forwarded-For gets no say in its rate-limit bucket — the socket peer is
    the only address considered.
    """
    application = _app(monkeypatch, source="remote-addr", proto_hops=0)

    assert not isinstance(application.wsgi_app, ProxyFix)
    assert not isinstance(application.wsgi_app, ForwardedForLeftmost)

    body = _get(application, xff=f"{FORGED_IP}, {CLIENT_IP}", xfp="https")

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER
    assert body["scheme"] == "http"


def test_forged_value_never_reaches_the_app_through_railway(monkeypatch):
    """
    What was observed live: a request sent with X-Forwarded-For: 1.2.3.4 arrived
    with the forged value already gone, the real client leftmost. Replaying that
    shape asserts the app resolves the client — and never the forged address —
    from what the edge actually delivers.
    """
    body = _get(_app(monkeypatch), xff=f"{CLIENT_IP}, {EDGE_A}", xfp="https")

    assert body["remote_addr"] == CLIENT_IP
    assert body["remote_addr"] != FORGED_IP
    assert body["limiter_key"] != FORGED_IP


def test_a_leftmost_value_that_is_not_an_ip_falls_back_to_the_peer(monkeypatch):
    """
    X-Forwarded-For may carry `unknown`, an obfuscated identifier or a host:port
    pair. None of those should become a rate-limit key. Falling back to the
    socket peer is the restrictive direction: it merges the request into the
    shared bucket rather than minting one from arbitrary text.
    """
    application = _app(monkeypatch)

    for junk in ("unknown", "not-an-ip", f"{CLIENT_IP}:443", ""):
        body = _get(application, xff=f"{junk}, {EDGE_A}", xfp="https")
        assert body["remote_addr"] == DIRECT_PEER, junk
        assert body["limiter_key"] == DIRECT_PEER, junk


def test_equivalent_addresses_do_not_split_into_separate_buckets(monkeypatch):
    """
    The leftmost value is parsed and normalised rather than passed through as
    text, so one IPv6 client cannot occupy two buckets by varying case.
    """
    application = _app(monkeypatch)

    upper = _get(application, xff="2001:DB8::1", xfp="https")["limiter_key"]
    lower = _get(application, xff="2001:db8::1", xfp="https")["limiter_key"]

    assert upper == lower == "2001:db8::1"


def test_x_real_ip_is_never_consulted(monkeypatch):
    """
    X-Real-IP agreed with the client on every request measured, and Railway
    populates it with the CDN edge address instead whenever the CDN path is
    active — an acknowledged bug on their side. Reading it would work until the
    routing flipped. This pins that it is not read at all: X-Forwarded-For wins
    even when X-Real-IP disagrees.
    """
    body = _get(_app(monkeypatch), xff=f"{CLIENT_IP}, {EDGE_A}", xfp="https", xri=EDGE_B)

    assert body["remote_addr"] == CLIENT_IP


# ── the scheme, kept independent of the chain ───────────────────────

def test_https_is_recognized_from_forwarded_proto(monkeypatch):
    body = _get(_app(monkeypatch), xff=f"{CLIENT_IP}, {EDGE_A}", xfp="https")

    assert body["scheme"] == "https"
    assert body["is_secure"] is True


@pytest.mark.parametrize("xff", VARIABLE_CHAINS)
def test_scheme_does_not_depend_on_the_xff_chain_length(monkeypatch, xff):
    """
    The separation required by the design: X-Forwarded-Proto is read from the
    right, where the TLS terminator writes, so the client-IP chain moving
    underneath it must not change the answer. This is the regression that a
    single shared hop count produced.
    """
    body = _get(_app(monkeypatch), xff=xff, xfp="https")

    assert body["scheme"] == "https"
    assert body["is_secure"] is True


def test_multiple_forwarded_proto_values_still_resolve_to_https(monkeypatch):
    """x_proto=1 reads the rightmost, so an extra hop in front is harmless."""
    body = _get(_app(monkeypatch), xff=f"{CLIENT_IP}, {EDGE_A}", xfp="https, https")

    assert body["scheme"] == "https"


def test_scheme_and_client_ip_are_configured_independently(monkeypatch):
    """
    Trust the TLS terminator's scheme without trusting any forwarded address.
    Neither setting may imply the other.
    """
    body = _get(
        _app(monkeypatch, source="remote-addr", proto_hops=1),
        xff=f"{CLIENT_IP}, {EDGE_A}",
        xfp="https",
    )

    assert body["remote_addr"] == DIRECT_PEER
    assert body["scheme"] == "https"


# ── local development: nothing in front of the app ──────────────────

def test_local_development_posture_installs_no_proxy_middleware(monkeypatch):
    """
    The default. With nothing in front of Flask, neither wrapper belongs in the
    WSGI stack — not even as a no-op — and a request with no forwarded headers
    at all resolves to the real connection.
    """
    application = _app(monkeypatch, source="remote-addr", proto_hops=0)

    assert not isinstance(application.wsgi_app, (ProxyFix, ForwardedForLeftmost))

    body = _get(application)

    assert body["remote_addr"] == DIRECT_PEER
    assert body["limiter_key"] == DIRECT_PEER
    assert body["user_key"] == f"ip:{DIRECT_PEER}"
    assert body["scheme"] == "http"
    assert body["is_secure"] is False


def test_leftmost_mode_without_the_header_falls_back_to_the_peer(monkeypatch):
    """
    Running the Railway posture locally, or a healthcheck reaching the container
    directly, sends no X-Forwarded-For. The client is then the connection.
    """
    body = _get(_app(monkeypatch), xfp="https")

    assert body["remote_addr"] == DIRECT_PEER


# ── config: what the two settings accept and refuse ─────────────────

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

PROXY_SETTINGS = ["CLIENT_IP_SOURCE", "TRUSTED_PROXY_PROTO_HOPS"]


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
        _load_config(monkeypatch, PROD_ENV | {"CLIENT_IP_SOURCE": "xff-leftmost"})


def test_production_accepts_the_railway_posture(monkeypatch):
    cfg = _load_config(
        monkeypatch,
        PROD_ENV | {"CLIENT_IP_SOURCE": "xff-leftmost", "TRUSTED_PROXY_PROTO_HOPS": "1"},
    )

    assert cfg.ProductionConfig.CLIENT_IP_SOURCE == "xff-leftmost"
    assert cfg.ProductionConfig.TRUSTED_PROXY_PROTO_HOPS == 1


def test_the_no_proxy_posture_is_a_legal_explicit_answer(monkeypatch):
    """Compose runs FLASK_ENV=production with no proxy at all, so this must boot."""
    cfg = _load_config(
        monkeypatch,
        PROD_ENV | {"CLIENT_IP_SOURCE": "remote-addr", "TRUSTED_PROXY_PROTO_HOPS": "0"},
    )

    assert cfg.ProductionConfig.CLIENT_IP_SOURCE == "remote-addr"
    assert cfg.ProductionConfig.TRUSTED_PROXY_PROTO_HOPS == 0


def test_development_defaults_to_trusting_nothing(monkeypatch):
    cfg = _load_config(monkeypatch, {"FLASK_ENV": "development"})

    assert cfg.Config.CLIENT_IP_SOURCE == "remote-addr"
    assert cfg.Config.TRUSTED_PROXY_PROTO_HOPS == 0


@pytest.mark.parametrize("value", ["xff_leftmost", "leftmost", "true", "2"])
def test_an_unrecognised_client_ip_source_is_rejected(monkeypatch, value):
    """
    A typo must not silently downgrade to the socket peer. That downgrade is the
    site-wide-lockout failure, and it would happen at boot with a healthy log.
    """
    with pytest.raises(ValueError, match="CLIENT_IP_SOURCE must be one of"):
        _load_config(monkeypatch, {"FLASK_ENV": "development", "CLIENT_IP_SOURCE": value})


def test_client_ip_source_is_case_insensitive(monkeypatch):
    cfg = _load_config(
        monkeypatch, {"FLASK_ENV": "development", "CLIENT_IP_SOURCE": "  XFF-Leftmost "}
    )

    assert cfg.Config.CLIENT_IP_SOURCE == "xff-leftmost"


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


@pytest.mark.parametrize("name", PROXY_SETTINGS)
def test_blank_value_is_rejected_in_production(monkeypatch, name):
    """A variable created in the platform UI but left empty is not 'set'."""
    other = {"CLIENT_IP_SOURCE": "remote-addr", "TRUSTED_PROXY_PROTO_HOPS": "1"}
    other.pop(name)

    with pytest.raises(ValueError, match=f"{name} must be set"):
        _load_config(monkeypatch, PROD_ENV | other | {name: "  "})
