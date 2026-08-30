"""
`flask verify-user <email>` — the local-evaluation path past email verification.

Exists because verification is otherwise reachable only through a Resend-delivered
token: with no mail provider configured, registration succeeds, login is refused at
/auth/unverified, and the application cannot be entered at all.

What these tests pin down:
  * it verifies, and clears the token fields the way the real token flow does
  * it is idempotent, so a setup script can call it unconditionally
  * it matches the email the way the registration form stores it
  * it refuses production unless --force, and --force actually works
"""

import pytest

from app.cli import (
    EXIT_NO_SUCH_USER,
    EXIT_REFUSED_IN_PRODUCTION,
    verify_user_command,
)
from app.extensions import db
from app.models import User


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


def _make_user(email="cli@example.com", verified=False, token="tok-123"):
    user = User()
    user.email = email
    user.password = "T3st-secret*"
    user.is_verified = verified
    user.verification_token = None if verified else token
    db.session.add(user)
    db.session.commit()
    return user


def _reload(user):
    db.session.expire_all()
    return db.session.get(User, user.id)


# ══════════════════════════════════════════════════════════════════════════════
# the happy path
# ══════════════════════════════════════════════════════════════════════════════

def test_verifies_an_unverified_user(app, runner):
    user = _make_user()

    result = runner.invoke(verify_user_command, ["cli@example.com"])

    assert result.exit_code == 0
    assert "is now verified" in result.output
    assert _reload(user).is_verified is True


def test_clears_the_outstanding_token(app, runner):
    """
    Mirrors auth_service.verify_email_token(). Flipping the flag but leaving a live
    token behind would keep that token redeemable after the account is already in.
    """
    user = _make_user(token="still-live")

    runner.invoke(verify_user_command, ["cli@example.com"])

    fresh = _reload(user)
    assert fresh.verification_token is None
    assert fresh.token_expires_at is None


def test_is_idempotent_for_an_already_verified_user(app, runner):
    """Exit 0 and a no-op, so an unconditional call in a setup script is safe."""
    user = _make_user(verified=True)

    result = runner.invoke(verify_user_command, ["cli@example.com"])

    assert result.exit_code == 0
    assert "already verified" in result.output
    assert _reload(user).is_verified is True


@pytest.mark.parametrize("typed", [
    "CLI@Example.COM",   # case
    "  cli@example.com", # leading space
    "cli@example.com  ", # trailing space
])
def test_matches_the_email_the_way_registration_stores_it(app, runner, typed):
    """
    The registration form normalizes through the same helper before saving. Without
    the same normalization here, the command reports "no user" for an account that
    plainly exists — the most likely way for this to waste someone's afternoon.
    """
    user = _make_user(email="cli@example.com")

    result = runner.invoke(verify_user_command, [typed])

    assert result.exit_code == 0
    assert _reload(user).is_verified is True


# ══════════════════════════════════════════════════════════════════════════════
# missing user
# ══════════════════════════════════════════════════════════════════════════════

def test_missing_user_exits_nonzero(app, runner):
    result = runner.invoke(verify_user_command, ["nobody@example.com"])

    assert result.exit_code == EXIT_NO_SUCH_USER
    assert "No user with email nobody@example.com" in result.output


def test_missing_user_writes_nothing(app, runner):
    _make_user(email="other@example.com")

    runner.invoke(verify_user_command, ["nobody@example.com"])

    assert db.session.execute(
        db.select(db.func.count(User.id)).where(User.is_verified.is_(True))
    ).scalar() == 0


# ══════════════════════════════════════════════════════════════════════════════
# the production guard
# ══════════════════════════════════════════════════════════════════════════════

def test_production_refuses_without_force(app, runner, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    user = _make_user()

    result = runner.invoke(verify_user_command, ["cli@example.com"])

    assert result.exit_code == EXIT_REFUSED_IN_PRODUCTION
    assert "--force" in result.output
    assert _reload(user).is_verified is False   # refusal means refusal


def test_production_proceeds_with_force(app, runner, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    user = _make_user()

    result = runner.invoke(verify_user_command, ["cli@example.com", "--force"])

    assert result.exit_code == 0
    assert _reload(user).is_verified is True


def test_non_production_does_not_need_force(app, runner, monkeypatch):
    """
    The guard keys on FLASK_ENV=production only. Local Docker sets FLASK_ENV=production
    even though it runs on a laptop, which is exactly why --force is documented for the
    Docker path rather than the guard being loosened to guess at "is this really prod".
    """
    monkeypatch.setenv("FLASK_ENV", "development")
    user = _make_user()

    result = runner.invoke(verify_user_command, ["cli@example.com"])

    assert result.exit_code == 0
    assert _reload(user).is_verified is True


def test_force_is_harmless_outside_production(app, runner, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    user = _make_user()

    result = runner.invoke(verify_user_command, ["cli@example.com", "--force"])

    assert result.exit_code == 0
    assert _reload(user).is_verified is True


# ══════════════════════════════════════════════════════════════════════════════
# registration
# ══════════════════════════════════════════════════════════════════════════════

def test_command_is_registered_on_the_app(app):
    """`flask verify-user` must actually be discoverable, not just importable."""
    assert "verify-user" in app.cli.commands
