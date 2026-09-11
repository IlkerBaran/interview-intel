import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent


# How the app determines the client IP:
#
# remote-addr      = use the socket peer; ignore forwarded headers.
# cf-connecting-ip = use the CF-Connecting-IP header Cloudflare writes.
#
# cf-connecting-ip trusts the header only on requests that prove they came
# through Cloudflare: Cloudflare sets X-Interview-Intel-Origin to
# CF_ORIGIN_SECRET on every request it forwards, and the app checks it. A
# request without the secret — one that reached the origin around Cloudflare —
# keeps the socket peer. X-Forwarded-For is not an option in either mode:
# through Cloudflare its leftmost value is a Cloudflare edge, not the client,
# and a right-hand hop count is unsafe because Railway's chain length varies
# by routing path. See ADR-0012.
CLIENT_IP_SOURCES = ("remote-addr", "cf-connecting-ip")

# Removed mode, refused with a pointer rather than silently ignored. It was the
# correct answer on direct Railway and is the value a pre-Cloudflare
# environment still carries; behind Cloudflare it keys rate limits on
# Cloudflare's own edge addresses, so a stale variable must fail at boot.
_REMOVED_CLIENT_IP_SOURCE = "xff-leftmost"


def _client_ip_source(name="CLIENT_IP_SOURCE", *, required=False):
    """
    Read the client-IP resolution mode from the environment.

    Defaults to the socket peer, which is the only answer that is safe without
    knowing what is in front of the app. ProductionConfig passes `required` so
    a real deployment has to state its ingress model rather than inherit a
    default that happens to be wrong for it.

    An unrecognised value raises instead of falling back. Falling back would
    turn a typo — `cf_connecting_ip`, `cloudflare` — into a silent downgrade to
    the peer address, which is the site-wide-lockout failure in ADR-0012.
    """
    raw = (os.getenv(name) or "").strip().lower()

    if not raw:
        if required:
            raise ValueError(
                f"{name} must be set in environment "
                f"(one of {', '.join(CLIENT_IP_SOURCES)}; "
                "use cf-connecting-ip behind Cloudflare, "
                "remote-addr with no proxy in front)"
            )
        return "remote-addr"

    if raw not in CLIENT_IP_SOURCES:
        message = f"{name} must be one of {', '.join(CLIENT_IP_SOURCES)}, got {raw!r}"
        if raw == _REMOVED_CLIENT_IP_SOURCE:
            message += (
                " (removed: through Cloudflare the leftmost X-Forwarded-For value "
                "is a Cloudflare edge, not the client; use cf-connecting-ip, "
                "see ADR-0012)"
            )
        raise ValueError(message)

    return raw


# The floor on CF_ORIGIN_SECRET where it is required. Anyone with a route to
# the origin around Cloudflare can guess at the secret one request at a time,
# and the rate limits it protects are exactly what would otherwise slow that
# down — so a short secret is a bypass waiting to happen. 32 characters of
# secrets.token_urlsafe output is ~190 bits; the recommended token_urlsafe(48)
# gives 64 characters, comfortably above the floor.
CF_ORIGIN_SECRET_MIN_LENGTH = 32


def _cf_origin_secret(name="CF_ORIGIN_SECRET", *, required=False):
    """
    Read the shared secret that proves a request came through Cloudflare.

    Required only when the client-IP mode is cf-connecting-ip: that mode is
    unusable without it, because the middleware refuses to trust
    CF-Connecting-IP on any request that does not carry the secret. The
    remote-addr mode never reads it, so local development and the Compose
    stack need not set it.

    Where it is required it must also be at least CF_ORIGIN_SECRET_MIN_LENGTH
    characters; a value that is set but guessable is the same failure as one
    that is missing, only quieter.

    Whitespace is stripped so a trailing newline pasted into a platform UI
    cannot make every request fail the check. The value is returned, never
    logged, and never appears in an error message — the length check reports
    only the count.
    """
    raw = (os.getenv(name) or "").strip()

    if not raw:
        if required:
            raise ValueError(
                f"{name} must be set in environment when CLIENT_IP_SOURCE=cf-connecting-ip "
                "(the value the Cloudflare Request Header Transform rule sets in "
                "X-Interview-Intel-Origin; generate with "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
            )
        return None

    if required and len(raw) < CF_ORIGIN_SECRET_MIN_LENGTH:
        raise ValueError(
            f"{name} must be at least {CF_ORIGIN_SECRET_MIN_LENGTH} characters "
            f"when CLIENT_IP_SOURCE=cf-connecting-ip, got {len(raw)}; generate with "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    return raw


def _proxy_hop_count(name, hint="", *, required=False):
    """
    Read the X-Forwarded-Proto trusted-hop count.

    Client IP does not use a hop count; it comes from the single-value
    CF-Connecting-IP header, resolved separately. Proto is read from the
    right, so one trusted TLS terminator remains one hop regardless of how
    many layers sit in front of it.

    Missing values default to 0 unless required. Invalid or negative values
    raise instead of silently disabling proxy trust.
    """
    raw = (os.getenv(name) or "").strip()

    if not raw:
        if required:
            raise ValueError(
                f"{name} must be set in environment" + (f" ({hint})" if hint else "")
            )
        return 0

    try:
        hops = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None

    if hops < 0:
        raise ValueError(f"{name} must be zero or greater, got {hops}")

    return hops

class Config:
    """
    Application configuration module.

    Defines environment-specific configuration classes for the Flask application
    using a shared base `Config`. Environment variables are loaded from `.env`
    for development, while production relies on system-level environment variables.

    Key behavior:
    - `Config` (base):
      - Loads core settings such as `SECRET_KEY`, CSRF configuration, database URI,
        Celery/Redis wiring, and rate-limiting settings.
      - Uses environment variables when available, with a fallback `SECRET_KEY`
        for development convenience.
      - Supports PostgreSQL (with URI normalization) and defaults to SQLite locally.
      - `RATELIMIT_KEY_SECRET` is defined per class rather
        than only here, so it tracks each class's own `SECRET_KEY` override instead
        of binding to the base value at import time.

    - `DevelopmentConfig`:
      - Inherits base settings.
      - Enables debugging for local development.
      - Uses committed literal fallbacks for `SECRET_KEY` and `WTF_CSRF_SECRET_KEY`
        so a fresh clone runs with no `.env` present. These are development-only
        values and must never be used on a reachable host.

    - `TestingConfig`:
      - Uses an in-memory SQLite database for fast, isolated tests.
      - Disables CSRF to simplify form testing.
      - Overrides `SECRET_KEY` and `WTF_CSRF_SECRET_KEY` with fixed values so the
        suite runs with no external services and no `.env`.

    - `ProductionConfig`:
      - Disables debug and testing modes.
      - Requires `SECRET_KEY`, `DATABASE_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`,
        `RATELIMIT_STORAGE_URI`, `WTF_CSRF_SECRET_KEY`, `ANTHROPIC_API_KEY`,
        `RESEND_API_KEY`, `MAIL_DEFAULT_SENDER`, `SERVER_NAME`,
        `CLIENT_IP_SOURCE` and `TRUSTED_PROXY_PROTO_HOPS` to be set in the
        environment, raising on startup if any is missing; `CF_ORIGIN_SECRET`
        is required as well whenever `CLIENT_IP_SOURCE=cf-connecting-ip`.
      - These checks are gated on `FLASK_ENV == "production"`. The class body runs on
        every import of this module, so an ungated raise would make `config.py`
        unimportable in development and CI without a `.env`.

    `config_by_name` maps environment names to their corresponding configuration
    classes, allowing dynamic selection via `FLASK_ENV`.
    """
    SECRET_KEY = os.getenv("SECRET_KEY")
    WTF_CSRF_SECRET_KEY = os.getenv("WTF_CSRF_SECRET_KEY") or SECRET_KEY

    db_url = os.getenv("DATABASE_URL")
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    SQLALCHEMY_DATABASE_URI = db_url or f"sqlite:///{BASE_DIR / 'data.sqlite'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ≈≈≈≈ LLM config ≈≈≈≈
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")

    # ≈≈≈≈ LLM API timeout and retry settings (Stage 3) ≈≈≈≈
    # Prevent Anthropic/LLM calls from hanging forever.
    # These limits make sure a stuck request fails so Celery can handle retry/failure logic.
    #
    # LLM_MAX_RETRIES=0 means the Anthropic client will not retry internally.
    # Celery owns task retries, which keeps timing predictable.
    LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    LLM_CONNECT_TIMEOUT_SECONDS = float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "10"))
    LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "0"))

    # ≈≈≈≈ Fake LLM mode for tests ≈≈≈≈
    # When enabled, llm_service returns deterministic test text instead of calling Anthropic.
    # This is used for live-worker integration tests, similar to MAIL_SUPPRESS_SEND for email.
    # Keep this false in normal production operation.
    LLM_FAKE = os.getenv("LLM_FAKE", "false").strip().lower() in ("1", "true", "yes", "on")

    # LLM_FAKE_MODE selects the fake's behavior when LLM_FAKE=1:
    #   "degrade" (default): non-JSON echo → extraction degrades to None, enrichments non-None
    #   "success": valid JSON for the extraction call (parses → structured fields populate)
    #              + canned prose for the 5 enrichment calls
    LLM_FAKE_MODE = os.getenv("LLM_FAKE_MODE", "degrade").strip().lower()

    # ≈≈≈≈ Resend config ≈≈≈≈
    RESEND_API_KEY = os.getenv("RESEND_API_KEY")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER")

    # ≈≈≈≈ Email and Password expiry hours config ≈≈≈≈
    EMAIL_VERIFICATION_TOKEN_EXPIRY_HOURS = 24
    PASSWORD_RESET_TOKEN_EXPIRY_HOURS = 1

    # ≈≈≈≈ Celery / Redis config ≈≈≈≈
    CELERY = {
        # Stage 1: Two separate env vars even though both target the same local Redis today,
        # so broker/results can later split onto different DBs or instances by env
        # only - no code changes.
        #
        # Redis numbered-DB split is not real security separation,
        # It helps organize data; real security separation = separate instances.
        "broker_url": os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        "result_backend": os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),

        # json: blocks pickle RCE(Remote Code Execution) and rules out ORM-object args.
        # JSON makes the format safer, but it does not stop you from sending huge data
        # so small/ID-sized messages stay a convention.
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],

        # Explicitly keep Celery's startup retry behavior.
        # In Celery 5.x this avoids the warning about the behavior changing in Celery 6.0.
        "broker_connection_retry_on_startup": True,

        # result_expires is left at Celery's default.
        # With Redis, Celery applies this as a per-key TTL (Time To Live).
        #
        # Tune this at the pre-production gate.
        # Email tasks use ignore_result=True per task, so fire-and-forget sends
        # do not store results in Redis.

        # Stage 3: Route heavy analysis tasks to the ML worker queue.
        # These workers run with LOAD_MODELS=1 and are separate from email/default workers.
        "task_routes": {
            "app.celery_tasks.analyze_message": {"queue": "ml"},
        },
        # Periodic cleanup for stuck analyses.
        # Celery Beat sends this task every 120(2min).
        # The task only uses the DB, so it can run on the default LOAD_MODELS=0 worker.
        "beat_schedule": {
            "sweep-stuck-analyses": {
                "task": "app.celery_tasks.sweep_stuck_analyses",
                "schedule": float(os.getenv("ANALYSIS_SWEEP_INTERVAL_SECONDS", "120"))
            }
        }
    }

    # ≈≈≈≈ Rate limiting (Flask-Limiter) config ≈≈≈≈
    # Own Redis DB (/2), separate from Celery's broker (/0) and results (/1). Same
    # env-var-per-URL convention as CELERY_BROKER_URL, so storage can move to another
    # DB or a dedicated instance by env only — no code changes.
    #
    # As with the Celery block above: the numbered-DB split is organizational, not
    # security separation. Its main benefit is limiting operational impact:
    # for example, running FLUSHDB on the rate-limiter database cannot delete
    # queued Celery tasks or stored task results.
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "redis://localhost:6379/2")

    # Default per-route safeguard for endpoints without an explicit rate limit.
    # Limits are keyed by client IP, and Flask's built-in static endpoint is
    # exempt, so page assets do not consume this budget.
    RATELIMIT_DEFAULT = os.getenv("RATELIMIT_DEFAULT", "300 per hour")

    # Include rate-limit details in response headers, including the remaining
    # request count and when the client may retry after reaching a limit.
    RATELIMIT_HEADERS_ENABLED = True

    # Graceful degradation, matching the posture elsewhere in this app (compare the
    # queue_message_analysis() failure path). If Redis is unreachable, the limiter
    # falls back to per-process in-memory counters instead of raising an error, which
    # would make the rate-limited routes unavailable.
    #
    # RATELIMIT_SWALLOW_ERRORS is deliberately NOT set: it would let requests through
    # without rate limits, which is the wrong failure mode for email-sending routes.
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = True

    # Bound Redis connection and response waits so an unavailable rate-limit
    # backend fails promptly instead of blocking requests until the OS-level
    # network timeout.
    _RATELIMIT_SOCKET_TIMEOUT = int(
        os.getenv("RATELIMIT_SOCKET_TIMEOUT", "2")
    )

    RATELIMIT_STORAGE_OPTIONS = {
        "socket_connect_timeout": _RATELIMIT_SOCKET_TIMEOUT,
        "socket_timeout": _RATELIMIT_SOCKET_TIMEOUT,
    }

    # ≈≈≈≈ Reverse proxy: client IP and scheme ≈≈≈≈
    # These settings are intentionally separate:
    #
    # CLIENT_IP_SOURCE         = client IP from CF-Connecting-IP
    # TRUSTED_PROXY_PROTO_HOPS = scheme from X-Forwarded-Proto
    #
    # Client IP:
    # The public ingress is Cloudflare, in front of Railway. Measured live,
    # CF-Connecting-IP carried the real client on every request, and the
    # leftmost X-Forwarded-For value did not (it is a Cloudflare edge, because
    # Railway sees Cloudflare as its connecting client). So the production
    # deployment uses cf-connecting-ip, and X-Forwarded-For is never read for
    # the client IP in any mode. See ADR-0012.
    #
    # cf-connecting-ip is safe only on requests that actually came through
    # Cloudflare, so the app checks: Cloudflare sets X-Interview-Intel-Origin
    # to CF_ORIGIN_SECRET on every request it forwards, and the middleware
    # trusts CF-Connecting-IP only when that header matches (constant-time).
    # A request that reached Railway around Cloudflare keeps the socket peer.
    # The default is still remote-addr and the mode is opt-in; the secret is
    # read only for cf-connecting-ip and is never logged.
    #
    # Scheme:
    # X-Forwarded-Proto is handled separately with ProxyFix(x_proto=N).
    # It is read from the right, so one trusted TLS terminator means x_proto=1,
    # however many layers (Cloudflare, Railway) sit in front of it.
    #
    # 0 = no trusted proxy; use the real connection scheme
    # 1 = trust one X-Forwarded-Proto value from the nearest TLS terminator
    #
    # Setting this too high can leave request.is_secure false on HTTPS requests,
    # disabling Flask-WTF's strict HTTPS CSRF referer check.
    #
    # The scheme setting assumes the app cannot be reached directly around the
    # ingress; the client-IP setting verifies it per request via the secret.
    CLIENT_IP_SOURCE = _client_ip_source()
    CF_ORIGIN_SECRET = _cf_origin_secret()
    TRUSTED_PROXY_PROTO_HOPS = _proxy_hop_count("TRUSTED_PROXY_PROTO_HOPS")

    # ≈≈≈≈ Security headers (Flask-Talisman) ≈≈≈≈
    # Controls whether this deployment actually uses HTTPS/TLS.
    # It is off by default and enabled explicitly through the environment.
    #
    # Do not base this on FLASK_ENV. The Docker Compose setup uses
    # FLASK_ENV=production while still running locally over plain HTTP
    # (SERVER_NAME=localhost:8000). Automatically enabling HTTPS just because
    # FLASK_ENV=production would therefore break `docker compose up`.
    #
    # This one setting controls HTTPS redirecting, HSTS, and the Secure flag on
    # the session cookie together. All three depend on the same question:
    # "Does this deployment really use HTTPS?" Keeping them together avoids
    # inconsistent setups, such as redirecting to HTTPS while using cookies
    # configured for the wrong transport.
    #
    # CSP is separate and always enabled. This lets CSP problems appear during
    # local development instead of being discovered only after deployment.
    TALISMAN_HTTPS = os.getenv("TALISMAN_HTTPS", "false").strip().lower() in ("1", "true", "yes", "on")

    # HSTS tells browsers to keep using HTTPS for this hostname for the configured
    # amount of time. Start with the final value on the first deployment instead
    # of gradually increasing it, because this deployment is not expected to
    # switch back to HTTP.
    #
    # includeSubDomains and HSTS preload remain disabled, so the HSTS policy applies
    # only to this hostname and does not automatically affect other subdomains.
    #
    # Setting TALISMAN_HSTS_MAX_AGE=0 can clear an existing HSTS policy, but only
    # if the browser can still connect successfully over valid HTTPS and receive
    # that new header. If the TLS certificate is expired or otherwise broken, the
    # browser may reject the connection before it can receive the clearing header.
    # In that case, the existing HSTS policy remains until it expires unless the
    # user clears it manually.
    #
    # For that reason, verify that HTTPS/TLS works correctly before enabling HSTS.
    TALISMAN_HSTS_MAX_AGE = int(os.getenv("TALISMAN_HSTS_MAX_AGE", "31536000"))

    # ≈≈≈≈ External URL building ≈≈≈≈
    # Stage 2: Required so url_for(_external=True) works OUTSIDE a request — e.g. verification /
    # reset links built inside the Celery worker (ADR-0006). SERVER_NAME is app-wide.
    #
    # It does NOT restrict which Host header is accepted: Flask dropped that behavior
    # in 2.3, and this app runs Flask 3.x. Requests with any Host are served normally,
    # which is why the container health probe can hit 127.0.0.1:8000 directly.
    SERVER_NAME = os.getenv("SERVER_NAME")
    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME", "https")

    # ≈≈≈≈ Worker role: model loading ≈≈≈≈
    # Email-only workers set LOAD_MODELS=0 to skip the heavy ML/LLM weight load. (ADR-0008)
    LOAD_MODELS = os.getenv("LOAD_MODELS", "true").strip().lower() in ("1", "true", "yes", "on")

    # ≈≈≈≈ Email send suppression (tests) ≈≈≈≈
    # When true, email tasks record a token-free summary instead of calling Resend, for
    # the live-worker integration test. False in normal operation.
    MAIL_SUPPRESS_SEND = os.getenv("MAIL_SUPPRESS_SEND", "false").strip().lower() in ("1", "true", "yes", "on")

    # ≈≈≈≈ Lifetime analysis quota ≈≈≈≈
    # Maximum number of analyses a user can run. Each submission can trigger 6 paid
    # LLM calls, so this is the hard cost ceiling per account — the per-hour/day
    # rate limits on messages.new_message shape the BURST, this caps the TOTAL.
    #
    # Config-owned on purpose: Store how many analyses each user has used, not how many remain.
    # This allows to change the quota here without updating database records
    # or creating a migration.
    ANALYSIS_LIFETIME_QUOTA = int(os.getenv("ANALYSIS_LIFETIME_QUOTA", "3"))

    # Maximum number of refunds allowed for "nothing to show" results.
    # This path still makes one paid LLM call, so unlimited refunds could let
    # users repeatedly submit unusable input without consuming their quota.
    # Every other FAILED path refunds unconditionally.
    NOTHING_TO_SHOW_REFUND_CAP = int(os.getenv("NOTHING_TO_SHOW_REFUND_CAP", "2"))

    # Used to HMAC the pending-verification address in rate-limit keys, so raw
    # addresses never appear in Redis. Derived from SECRET_KEY rather than being its
    # own variable — the threat model is "don't leave plaintext personal data in
    # Redis", not "survive a full secret compromise" (extensions.py).
    RATELIMIT_KEY_SECRET = os.getenv("RATELIMIT_KEY_SECRET") or SECRET_KEY


class DevelopmentConfig(Config):
    DEBUG = True
    TESTING = False

    SECRET_KEY = os.getenv("SECRET_KEY") or "dev-secret-key"  # SECRET_KEY fallback
    WTF_CSRF_SECRET_KEY = os.getenv("WTF_CSRF_SECRET_KEY") or "dev-csrf-secret-key" # WTF_CSRF_SECRET_KEY fallback
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER") or "onboarding@resend.dev" # MAIL_DEFAULT_SENDER fallback

    # Access the dev server at this host. Port 5001 (not Flask's usual 5000) because macOS
    # AirPlay Receiver squats on port 5000 and answers localhost:5000 with a 403.
    SERVER_NAME = os.getenv("SERVER_NAME", "localhost:5001")
    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME", "http")

    RATELIMIT_KEY_SECRET = os.getenv("RATELIMIT_KEY_SECRET") or SECRET_KEY

class TestingConfig(Config):
    DEBUG = False
    TESTING = True

    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False  # disable CSRF in tests so forms submit cleanly
    SECRET_KEY = "test-secret-key"  # hardcode so tests don't need .env
    WTF_CSRF_SECRET_KEY = "test-csrf-secret-key" # hardcode so tests don't need .env

    SERVER_NAME = "localhost"  # makes url_for(_external=True) deterministic in tests
    PREFERRED_URL_SCHEME = "http"
    LOAD_MODELS = False  # unit tests never need the ML/LLM stack
    MAIL_SUPPRESS_SEND = True  # unit tests never hit Resend

    # Pinned explicitly, not merely inherited: an exported TALISMAN_HTTPS would
    # otherwise turn on force_https and break every test client request, and set
    # Secure on the session cookie so login-flow tests silently lose their session.
    TALISMAN_HTTPS = False

    # Rate limiting OFF in tests: the suite must run with zero external services,
    # and shared limit counters would make repeated requests order-dependent.
    # The memory:// URI means a test that WANTS to exercise limiting can flip
    # RATELIMIT_ENABLED back on per-test without needing Redis.
    RATELIMIT_ENABLED = False
    RATELIMIT_STORAGE_URI = "memory://"

    RATELIMIT_KEY_SECRET = os.getenv("RATELIMIT_KEY_SECRET") or SECRET_KEY

class ProductionConfig(Config):
    DEBUG = False
    TESTING = False

    SECRET_KEY = os.getenv("SECRET_KEY")
    if os.getenv("FLASK_ENV") == "production" and not SECRET_KEY:
        raise ValueError("SECRET_KEY must be set in environment")

    DATABASE_URL = os.getenv("DATABASE_URL")
    if os.getenv("FLASK_ENV") == "production" and not DATABASE_URL:
        raise ValueError("DATABASE_URL must be set in environment")

    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL")
    if os.getenv("FLASK_ENV") == "production" and not CELERY_BROKER_URL:
        raise ValueError("CELERY_BROKER_URL must be set in environment")

    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND")
    if os.getenv("FLASK_ENV") == "production" and not CELERY_RESULT_BACKEND:
        raise ValueError("CELERY_RESULT_BACKEND must be set in environment")

    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI")
    if os.getenv("FLASK_ENV") == "production" and not RATELIMIT_STORAGE_URI:
        raise ValueError("RATELIMIT_STORAGE_URI must be set in environment")

    WTF_CSRF_SECRET_KEY = os.getenv("WTF_CSRF_SECRET_KEY")
    if os.getenv("FLASK_ENV") == "production" and not WTF_CSRF_SECRET_KEY:
        raise ValueError("WTF_CSRF_SECRET_KEY must be set in environment")

    RATELIMIT_KEY_SECRET = os.getenv("RATELIMIT_KEY_SECRET") or SECRET_KEY

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    if os.getenv("FLASK_ENV") == "production" and not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY must be set in environment")

    RESEND_API_KEY = os.getenv("RESEND_API_KEY")
    if os.getenv("FLASK_ENV") == "production" and not RESEND_API_KEY:
        raise ValueError("RESEND_API_KEY must be set in environment")

    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER")
    if os.getenv("FLASK_ENV") == "production" and not MAIL_DEFAULT_SENDER:
        raise ValueError("MAIL_DEFAULT_SENDER must be set in environment")

    # Required so the worker can build correct external links (ADR-0006).
    SERVER_NAME = os.getenv("SERVER_NAME")
    if os.getenv("FLASK_ENV") == "production" and not SERVER_NAME:
        raise ValueError("SERVER_NAME must be set in environment (used to build external email links)")

    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME") or "https"

    # Proxy trust must be explicit in production.
    #
    # CLIENT_IP_SOURCE controls client-IP resolution. Defaulting to the socket peer
    # behind a proxy would put all users in the same rate-limit bucket.
    #
    # TRUSTED_PROXY_PROTO_HOPS controls X-Forwarded-Proto separately; it must never
    # be derived from the X-Forwarded-For configuration. A wrong value can leave
    # request.is_secure false on HTTPS requests. See ADR-0012.
    #
    # These are required only in production because config.py is also imported by
    # development and tests, where no proxy settings may be present.
    _proxy_required = os.getenv("FLASK_ENV") == "production"

    CLIENT_IP_SOURCE = _client_ip_source(required=_proxy_required)

    # The origin secret is what makes cf-connecting-ip safe, so that mode cannot
    # boot without it. remote-addr never reads it, so the Compose stack — which
    # is FLASK_ENV=production with no Cloudflare — is not asked for one.
    CF_ORIGIN_SECRET = _cf_origin_secret(
        required=_proxy_required and CLIENT_IP_SOURCE == "cf-connecting-ip",
    )

    TRUSTED_PROXY_PROTO_HOPS = _proxy_hop_count(
        "TRUSTED_PROXY_PROTO_HOPS",
        "count the trusted X-Forwarded-Proto values from the right, independently "
        "of the client-IP setting: 0 = no proxy, 1 = the nearest TLS terminator "
        "sets it (Railway, with or without Cloudflare in front)",
        required=_proxy_required,
    )


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig
}
