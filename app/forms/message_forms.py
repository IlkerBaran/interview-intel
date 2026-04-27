from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, EmailField, SubmitField
from wtforms.validators import DataRequired, Email, Optional, Length

from app.utils import normalize_email, normalize_input
from app.models import MessageCategory, MessageUrgency


class MessageSubmissionForm(FlaskForm):
    """
    Form for submitting an interview-related email for analysis
    """
    subject = StringField(
        "Email Subject",
        validators=[
            Optional(),
            Length(max=255, message="Email subject must be less than 255 characters.")
        ],
        filters=[normalize_input],
        render_kw={"placeholder": "Interview Invitation - Position Name"}
    )

    sender_email = EmailField(
        "Sender Email",
        validators=[
            Optional(),
            Email(message= "Enter a valid email address."),
            Length(max=255, message="Email must be less than 255 characters.")
        ],
        filters=[normalize_email],
        render_kw={"placeholder": "recruiter@company.com"}
    )

    raw_text = TextAreaField(
        "Email Content",
        validators=[
            DataRequired(message="Please paste the email content to analyze."),
            Length(min=10, max=20000, message="Email content must be between 10 and 20,000 characters.")
        ],
        filters=[normalize_input],
        render_kw={
            "rows": 10,
            "placeholder": "Paste the full interview email here..."}
    )

    submit = SubmitField("Analyze Email")


class MessageSearchForm(FlaskForm):
    """
    Allows users to search previously analyzed interview emails
    """
    query = StringField(
        "Search Message",
        validators=[
            Optional(),
            Length(max=200, message="Search query must be less than 200 characters.")
        ],
        render_kw={"placeholder": "Search company, role, or keywords"}
        )

    submit = SubmitField("Search")


class MessageFilterForm(FlaskForm):
    """
    Filters messages by category and urgency level.
    No-op on SelectField but kept for WTForms compatibility
    """
    category = SelectField(
        "Category",
        choices=[("", "All")] + [(c.value, c.label) for c in MessageCategory],
        validators=[Optional()],
        coerce=str
    )

    urgency = SelectField(
        "Urgency",
        choices=[("", "All")] + [(u.value, u.label) for u in MessageUrgency],
        validators=[Optional()],
        coerce=str
    )

    submit = SubmitField("Apply Filters")


class MessageNoteForm(FlaskForm):
    """
    Allows users to attach personal notes to a message
    """
    note = TextAreaField(
        "Note",
        validators=[
            Optional(),
            Length(max=400, message="Note cannot exceed 400 characters.")
        ],
        filters=[normalize_input],
        render_kw={
            "placeholder": "Add personal note about this interview...",
            "rows": 4
        }
    )

    submit = SubmitField("Add Note")
