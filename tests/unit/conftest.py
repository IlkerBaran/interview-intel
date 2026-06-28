"""
Fixtures for the fast unit suite.

Uses testing config with:
- an in-memory database
- ML/LLM model loading disabled
- email sending suppressed

This file helps:
- Avoid repeating setup code in every test.
- Make tests fast by using testing config.
- Avoid real email sending.
- Avoid loading heavy ML/LLM models.
- Give each test a clean temporary database.
- Provide reusable fake data, like an unverified user.
"""


import os

# Set testing mode before importing the Flask app, because config.py reads
# FLASK_ENV during app creation to choose TestingConfig.
os.environ.setdefault("FLASK_ENV", "testing")

import pytest # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture
def app():
    application = create_app()
    if application.config.get("TESTING") is not True:
        raise RuntimeError(
            "Test suite is not running under TestingConfig — "
            "refusing to run against a non-test database."
        )

    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def unverified_user(app):
    user = User()
    user.email = "test_user@example.com"
    user.password = "T3st-secret*"
    user.is_verified = False

    _db.session.add(user)
    _db.session.commit()

    return user
