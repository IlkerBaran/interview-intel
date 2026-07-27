from datetime import date
from urllib.parse import urlparse

from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, DateField, TextAreaField, SubmitField
from wtforms.validators import DataRequired, Length, Optional, URL, ValidationError

from app.models import ApplicationStatus
from app.utils import normalize_input


class JobApplicationForm(FlaskForm):
    """
    Form for creating and editing job applications
    """
    company = StringField(
        "Company",
        validators=[
            DataRequired(message="Company name is required."),
            Length(max=150, message="Company name must be less than 150 characters.")
        ],
        filters=[normalize_input],
        render_kw={"placeholder": "e.g. Google"}
    )

    role = StringField(
        "Role",
        validators=[
            DataRequired(message="Role name is required."),
            Length(max=150, message="Role name must be less than 150 characters.")
        ],
        filters=[normalize_input],
        render_kw={"placeholder": "e.g. Software Engineer Intern"}
    )

    status = SelectField(
        "Status",
        choices=[(s.value, s.label) for s in ApplicationStatus],
        default=ApplicationStatus.SAVED.value,
        coerce=str
    )

    source = StringField(
        "Source",
        validators=[
            Optional(),
            Length(max=100, message="Source must be less than 100 characters.")
        ],
        filters=[normalize_input],
        render_kw={"placeholder": "e.g. LinkedIn, referral, company site"}
    )

    posting_url = StringField(
        "Job posting URL",
        validators=[
            Optional(),
            URL(message="Enter a valid URL."),
            Length(max=500),
        ],
        filters=[normalize_input],
        render_kw={"placeholder": "https://", "type": "url"},
    )

    applied_date = DateField(
        "Date applied",
        validators=[Optional()],
        format="%Y-%m-%d",
    )

    note = TextAreaField(
        "Note",
        validators=[
            Optional(),
            Length(max=400, message="Note must be less than 400 characters.")
        ],
        filters=[normalize_input],
        render_kw={
            "placeholder": "Anything worth remembering about this application...",
            "rows": 4
        }
    )

    submit = SubmitField("Save application")


    def validate_applied_date(self, field):
        """Ensure applied date is not in the future or too far in the past."""
        if field.data:
            if field.data > date.today():
                raise ValidationError("Applied date cannot be in the future.")
            if field.data < date(2000, 1, 1):
                raise ValidationError("Applied date is too far in the past.")


    def validate_posting_url(self, field):
        """Ensure URL uses http or https scheme."""
        if field.data:
            if urlparse(field.data).scheme.lower() not in ("http", "https"):
                raise ValidationError("URL must use http or https.")
