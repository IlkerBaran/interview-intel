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

    # ≈≈≈≈ Resend config ≈≈≈≈
    RESEND_API_KEY = os.getenv("RESEND_API_KEY")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER")

    # ≈≈≈≈ Email and Password expiry hours config ≈≈≈≈
    EMAIL_VERIFICATION_TOKEN_EXPIRY_HOURS = 24
    PASSWORD_RESET_TOKEN_EXPIRY_HOURS = 1

    # ≈≈≈≈ Celery / Redis config ≈≈≈≈
    # Two separate env vars even though both target the same local Redis today,
    # so broker/results can later split onto different DBs or instances by env
    # only - no code changes. (Numbered-DB split is not real security separation,
    # It helps organize data; real security separation = separate instances.)
    CELERY = {
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
        # result_expires: left at Celery's default (Redis applies it as a per-key
        # TTL(Time To Live)).
        # Tune at the pre-production gate;
        # Email tasks set ignore_result=True per-task (see celery_tasks.py) so
        # fire-and-forget sends don't store results in Redis.
    }

    # ≈≈≈≈ External URL building ≈≈≈≈
    # Required so url_for(_external=True) works OUTSIDE a request — e.g. verification /
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

    # Access the dev server at this exact host (Flask enforces it once SERVER_NAME is set).
    SERVER_NAME = os.getenv("SERVER_NAME", "localhost:5000")
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