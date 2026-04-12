import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC
from typing import Optional

from app.extensions import db
from app.models import Message, AnalysisResult, Task, AgentRun, MessageStatus, AgentRunStatus, TaskPriority
from app.services.ml_service import ml_service
from app.services.llm_service import llm_service
from app.services.preprocess_language import normalize_text

logger = logging.getLogger(__name__)

# ≈≈≈≈ backend urgency rules ≈≈≈≈
# ML predicts urgency also backend ensures overrides for known high-urgency categories
HIGH_URGENCY_CATEGORIES = {"interview_invitation", "offer"}

# ≈≈≈≈ Context Sharing ≈≈≈≈
# similar categories for inheritance check
# from previous messages and from this SAME sender then reuse the job_field for consistency
SIMILAR_CATEGORY_GROUPS = {
    "interview_invitation": {"interview_invitation", "scheduling", "offer", "follow_up"},
    "scheduling":           {"interview_invitation", "scheduling", "offer", "follow_up"},
    "offer":                {"interview_invitation", "scheduling", "offer", "follow_up"},
    "follow_up":            {"interview_invitation", "scheduling", "offer", "follow_up"},
    "recruiter_outreach":   {"recruiter_outreach"},
    "rejection":            {"rejection"}
}

# ≈≈≈≈ task generation rules ≈≈≈≈
# Backend decides what tasks to create based on category
# LLM never writes directly to the database
TASK_RULES = {
    "interview_invitation": [
        {
            "task_name": "Confirm interview schedule",
            "description": "Reply to confirm your availability for the interview.",
            "priority": TaskPriority.HIGH
        },

        {
            "task_name": "Research the company",
            "description": "Look up the company mission, products, team, and recent news.",
            "priority": TaskPriority.HIGH
        },

        {
            "task_name": "Prepare role-specific topics",
            "description": "Review key skills and topics relevant to the role.",
            "priority": TaskPriority.MEDIUM
        }
    ],

    "offer": [
        {
            "task_name": "Review the offer details",
            "description": "Read the offer letter carefully including salary, benefits, and equity.",
            "priority": TaskPriority.HIGH
        },

        {
            "task_name": "Respond by the deadline",
            "description": "Send your decision before the offer expiration date.",
            "priority": TaskPriority.HIGH
        },

        {
            "task_name": "Research compensation benchmarks",
            "description": "Compare the offer against market rates for the role and location.",
            "priority": TaskPriority.MEDIUM
        }
    ],

    "scheduling": [
        {
            "task_name": "Confirm the new interview time",
            "description": "Reply to confirm or propose an alternative time.",
            "priority": TaskPriority.HIGH
        },

        {
            "task_name": "Update your calendar",
            "description": "Block the new interview time and set a reminder.",
            "priority": TaskPriority.MEDIUM
        }
    ]
}

def process_message_submission(
        user_id: int,
        raw_text: str,
        subject: Optional[str] = None,
        sender_email: Optional[str] = None,
) -> Message:
    """
    Full pipeline: normalize language → ML → LLM → save → tasks.

    Saves Message and AnalysisResult to DB.
    Generates tasks based on backend rules.
    Returns the saved Message object.

    Raises on hard failure — route handles flash/redirect.
    """

    # ≈≈≈≈ edge case: empty input ≈≈≈≈
    if not raw_text or not raw_text.strip():
        raise ValueError("raw_text cannot be empty")

    # ≈≈≈≈ save message first ≈≈≈≈
    # Save the raw message before ML/LLM
    # so user data is never lost on downstream failure
    message = _save_message(user_id, raw_text, subject, sender_email)

    # ≈≈≈≈ try block: everything's after message save to protect the pipeline ≈≈≈≈
    try:
        # language normalization
        normalized_text, detected_lang, translation_success = normalize_text(raw_text)

        if detected_lang != "en" and not translation_success:
            # structured logger, queryable in Datadog/ELK
            logger.warning(
                "Translation failed. Skipping ML/LLM pipeline.",
                extra={
                    "message_id": message.id,
                    "detected_lang": detected_lang,
                }
            )

            # save minimal result with language info - no ML/LLM data
            _save_pipeline_results(
                message=message,
                ml_result={},
                details={},
                detected_lang=detected_lang,
                llm_outputs={},
                tools_used=[],
            )
            return message

        if detected_lang != "en" and translation_success:
            # structured logger, queryable in Datadog/ELK
            logger.info(
                "Message translated to English.",
                extra={
                    "message_id": message.id,
                    "detected_lang": detected_lang,
                }
            )

        # ≈≈≈≈ Ml predictions ≈≈≈≈
        ml_result = {}
        ml_ran = False

        if ml_service.is_loaded:
            ml_result = ml_service.predict(normalized_text) or {}
            ml_ran = True
        else:
            logger.warning("ML service not loaded — skipping predictions")

        # ≈≈≈≈ Backend urgency rule ≈≈≈≈
        category = ml_result.get("category")

        if not ml_ran:
            # ML did not run — sources are unavailable not predicted
            ml_result["urgency_source"] = "unavailable"
            ml_result["job_field_source"] = "unavailable"
        else:
            if category in HIGH_URGENCY_CATEGORIES:
                ml_result["urgency"] = "high"
                ml_result["urgency_source"] = "rule_based"
                logger.debug(
                    "Urgency overridden to high for category: %s", category
                )
            else:
                ml_result["urgency_source"] = "ml_predicted"

            # ≈≈≈≈ context enrichment ≈≈≈≈
            # If job_field is general and sender is known,
            # inherit job_field from similar previous messages
            if ml_result.get("job_field") == "general" and sender_email:
                inherited = _inherit_job_field(user_id, sender_email, category)
                if inherited:
                    ml_result["job_field"] = inherited
                    ml_result["job_field_source"] = "inherited"
                    logger.info(
                        "Job field inherited from previous message: %s",
                        inherited
                    )
                else:
                    ml_result["job_field_source"] = "ml_predicted"
            else:
                ml_result["job_field_source"] = "ml_predicted"

        # ≈≈≈≈ LLM extraction(sequential) ≈≈≈≈
        # Runs first alone — other LLM calls depend on its output
        details = {}
        llm_ran = False

        if llm_service.is_loaded:
            try:
                details = llm_service.extract_interview_details(normalized_text)
                llm_ran = True
            except Exception:
                logger.exception(
                    "LLM extraction failed for message %s", message.id
                )
        else:
            logger.warning("LLM service not loaded — skipping extraction")

        # use final_category after all enrichment — never use stale category
        final_category = ml_result.get("category")

        # ≈≈≈≈ LLM enrichment (parallel) ≈≈≈≈
        # 5 independent calls run concurrently
        # Anthropic SDK client is thread-safe for concurrent requests
        llm_outputs = {}
        if llm_ran:
            llm_outputs = _run_llm_parallel(
                normalized_text=normalized_text,
                category=final_category,
                role_title=details.get("role_title"),
                company_name=details.get("company_name"),
                interview_stage=details.get("interview_stage"),
                interview_format=details.get("interview_format"),
                job_field=ml_result.get("job_field")
            )

        # track what actually ran for honest agent logging
        tools_used = []
        if ml_ran:
            tools_used.append("ml_service")
        if llm_ran:
            tools_used.append("llm_service")
        if final_category in TASK_RULES:
            tools_used.append("task_generator")

        # ≈≈≈≈ single transaction ≈≈≈≈
        # analysis + tasks + agent log + status update
        _save_pipeline_results(
            message=message,
            ml_result=ml_result,
            details=details,
            detected_lang=detected_lang,
            llm_outputs=llm_outputs,
            tools_used=tools_used,
        )

        logger.info(
            "Message processed successfully.",
            extra={"message_id": message.id}
        )
        return message

    except Exception:
        # if it fails mark message as failed
        logger.exception(
            "Pipeline failed.",
            extra={"message_id": message.id}
        )

        try:
            message.status = MessageStatus.FAILED
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Could not update message %s status to FAILED", message.id
            )
        raise


# ≈≈≈≈ Private Helpers ≈≈≈≈

def _save_message(
    user_id: int,
    raw_text: str,
    subject: Optional[str],
    sender_email: Optional[str],
) -> Message:
    """Save raw message to DB. Raises on failure."""
    try:
        message = Message(
            user_id=user_id,
            raw_text=raw_text,
            subject=subject,
            sender_email=sender_email,
            status=MessageStatus.PROCESSING,
        )
        db.session.add(message)
        db.session.commit()
        logger.info("Message %s saved", message.id)
        return message

    except Exception:
        db.session.rollback()
        logger.exception("Failed to save message")
        raise


def _save_pipeline_results(
        message: Message,
        ml_result: dict,
        details: dict,
        detected_lang: str,
        llm_outputs: dict,
        tools_used: list,
) -> None:
    """
    Single transaction: save AnalysisResult + Tasks + AgentRun + status.

    detected_lang is included in AgentRun.decision_reason for auditing.
    tools_used reflects what actually ran — not what was available.

    Raises on failure — top-level handler sets message to FAILED.
    """
    try:
        final_category = ml_result.get("category")

        # ── analysis result ──
        analysis = AnalysisResult(
            message_id=message.id,

            # ML predictions
            message_category=final_category,
            category_confidence=ml_result.get("category_conf"),
            urgency_level=ml_result.get("urgency"),
            urgency_confidence=ml_result.get("urgency_conf"),
            job_field=ml_result.get("job_field"),
            job_field_confidence=ml_result.get("job_field_conf"),

            # extracted details
            company_name=details.get("company_name"),
            role_title=details.get("role_title"),
            interview_stage=details.get("interview_stage"),
            interview_format=details.get("interview_format"),
            date_text=details.get("date_text"),
            time_text=details.get("time_text"),
            detected_language=detected_lang,
            location_text=details.get("location_text"),

            # LLM outputs
            preparation_guidance=llm_outputs.get("preparation_guidance"),
            suggested_questions=llm_outputs.get("suggested_questions"),
            reply_suggestions=llm_outputs.get("reply_suggestions"),
            role_summary=llm_outputs.get("role_summary"),
            archive_summary=llm_outputs.get("archive_summary"),

            processed_at=datetime.now(UTC),
        )
        db.session.add(analysis)

        # ≈≈≈≈ tasks ≈≈≈≈
        if final_category in TASK_RULES:
            for rule in TASK_RULES[final_category]:
                task = Task(
                    message_id=message.id,
                    task_name=rule["task_name"],
                    description=rule["description"],
                    priority=rule["priority"],
                    is_completed=False,
                )
                db.session.add(task)
            logger.info(
                "Generated %d tasks for message %s",
                len(TASK_RULES[final_category]), message.id
            )

        # ≈≈≈≈ agent run ≈≈≈≈
        # tools_used reflects what actually ran — honest audit log
        agent_run = AgentRun(
            message_id=message.id,
            agent_name="workflow_agent",
            selected_tools=tools_used,
            decision_reason=(
                f"Category: {final_category} | "
                f"Urgency: {ml_result.get('urgency')} "
                f"({ml_result.get('urgency_source')}) | "
                f"Job field: {ml_result.get('job_field')} "
                f"({ml_result.get('job_field_source')}) | "
                f"Detected language: {detected_lang}"
            ),
            status=AgentRunStatus.COMPLETED
        )
        db.session.add(agent_run)

        # ≈≈≈≈ message status ≈≈≈≈
        message.status = MessageStatus.COMPLETED

        # ≈≈≈≈ single commit for everything ≈≈≈≈
        db.session.commit()
        logger.info("Pipeline results saved for message %s", message.id)

    except Exception:
        db.session.rollback()
        logger.exception(
            "Failed to save pipeline results for message %s", message.id
        )
        raise


def _run_llm_parallel(
        normalized_text: str,
        category: Optional[str],
        role_title: Optional[str],
        company_name: Optional[str],
        interview_stage: Optional[str],
        interview_format: Optional[str],
        job_field: Optional[str],
) -> dict:
    """
    Run all independent LLM enrichment calls in parallel.
    Returns dict of outputs — None values on individual failure.

    Thread safety: Anthropic Python SDK creates a new HTTP request
    per API call and does not share mutable state between calls.
    Safe to call from multiple threads simultaneously.
    """
    if not llm_service.is_loaded:
        return {}

    outputs = {
        "preparation_guidance": None,
        "suggested_questions": None,
        "reply_suggestions": None,
        "role_summary": None,
        "archive_summary": None,
    }

    tasks = {
        "preparation_guidance": lambda: llm_service.generate_preparation_guidance(
            role_title=role_title,
            company_name=company_name,
            interview_format=interview_format,
            job_field=job_field,
        ),
        "suggested_questions": lambda: llm_service.suggest_candidate_questions(
            role_title=role_title,
            company_name=company_name,
            interview_stage=interview_stage,
            job_field=job_field,
        ),
        "reply_suggestions": lambda: llm_service.generate_reply_suggestions(
            raw_text=normalized_text,
            category=category,
            role_title=role_title,
            company_name=company_name,
        ),
        "role_summary": lambda: llm_service.generate_role_summary(
            role_title=role_title,
            company_name=company_name,
            job_field=job_field,
            raw_text=normalized_text,
        ),
        "archive_summary": lambda: llm_service.generate_archive_summary(
            role_title=role_title,
            company_name=company_name,
            category=category,
            raw_text=normalized_text,
        ),
    }

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fn): key
            for key, fn in tasks.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                outputs[key] = future.result()
            except Exception:
                logger.exception("LLM parallel task failed: %s", key)

    return outputs


def _inherit_job_field(
        user_id: int,
        sender_email: str,
        current_category: Optional[str],
) -> Optional[str]:
    """
    Context enrichment — if job_field is general, check if we've
    seen this sender before with a known field in a similar category.

    Only inherits from similar category groups to avoid wrong context.
    Example: interview_invitation can inherit from scheduling (same process).
    But recruiter_outreach stays separate from rejections.
    """
    try:
        similar_categories = SIMILAR_CATEGORY_GROUPS.get(
            current_category, set()
        )

        if not similar_categories:
            return None

        previous = db.session.execute(
            db.select(AnalysisResult)
            .join(Message)
            .where(
                Message.user_id == user_id,
                Message.sender_email == sender_email,
                AnalysisResult.job_field != "general",
                AnalysisResult.job_field.isnot(None),
                AnalysisResult.message_category.in_(similar_categories),
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        return previous.job_field if previous else None

    except Exception:
        logger.exception("Failed to query job field inheritance")
        return None
