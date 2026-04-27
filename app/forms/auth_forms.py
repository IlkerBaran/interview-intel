from flask_wtf import FlaskForm
from wtforms import PasswordField, BooleanField, EmailField, SubmitField
from wtforms.validators import DataRequired, Length, Email, EqualTo

from app.utils import normalize_email


class RegisterForm(FlaskForm):
    email = EmailField(
        "Email",
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email must be less than 255 characters."),
        ],
        filters=[normalize_email],
        render_kw={"placeholder": "Enter your email"},
    )

    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(min=8, max=255, message="Password must be between 8 and 255 characters."),
        ],
        render_kw={"placeholder": "Enter your password"},
    )

    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo("password", message="Passwords must match."),
        ],
        render_kw={"placeholder": "Re-enter your password"},
    )

    submit = SubmitField("Sign Up")


class LoginForm(FlaskForm):
    email = EmailField(
        "Email",
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email must be less than 255 characters."),
        ],
        filters=[normalize_email],
        render_kw={"placeholder": "Enter your email"},
    )

    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(max=255, message="Password is too long."),
        ],
        render_kw={"placeholder": "Enter your password"},
    )

    remember_me = BooleanField("Remember me")

    submit = SubmitField("Sign In")


class ForgotPasswordForm(FlaskForm):
    email = EmailField(
        "Email",
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email must be less than 255 characters."),
        ],
        filters=[normalize_email],
        render_kw={"placeholder": "Enter your email"},
    )

    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(min=8, max=255, message="Password must be between 8 and 255 characters."),
        ],
        render_kw={"placeholder": "Enter new password"},
    )

    confirm_password = PasswordField(
        "Confirm New Password",
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo("password", message="Passwords must match."),
        ],
        render_kw={"placeholder": "Re-enter new password"},
    )

    submit = SubmitField("Update password")
