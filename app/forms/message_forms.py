from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, SubmitField
from wtforms.validators import DataRequired, Email, Optional, Length

class MessageSubmissionForm(FlaskForm):
    """
    Form for submitting an interview-related email for analysis
    """

    subject = StringField(
        "Email Subject",
        validators=[Optional(), Length(max=255)],
        render_kw={"placeholder": "Interview Invitation - Position Name"}
    )

    sender_email = StringField(
        "Sender Email",
        validators=[Optional(), Email(), Length(max=255)],
        render_kw={"placeholder": "recruiter@company.com"}
    )

    raw_text = TextAreaField(
        "Email Content",
        validators=[DataRequired(message="Please paste the email content to analyze."),
                    Length(min=20, message="Email content too short to analyze.")],
        render_kw={
            "rows": 10,
            "placeholder": "Paste the full interview email here..."}
    )

    submit = SubmitField("Analyze Email")


class MessageSearchForm(FlaskForm):
    """
    Allows users to search  previously analyzed interview emails
    """

    query = StringField(
        "Search Message",
        validators=[Optional()],
        render_kw={"placeholder": "Search company, role, or keywords"}
        )

    submit = SubmitField("Search")

class MessageFilterForm(FlaskForm):
    """
    Filters messages based on analysis result, such as category and urgency level.
    """

    category = SelectField(
        "Category",
        choices=[
            ("", "All"),
            ("interview_invitation", "Interview Invitation"),
            ("recruiter_outreach", "Recruiter Outreach"),
            ("rejection", "Rejection"),
            ("scheduling", "Scheduling")
        ],
        validators=[Optional()]
    )

    urgency = SelectField(
        "Urgency",
        choices=[
            ("", "All"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High")
        ],
        validators=[Optional()]
    )

    submit = SubmitField("Apply Filters")


class MessageNoteForm(FlaskForm):
    """
    Allows users to attach personal notes to a message
    """

    note = TextAreaField(
        "Note",
        validators=[Optional(), Length(max=400, message="Note cannot exceed 400 characters.")],
        render_kw={
            "rows": 4,
            "placeholder": "Add personal note about this interview..."
        }
    )

    submit = SubmitField("Add Note")




