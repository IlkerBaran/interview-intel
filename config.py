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
      - Loads core settings such as `SECRET_KEY`, CSRF configuration, and database URI.
      - Uses environment variables when available, with a fallback `SECRET_KEY`
        for development convenience.
      - Supports PostgreSQL (with URI normalization) and defaults to SQLite locally.

    - `DevelopmentConfig`:
      - Inherits base settings.
      - Enables debugging for local development.

    - `TestingConfig`:
      - Uses an in-memory SQLite database for fast, isolated tests.
      - Disables CSRF to simplify form testing.
      - Overrides `SECRET_KEY` with a fixed value to avoid dependency on environment variables.

    - `ProductionConfig`:
      - Disables debug and testing modes.
      - Requires a valid `SECRET_KEY` from the environment for security.

    `config_by_name` maps environment names to their corresponding configuration
    classes, allowing dynamic selection via `FLASK_ENV`.
    """
    SECRET_KEY = os.getenv("SECRET_KEY")
    WTF_CSRF_SECRET_KEY = os.getenv("WTF_CSRF_SECRET_KEY", SECRET_KEY)

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


class DevelopmentConfig(Config):
    DEBUG = True
    TESTING = False

    SECRET_KEY = os.getenv("SECRET_KEY") or "dev-secret-key"  # SECRET_KEY fallback
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "onboarding@resend.dev") # MAIL_DEFAULT_SENDER fallback

    # Access the dev server at this host. Port 5001 (not Flask's usual 5000) because macOS
    # AirPlay Receiver squats on port 5000 and answers localhost:5000 with a 403.
    SERVER_NAME = os.getenv("SERVER_NAME", "localhost:5001")
    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME", "http")

class TestingConfig(Config):
    DEBUG = False
    TESTING = True

    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False  # disable CSRF in tests so forms submit cleanly
    SECRET_KEY = "test-secret-key"  # hardcode so tests don't need .env

    SERVER_NAME = "localhost"  # makes url_for(_external=True) deterministic in tests
    PREFERRED_URL_SCHEME = "http"
    LOAD_MODELS = False  # unit tests never need the ML/LLM stack
    MAIL_SUPPRESS_SEND = True  # unit tests never hit Resend

class ProductionConfig(Config):
    DEBUG = False
    TESTING = False

    SECRET_KEY = os.getenv("SECRET_KEY")
    if not SECRET_KEY:
        raise ValueError("SECRET_KEY must be set in environment")

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY must be set in environment")

    RESEND_API_KEY = os.getenv("RESEND_API_KEY")
    if not RESEND_API_KEY:
        raise ValueError("RESEND_API_KEY must be set in environment")

    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER")
    if not MAIL_DEFAULT_SENDER:
        raise ValueError("MAIL_DEFAULT_SENDER must be set in environment")

    # Required so the worker can build correct external links (ADR-0006).
    SERVER_NAME = os.getenv("SERVER_NAME")
    if not SERVER_NAME:
        raise ValueError("SERVER_NAME must be set in environment (used to build external email links)")

    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME", "https")

config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig
}
