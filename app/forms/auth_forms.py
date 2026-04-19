from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError

from app.extensions import db
from app.models import User


class RegisterForm(FlaskForm):
    email = StringField(
        "Email",
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email must be less than 256 characters."),
        ],
        render_kw={"placeholder": "Enter your email"},
    )

    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message='Password is required.'),
            Length(min=8, max=255, message="Password must be between 8 and 255 characters."),
        ],
        render_kw={"placeholder": "Enter your password"}
    )

    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(message="Please confirm you password."),
            EqualTo("password", message="Passwords must match."),
        ],
        render_kw={"placeholder": "Re-enter your password"},
    )

    submit = SubmitField("Sign Up")

    def validate_email(self, email):
        """Normalize email and prevent duplicate registration."""
        email.data = email.data.strip().lower()

        existing_user = db.session.execute(
            db.select(User).where(
                User.email == email.data
            )
        ).scalar_one_or_none()

        if existing_user and existing_user.is_verified:
            raise ValidationError("Email is already registered.")


class LoginForm(FlaskForm):
    email = StringField(
        "Email",
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email must be less than 256 characters."),
        ],
        render_kw={"placeholder": "Enter your email"},
    )

    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(max=255, message="Password is too long."),
        ]
    )

    remember_me = BooleanField("Remember me")

    submit = SubmitField("Sign In")

    def validate_email(self, email):
        """Normalize email input for login."""
        email.data = email.data.strip().lower()


