"""
Operator CLI commands.

`flask verify-user` lets me manually mark a user's email as verified without
using the emailed verification token.

This is mainly for local development or fresh-clone testing when Resend is not
set up. Without it, a new user can get stuck at /auth/unverified.

This is not another verification method. It skips the normal proof that the
user owns the email, so:
  * it refuses to run with FLASK_ENV=production unless --force is used
  * it should never be added to a route, entrypoint, or container command

The production check is only there to prevent mistakes. Anyone who can run this
command already has shell and database access.
"""


import os

import click
from flask.cli import with_appcontext

from app.extensions import db
from app.models import User
from app.utils import normalize_email


# Exit codes, distinct so a setup script can tell the two refusals apart.
EXIT_NO_SUCH_USER = 1
EXIT_REFUSED_IN_PRODUCTION = 2
EXIT_DB_ERROR = 3


@click.command("verify-user")
@click.argument("email")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Run even when FLASK_ENV=production. Required there; skips proof of email ownership.",
)
@with_appcontext
def verify_user_command(email: str, force: bool) -> None:
    """
    Mark the account with EMAIL as verified, bypassing the emailed token.

    Intended for local development and evaluation when no mail provider is configured.
    """
    # Normalized the same way the registration form normalizes it (strip + lowercase),
    # so `Some@Example.COM ` finds the row stored as `some@example.com`. Without this the
    # command would report "no such user" for an account that plainly exists.
    email = normalize_email(email)

    # Read the environment variable directly rather than a config key: that is how
    # config.py itself decides what "production" means, and the two must not drift.
    if os.getenv("FLASK_ENV") == "production" and not force:
        raise click.exceptions.Exit(_fail(
            "Refusing to run: FLASK_ENV=production.\n"
            "This bypasses proof of email ownership. If you really mean it on this "
            "environment, re-run with --force.",
            EXIT_REFUSED_IN_PRODUCTION,
        ))

    user = db.session.execute(
        db.select(User).where(User.email == email)
    ).scalar_one_or_none()

    if user is None:
        raise click.exceptions.Exit(_fail(
            f"No user with email {email}", EXIT_NO_SUCH_USER))

    # Idempotent on purpose: a local setup script can call this unconditionally without
    # having to branch on current state, and a second run is not an error.
    if user.is_verified:
        click.echo(f"{email} is already verified — nothing to do.")
        return

    # Mirrors auth_service.verify_email_token(): a live token left behind would still be
    # redeemable, so clear it here too rather than only flipping the flag.
    user.is_verified = True
    user.verification_token = None
    user.token_expires_at = None

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        raise click.exceptions.Exit(_fail(
            f"Could not verify {email} — {type(e).__name__}", EXIT_DB_ERROR))

    click.echo(f"{email} is now verified.")


def _fail(message: str, code: int) -> int:
    """Write to stderr and return the exit code, so failures are greppable in scripts."""
    click.echo(message, err=True)
    return code


def register_cli(app) -> None:
    """Attach the operator commands. Called from create_app()."""
    app.cli.add_command(verify_user_command)
