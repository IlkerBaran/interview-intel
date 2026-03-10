from datetime import datetime, UTC
from .extensions import db


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

    subject = db.Column(db.String(255))
    sender_email = db.Column(db.String(255), index=True)
    raw_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, index=True, default="pending")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC)
    )

    # Message table relations with other tables
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

    # debugging purpose for this table
    def __repr__(self):
        return f"<Message id={self.id} status={self.status}>"


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
    message_category = db.Column(db.String(100))
    category_confidence = db.Column(db.Float)

    urgency_level = db.Column(db.String(50))
    urgency_confidence = db.Column(db.Float)

    job_field = db.Column(db.String(100))
    job_field_confidence = db.Column(db.Float)

    # Base extraction / structured interview details
    company_name = db.Column(db.String(255))
    role_title = db.Column(db.String(255))
    interview_stage = db.Column(db.String(100))
    interview_format = db.Column(db.String(100))
    date_text = db.Column(db.String(100))
    time_text = db.Column(db.String(100))
    location_text = db.Column(db.String(255))
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
    priority = db.Column(db.String(20), nullable=False, default="medium", index=True)
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
    selected_tools = db.Column(db.JSON)
    decision_reason = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, index=True, default="pending")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # agent_runs table relations with messages table
    message = db.relationship(
        "Message",
        back_populates="agent_runs")

    # debugging purpose for this table
    def __repr__(self):
        return f"<AgentRun id={self.id} agent_name={self.agent_name}>"