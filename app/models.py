from datetime import datetime, UTC
from .extensions import db
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from enum import Enum


class BaseEnum(str, Enum):
    @property
    def label(self):
        """
        Generate a display-friendly label for the enum member.

        Converts enum names like "INTERVIEW_DETECTED"
        into "Interview Detected" or "SAVED" → "Saved".
        """
        return self.name.replace("_", " ").title()


class ApplicationStatus(BaseEnum):
    """
    Define the allowed application status values in one place to prevent typos.
    """
    SAVED = 'saved'
    APPLIED = 'applied'
    REJECTED = 'rejected'
    INTERVIEWING = 'interviewing'
    OFFERED = 'offered'
    WITHDRAWN = 'withdrawn'


# ≈≈≈≈ ApplicationStatus rule sets ≈≈≈≈
ACTIVE_APPLICATION_STATUSES = frozenset({
    ApplicationStatus.APPLIED,
    ApplicationStatus.INTERVIEWING
})

LOCKED_APPLICATION_STATUSES = frozenset({
    ApplicationStatus.SAVED,
    ApplicationStatus.OFFERED,
    ApplicationStatus.WITHDRAWN
})


class NotificationType(BaseEnum):
    """
    Define the notification type values in one place to prevent typos.
    """
    INTERVIEW_DETECTED = 'interview_detected'
    OFFER_DETECTED = 'offer_detected'
    TASK_DUE = 'task_due'
    APPLICATION_UPDATED = 'application_updated'


class MessageStatus(BaseEnum):
    """
    Define the allowed message status values in one place to prevent typos.
    """
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


class MessageCategory(BaseEnum):
    """
    Define the allowed message category values in one place to prevent typos.
    """
    INTERVIEW_INVITATION = 'interview_invitation'
    RECRUITER_OUTREACH = 'recruiter_outreach'
    REJECTION = 'rejection'
    SCHEDULING = 'scheduling'
    OFFER = 'offer'
    FOLLOW_UP = 'follow_up'


class MessageUrgency(BaseEnum):
    """
    Define the allowed message urgency values in one place to prevent typos.
    """
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'


class TaskPriority(BaseEnum):
    """
    Define the allowed task priority values in one place to prevent typos.
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentRunStatus(BaseEnum):
    """
    Define the allowed agentRun status values in one place to prevent typos.
    """
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class User(UserMixin, db.Model):
    """
    Represents an application user.

    Stores authentication credentials and links to messages
    submitted by the user.
    """

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    verification_token = db.Column(db.String(100), nullable=True, index=True, unique=True)
    token_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    password_reset_token = db.Column(db.String(100), nullable=True, index=True, unique=True)
    password_reset_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)


    # Prevents direct access to the password attribute.
    @property
    def password(self):
        raise AttributeError("password is not a readable attribute")

    # Catches the raw user password comes from the user,
    # then auto hashes the raw password before storing it
    @password.setter
    def password(self, password):
        self.password_hash = generate_password_hash(password)

    # Verifies a login attempt by comparing the entered password
    # with the stored hashed password.
    # Returns True if the password matches, otherwise False.
    def verify_password(self, password):
        return check_password_hash(self.password_hash, password)


    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC)
    )

    # users table relations with other tables
    job_applications = db.relationship(
        'JobApplication',
        back_populates='user',
        lazy='select',
        cascade='all, delete-orphan'
    )

    messages = db.relationship(
        "Message",
        back_populates="user",
        cascade="all, delete-orphan"
    )

    notifications = db.relationship(
        'Notification',
        back_populates='user',
        lazy='select',
        cascade='all, delete-orphan'
    )

    # debugging
    def __repr__(self):
        return f"<User id={self.id} email={self.email}>"


class JobApplication(db.Model):
    """
    Represents a job application tracked by the user.

    This is the central anchor of the application tracking system.
    Every email analyzed by Interview Intel can be linked back to
    a JobApplication, connecting the full story of one application
    in one place.

    Relationships:
        - belongs to User (many-to-one)
        - has many Messages (one-to-many) via auto-link or manual assignment

    Status flow:
        SAVED → APPLIED → RESPONDED → INTERVIEWING → OFFERED / REJECTED
    """

    __tablename__ = 'job_applications'

    # Data columns
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    company = db.Column(db.String(150), nullable=False, index=True)
    role = db.Column(db.String(150), nullable=False)
    job_field = db.Column(db.String(100), nullable=True)
    source = db.Column(db.String(100), nullable=True)
    posting_url = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(50), nullable=False, default=ApplicationStatus.SAVED, index=True)
    applied_date = db.Column(db.Date, nullable=True)
    note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC)
    )

    user = db.relationship(
        'User',
        back_populates='job_applications'
    )

    messages = db.relationship(
        'Message',
        back_populates='job_application',
        lazy='select'
    )

    # debugging purpose for this table
    def __repr__(self):
        return f'<JobApplication {self.company} - {self.role}>'


class Message(db.Model):
    """
    Represents an incoming email or message that the system analyzes.

    Stores the raw message content and metadata such as subject,
    sender email, status, and creation time. A message can have one
    analysis result, multiple tasks, and multiple agent run logs.
    """

    __tablename__ = "messages"

    # Data columns
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    job_application_id = db.Column(db.Integer, db.ForeignKey('job_applications.id'), nullable=True, index=True)

    subject = db.Column(db.String(255))
    sender_email = db.Column(db.String(255), index=True)
    raw_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, index=True, default=MessageStatus.PENDING)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC)
    )

    # Message table relations with other tables
    user = db.relationship(
        "User",
        back_populates="messages",
    )

    job_application = db.relationship(
        'JobApplication',
        back_populates='messages'
    )

    analysis_result = db.relationship(
        "AnalysisResult",
        back_populates="message",
        uselist=False,
        cascade="all, delete-orphan")

    tasks = db.relationship(
        "Task",
        back_populates="message",
        cascade="all, delete-orphan")

    agent_runs = db.relationship(
        "AgentRun",
        back_populates="message",
        cascade="all, delete-orphan")


    def to_dict(self):
        """
        Convert the Message object into a dictionary format.

        Used when returning message data as JSON (e.g., API responses or
        JavaScript fetch calls). Only includes fields relevant for client display.
        """
        return {
            "subject": self.subject,
            "sender_email": self.sender_email,
            "status": self.status,
            "note": self.note,
            "raw_text": self.raw_text,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


    # debugging purpose for this table
    def __repr__(self):
        return f"<Message id={self.id} status={self.status}>"


class Notification(db.Model):
    """
    Represents an in-app notification for the user.

    Notifications are created automatically by the workflow service
    when significant events are detected — such as an interview
    invitation or offer being identified in an analyzed email.

    Status flow:
        created (is_read=False) → read (is_read=True)

    Relationships:
        - belongs to User (many-to-one)
    """

    __tablename__ = 'notifications'

    # data columns
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    message = db.Column(db.String(300), nullable=False)
    notification_type = db.Column(db.String(50), nullable=False, index=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    user = db.relationship(
        'User',
        back_populates='notifications'
    )

    # debugging purpose for this table
    def __repr__(self):
        return f'<Notification {self.type} - read={self.is_read}>'


class AnalysisResult(db.Model):
    """
    Represents the structured analysis generated from a message.

    Stores ML predictions, extracted interview details, and LLM
    generated outputs such as preparation guidance and reply
    suggestions. Each analysis result is linked to one message.
    """

    __tablename__ = "analysis_results"

    # data columns
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=False, unique=True)

    # ML predictions
    message_category = db.Column(db.String(100), index=True)
    category_confidence = db.Column(db.Float)

    urgency_level = db.Column(db.String(50), index=True)
    urgency_confidence = db.Column(db.Float)

    job_field = db.Column(db.String(100), index=True)
    job_field_confidence = db.Column(db.Float)

    # Base extraction / structured interview details
    company_name = db.Column(db.String(255))
    role_title = db.Column(db.String(255))
    interview_stage = db.Column(db.String(100))
    interview_format = db.Column(db.String(100))
    date_text = db.Column(db.String(100))
    time_text = db.Column(db.String(100))
    location_text = db.Column(db.String(255))
    detected_language = db.Column(db.String(15), nullable=True, index=True)
    scheduled_at = db.Column(db.DateTime(timezone=True))
    processed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # LLM generated outputs
    preparation_guidance = db.Column(db.Text)
    suggested_questions = db.Column(db.Text)
    reply_suggestions = db.Column(db.Text)
    archive_summary = db.Column(db.Text)
    role_summary = db.Column(db.Text)

    # analysis_results table relations with messages table
    message = db.relationship(
        "Message",
        back_populates="analysis_result")


    def to_dict(self):
        """
        Convert the analysis result into grouped dictionary data.

        Separates ML predictions, extracted interview details, LLM-generated
        outputs, and metadata so the frontend can render each section clearly
        without mixing unrelated fields together.
        """
        return {
            "predictions": {
                "message_category": self.message_category,
                "category_confidence": self.category_confidence,
                "urgency_level": self.urgency_level,
                "urgency_confidence": self.urgency_confidence,
                "job_field": self.job_field,
                "job_field_confidence": self.job_field_confidence,
            },
            "interview_details": {
                "company_name": self.company_name,
                "role_title": self.role_title,
                "interview_stage": self.interview_stage,
                "interview_format": self.interview_format,
                "date_text": self.date_text,
                "time_text": self.time_text,
                "location_text": self.location_text,
                "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            },
            "llm_outputs": {
                "preparation_guidance": self.preparation_guidance,
                "suggested_questions": self.suggested_questions,
                "reply_suggestions": self.reply_suggestions,
            },
            "meta": {
                "processed_at": self.processed_at.isoformat() if self.processed_at else None,
                "archive_summary": self.archive_summary,
                "role_summary": self.role_summary,
            }
        }


    # debugging purpose for this table
    def __repr__(self):
        return f"<AnalysisResult id={self.id} message_id={self.message_id}>"


class Task(db.Model):
    """
    Represents an actionable task generated from an analyzed message.

    Stores tasks created by the backend workflow to help the user
    respond to interview-related emails, such as confirming an
    interview time, researching a company, or preparing technical
    topics. Each task is linked to the message that triggered it.
    """

    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=False)

    task_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    due_date = db.Column(db.DateTime(timezone=True))
    priority = db.Column(db.String(20), nullable=False, default=TaskPriority.MEDIUM, index=True)
    is_completed = db.Column(db.Boolean, nullable=False, index=True, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC)
    )

    # task table relations with messages table
    message = db.relationship(
        "Message",
        back_populates="tasks")

    # debugging purpose for this table
    def __repr__(self):
        return f"<Task id={self.id} task_name={self.task_name}>"


class AgentRun(db.Model):
    """
    Represents a record of an agent decision made for an analyzed message.

    Stores information about each run of the agent workflow,
    including which tools were selected, the reason for the decision,
    and the outcome of the run. Each agent run is linked to the message
    that triggered the agent’s next-best-action process.
    """

    __tablename__ = "agent_runs"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=False)

    agent_name = db.Column(db.String(50), nullable=False, default="action_agent")
    # e.g. ["send_confirmation", "generate_prep_guide"]
    selected_tools = db.Column(db.JSON)
    decision_reason = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, index=True, default=AgentRunStatus.PENDING)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # agent_runs table relations with messages table
    message = db.relationship(
        "Message",
        back_populates="agent_runs")

    # debugging purpose for this table
    def __repr__(self):
        return f"<AgentRun id={self.id} agent_name={self.agent_name}>"