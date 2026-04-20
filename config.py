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


class DevelopmentConfig(Config):
    DEBUG = True
    TESTING = False

    SECRET_KEY = os.getenv("SECRET_KEY") or "dev-secret-key"  # SECRET_KEY fallback
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "onboarding@resend.dev") # MAIL_DEFAULT_SENDER fallback


class TestingConfig(Config):
    DEBUG = False
    TESTING = True

    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False  # disable CSRF in tests so forms submit cleanly
    SECRET_KEY = "test-secret-key"  # hardcode so tests don't need .env


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


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig
}