"""
Security header regression tests.

The CSP fails silently: a directive that is too tight breaks a page with no
server-side error. These pin the exact policy, prove Talisman did not quietly
change cookie or policy behavior, and assert that no inline JS has crept back
in to invalidate the strict script-src.
"""

import re
from pathlib import Path

import config

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"

# Any inline event-handler attribute, any case, any spacing around '='.
# The leading class anchors to an attribute boundary so words ending in "on"
# ("comparison=") cannot match.
INLINE_ON_ATTR = re.compile(r"""[\s"']on[a-zA-Z]+\s*=""", re.IGNORECASE)
JAVASCRIPT_URL = re.compile(r"javascript\s*:", re.IGNORECASE)
# Opening <script> tag, tolerating "< SCRIPT" and attributes; group 1 is attrs.
SCRIPT_OPEN_TAG = re.compile(r"<\s*script\b([^>]*)>", re.IGNORECASE)
HAS_SRC = re.compile(r"\bsrc\s*=", re.IGNORECASE)

EXPECTED_CSP = {
    "default-src":     "'self'",
    "script-src":      "'self'",
    "style-src":       "'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src":        "'self' https://fonts.gstatic.com",
    "img-src":         "'self'",
    "connect-src":     "'self'",
    "form-action":     "'self'",
    "frame-ancestors": "'self'",
    "base-uri":        "'self'",
    "object-src":      "'none'",
}


def _line_of(text, index):
    return text[:index].count("\n") + 1


def _scan(root=None):
    """Return [(path, line, kind)] for every inline-JS construct found."""
    root = root or TEMPLATES
    offenders = []
    for path in sorted(root.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()

        for m in INLINE_ON_ATTR.finditer(text):
            offenders.append((rel, _line_of(text, m.start()), f"inline handler {m.group().strip()}"))
        for m in JAVASCRIPT_URL.finditer(text):
            offenders.append((rel, _line_of(text, m.start()), "javascript: URL"))
        for m in SCRIPT_OPEN_TAG.finditer(text):
            if not HAS_SRC.search(m.group(1)):
                offenders.append((rel, _line_of(text, m.start()), "inline <script> block"))
    return offenders


def _directives(csp):
    """Parse a CSP header into {directive: raw value string}."""
    parsed = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(" ")
        parsed[name.lower()] = value.strip()
    return parsed


def _make_app(monkeypatch, https):
    """A fresh app at the given HTTPS posture, with a session-writing probe route."""
    from flask import session

    from app import create_app

    monkeypatch.setattr(config.TestingConfig, "TALISMAN_HTTPS", https)
    application = create_app()

    # Registered before the first request, so Flask still allows setup. Returns a
    # bare string: no template render, so no context processor touches the DB.
    @application.route("/_cookie_probe")
    def _cookie_probe():
        session["probe"] = "1"
        return "ok"

    return application


# ── inline JS scanner ──────────────────────────────────────────────

def test_no_inline_js_in_templates():
    """
    Guards script-src 'self'. One inline handler or <script> block added later
    would break that page's JS silently in the browser, with nothing failing
    server-side. This is the only check that keeps the strict policy true.
    """
    offenders = _scan()
    assert not offenders, "Inline JS breaks script-src 'self':\n" + "\n".join(
        f"  {p}:{ln} — {kind}" for p, ln, kind in offenders
    )


def test_scanner_detects_planted_violations(tmp_path):
    """The scanner is only worth having if it actually fires — prove it does."""
    (tmp_path / "bad.html").write_text(
        '<button ONCLICK ="x()">a</button>\n'
        '<a href="JavaScript:history.back()">b</a>\n'
        "< SCRIPT >var x = 1;</script>\n"
        '<script src="/static/js/ok.js"></script>\n',
        encoding="utf-8",
    )

    kinds = [kind for _, _, kind in _scan(tmp_path)]

    assert any("inline handler" in k for k in kinds)
    assert any("javascript: URL" in k for k in kinds)
    assert any("inline <script>" in k for k in kinds)
    assert len(kinds) == 3          # the <script src> line must NOT be flagged


# ── CSP ────────────────────────────────────────────────────────────

def test_script_src_is_exactly_self(app):
    """
    Exact match, not merely 'unsafe-inline' and nonce absent. Asserting what is
    missing cannot catch a source that was ADDED — a CDN host, 'unsafe-eval',
    'strict-dynamic' — each of which reopens script injection.
    """
    csp = app.test_client().get("/").headers["Content-Security-Policy"]

    assert _directives(csp)["script-src"] == "'self'"


def test_full_csp_matches_expected(app):
    """Pins every directive, so no source can be added or dropped unnoticed."""
    csp = app.test_client().get("/").headers["Content-Security-Policy"]
    actual = _directives(csp)

    assert set(actual) == set(EXPECTED_CSP)
    for name, expected in EXPECTED_CSP.items():
        # Compare as token sets: Talisman's join order is not part of the contract.
        assert set(actual[name].split()) == set(expected.split()), name


# ── other headers ──────────────────────────────────────────────────

def test_legacy_headers_still_set(app):
    headers = app.test_client().get("/").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_permissions_policy_is_pinned(app):
    """
    Talisman 1.1.0 emits browsing-topics=() by default. Pinned explicitly in
    create_app, so this asserts our value rather than an inherited one — if a
    future version changes its default, this fails instead of shipping silently.
    """
    headers = app.test_client().get("/").headers

    assert headers["Permissions-Policy"] == "browsing-topics=()"
    assert "Feature-Policy" not in headers   # superseded; must not reappear


# ── cookies ────────────────────────────────────────────────────────

def test_cookie_config_in_http_mode(app):
    app.test_client().get("/")   # SESSION_COOKIE_SECURE is set during a request

    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SECURE"] is False   # TALISMAN_HTTPS is off here


def test_session_cookie_response_flags_http_mode(monkeypatch):
    """
    Response-level, not config-level: this is what the browser actually receives.
    Secure must be absent here or local and Docker HTTP sessions break silently —
    login appears to succeed, then every later request is anonymous.
    """
    application = _make_app(monkeypatch, https=False)

    cookie = application.test_client().get("/_cookie_probe").headers["Set-Cookie"]

    assert "SameSite=Lax" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie


def test_session_cookie_response_flags_https_mode(monkeypatch):
    application = _make_app(monkeypatch, https=True)

    # base_url avoids the force_https redirect, which would carry no Set-Cookie.
    resp = application.test_client().get("/_cookie_probe", base_url="https://localhost")
    cookie = resp.headers["Set-Cookie"]

    assert "SameSite=Lax" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie


# ── HTTPS posture ──────────────────────────────────────────────────

def test_no_hsts_without_https(app):
    assert "Strict-Transport-Security" not in app.test_client().get("/").headers


def test_healthz_is_not_redirected_when_https_forced(monkeypatch):
    """The Docker health check probes /healthz over plain HTTP."""
    from app import create_app

    monkeypatch.setattr(config.TestingConfig, "TALISMAN_HTTPS", True)
    resp = create_app().test_client().get("/healthz")

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_hsts_is_final_value_when_https_enabled(monkeypatch):
    from app import create_app

    monkeypatch.setattr(config.TestingConfig, "TALISMAN_HTTPS", True)
    application = create_app()

    hsts = application.test_client().get("/", base_url="https://localhost").headers[
        "Strict-Transport-Security"
    ]

    assert "max-age=31536000" in hsts
    assert "includeSubDomains" not in hsts
    assert "preload" not in hsts


def test_two_apps_do_not_share_talisman_state(monkeypatch):
    """
    Regression guard for the per-app Talisman instance in create_app().

    Talisman stores every option on the INSTANCE and ends init_app with
    `self.app = app`; _force_https then reads self.app.debug and WRITES
    self.app.config['SESSION_COOKIE_SECURE']. A shared module-level singleton
    would let the second create_app() overwrite the first's settings and mutate
    the wrong app's config, making the HTTPS-posture tests order-dependent.
    """
    from app import create_app

    monkeypatch.setattr(config.TestingConfig, "TALISMAN_HTTPS", False)
    http_app = create_app()

    monkeypatch.setattr(config.TestingConfig, "TALISMAN_HTTPS", True)
    https_app = create_app()

    # Exercise the https app FIRST, then prove the http app is unaffected.
    https_resp = https_app.test_client().get("/", base_url="https://localhost")
    assert "Strict-Transport-Security" in https_resp.headers

    http_resp = http_app.test_client().get("/")
    assert http_resp.status_code == 200                          # not redirected
    assert "Strict-Transport-Security" not in http_resp.headers
    assert http_app.config["SESSION_COOKIE_SECURE"] is False     # not mutated
