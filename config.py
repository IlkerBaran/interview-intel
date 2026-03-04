import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent

class Config:
    """
    Configuration Class for the Flask app.

    Handles environment-based settings, security configuration,
    and database connection setup for both development and production.
    """
    SECRET_KEY = os.getenv("SECRET_KEY")
    if not SECRET_KEY:
        raise ValueError("SECRET_KEY must be set in environment")

    db_url = os.getenv("DATABASE_URL")
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    SQLALCHEMY_DATABASE_URI = db_url or f"sqlite:///{BASE_DIR / 'data.sqlite'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

