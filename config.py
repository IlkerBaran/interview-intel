import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent

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
      - Requires `SECRET_KEY`, `WTF_CSRF_SECRET_KEY`, `ANTHROPIC_API_KEY`,
        `RESEND_API_KEY`, `MAIL_DEFAULT_SENDER`, `SERVER_NAME` and
        `TRUSTED_PROXY_HOPS` to be set in the environment, raising on startup if any
        is missing.
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

    # ≈≈≈≈ Reverse proxy hops (X-Forwarded-For) ≈≈≈≈
    #   0 = no trusted proxy; use the direct connection address
    #   1 = one trusted reverse proxy or platform router
    #   2 = two trusted layers, such as a CDN followed by a reverse proxy
    #
    # ProxyFix selects the client address based on this many trusted values from
    # the right side of X-Forwarded-For. Configure this to match the deployed
    # proxy chain exactly. Too few may identify a shared proxy as the client;
    # too many may trust a client-supplied value and allow IP-based limits to be
    # bypassed.
    #
    # Verify the deployed platform's header behavior and ensure the application
    # cannot be reached directly around the trusted proxies.
    TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))

    # ≈≈≈≈ External URL building ≈≈≈≈
    # Stage 2: Required so url_for(_external=True) works OUTSIDE a request — e.g. verification /
    # reset links built inside the Celery worker (ADR-0006). SERVER_NAME is app-wide:
    # it also makes the web app enforce Host-header matching (acceptable, single domain).
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

    # Rate limiting keys on the client IP, so the hop count must be stated explicitly
    # in production: silently defaulting to 0 behind a proxy puts every user in one
    # bucket and the global limit locks out the whole site.
    #
    # The FLASK_ENV check is required, not redundant: this class body executes on every
    # import of config.py — including under development/testing, where
    # TRUSTED_PROXY_HOPS is not set — so an unconditional raise would break dev startup.
    TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "-1"))
    if TRUSTED_PROXY_HOPS < 0 and os.getenv("FLASK_ENV") == "production":
        raise ValueError(
            "TRUSTED_PROXY_HOPS must be set in environment "
            "(0 = no proxy, 1 = one nginx / PaaS router / ALB, 2 = Cloudflare -> nginx)"
        )


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig
}
