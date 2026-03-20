import logging
from app.extensions import db
from app.models import Message

logger = logging.getLogger(__name__)

def process_message_submission(user_id, raw_text, subject=None, sender_email=None):
    """
    Process a newly submitted interview-related message.

    This function creates and saves the raw message only.
    Later, it can be expanded to run ML predictions, LLM extraction,
    task generation, and agent workflows.

    Returns:
        Message: The saved Message object.
    """

    try:
        message = Message(
            user_id=user_id,
            raw_text=raw_text,
            subject=subject,
            sender_email=sender_email
        )

        db.session.add(message)
        db.session.commit()

        return message

    except Exception:
        db.session.rollback()
        logger.exception("Failed to process message submission!")
        raise