import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC
from typing import Optional
from dataclasses import dataclass, field


from app.extensions import db
from app.models import(
    Message,
    AnalysisResult,
    Task,
    AgentRun,
    MessageStatus,
    AgentRunStatus,
    TaskPriority,
    JobApplication,
    ApplicationStatus
)
from app.services.ml_service import ml_service
from app.services.llm_service import llm_service
from app.services.preprocess_language import normalize_text


logger = logging.getLogger(__name__)


# Structured result for auto-linking (success or fallback candidates)
@dataclass
class AutoLinkResult:
    linked: bool
    application_id: Optional[int] = None
    candidates: list = field(default_factory=list)


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
) -> tuple[Message, AutoLinkResult]:
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

            # ≈≈≈≈ auto-link to job application ≈≈≈≈
            link_result = _auto_link_applications(
                message=message,
                company_name=None,
                role_title=None,
                sender_email=sender_email,
                user_id=user_id,
            )

            return message, link_result

        if detected_lang != "en" and translation_success:
            # translation path is degraded — warn so it's queryable for triage
            logger.warning(
                "Message %s translated to English from %s",
                message.id,
                detected_lang,
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
            logger.warning(
                "ML service not loaded — skipping predictions for message %s",
                message.id,
                extra={"message_id": message.id, "service": "ml_service"}
            )

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
                    "Urgency overridden to high for message %s",
                    message.id,
                    extra={
                        "message_id": message.id,
                        "rule_applied": "high_urgency_override"
                    }
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
                        "Job field inherited from prior message for message %s",
                        message.id,
                        extra={
                            "message_id": message.id,
                            "job_field_source": "inherited"
                        }
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
            except Exception as e:
                logger.error(
                    "LLM extraction failed for message %s — %s",
                    message.id,
                    type(e).__name__,
                    extra={
                        "message_id": message.id,
                        "error_type": type(e).__name__
                    }
                )
        else:
            logger.warning(
                "LLM service not loaded — skipping extraction for message %s",
                message.id,
                extra={"message_id": message.id, "service": "llm_service"}
            )

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

        # ≈≈≈≈ auto-link to job application ≈≈≈≈
        link_result = _auto_link_applications(
            message=message,
            company_name=details.get("company_name"),
            role_title=details.get("role_title"),
            sender_email=sender_email,
            user_id=user_id,
        )

        logger.info(
            "Pipeline completed for message %s",
            message.id,
            extra={"message_id": message.id}
        )
        return message, link_result

    except Exception as e:
        # if it fails mark message as failed
        logger.error(
            "Pipeline failed for message %s — %s",
            message.id,
            type(e).__name__,
            extra={
                "message_id": message.id,
                "error_type": type(e).__name__
            }
        )

        try:
            message.status = MessageStatus.FAILED
            db.session.commit()
        except Exception as inner_e:
            db.session.rollback()
            logger.error(
                "Could not update message %s status to FAILED — %s",
                message.id,
                type(inner_e).__name__,
                extra={
                    "message_id": message.id,
                    "error_type": type(inner_e).__name__
                }
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
        logger.info(
            "Message %s saved",
            message.id,
            extra={"message_id": message.id}
        )
        return message

    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to save message — %s",
            type(e).__name__,
            extra={
                "user_id": user_id,
                "error_type": type(e).__name__
            }
        )
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
                len(TASK_RULES[final_category]),
                message.id,
                extra={
                    "message_id": message.id,
                    "task_count": len(TASK_RULES[final_category])
                }
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

    except Exception as e:
        db.session.rollback()
        logger.error(
            "Failed to save pipeline results for message %s — %s",
            message.id,
            type(e).__name__,
            extra={
                "message_id": message.id,
                "error_type": type(e).__name__
            }
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
            except Exception as e:
                logger.error(
                    "LLM parallel task failed: %s — %s",
                    key,
                    type(e).__name__,
                    extra={
                        "task_key": key,
                        "error_type": type(e).__name__
                    }
                )

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

    except Exception as e:
        logger.error(
            "Failed to query job field inheritance — %s",
            type(e).__name__,
            extra={"error_type": type(e).__name__}
        )
        return None


def _auto_link_applications(
        message: Message,
        company_name: Optional[str],
        role_title: Optional[str],
        sender_email: Optional[str],
        user_id: int
) -> AutoLinkResult:
    """
    Auto-link a processed message to a JobApplication using multi-signal scoring
    with sender history as a tie-breaker only. if it fails fallback to Manual-Review.

    Stage 1 — score every application on factual signals (max = 5):
        +2  company name exact match (case-insensitive)
        +1  company name partial match (one contains the other)
        +2  role title exact match (case-insensitive)
        +1  role title partial match
        +1  status is APPLIED or INTERVIEWING
        threshold = 3 (company alone can never reach this)

    Stage 2 — single winner above threshold → link it.

    Stage 3 — tie → check sender history to break it:
        which tied application has prior messages from this sender?
        exactly one → link it.
        still tied (recruiter handles multiple roles) → return candidates.

    Returns AutoLinkResult:
        linked=True   → message.job_application_id is set and committed.
        linked=False  → candidates list is populated for the UI to show.
    """
    if not company_name or not company_name.strip():
        logger.debug(
            "Auto-link skipped for message_id %s: no company name extracted",
            message.id,
            extra={"message_id": message.id}
        )
        return AutoLinkResult(linked=False)

    try:
        needle_company = company_name.strip().lower()
        needle_role = role_title.strip().lower() if role_title else None
        ACTIVE_STATUSES = {ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEWING}

        # Pre-filter applications by company name before Python scoring.
        # Checks both directions so "Apple" matches "Apple Inc."
        # and "Apple Inc." matches "Apple".
        # Verified to compile to LIKE with || concatenation on PostgreSQL and SQLite.
        company_lower = db.func.lower(JobApplication.company)
        applications = db.session.execute(
            db.select(JobApplication).where(
                JobApplication.user_id == user_id,
                db.or_(
                    company_lower.contains(needle_company),
                    db.literal(needle_company).contains(company_lower),
                ),
            )
        ).scalars().all()

        if not applications:
            return AutoLinkResult(linked=False)

        # ── stage 1: score on factual signals ──
        score_list = []
        for app in applications:
            score = 0
            haystack_company = app.company.lower().strip()

            if needle_company == haystack_company:
                score += 2
            elif needle_company in haystack_company or haystack_company in needle_company:
                score += 1

            # skip role + status scoring entirely if company didn't match at all
            # prevents a role-only match accidentally crossing the threshold
            if score == 0:
                continue

            if needle_role and app.role:
                haystack_role = app.role.lower().strip()
                if needle_role == haystack_role:
                    score += 2
                elif needle_role in haystack_role or haystack_role in needle_role:
                    score += 1

            if app.status in ACTIVE_STATUSES:
                score += 1

            score_list.append((score, app))

        if not score_list:
            logger.debug(
                "Auto-link: no company match for message %s",
                message.id,
                extra={"message_id": message.id}
            )
            return AutoLinkResult(linked=False)

        top_score = max(s for s, _ in score_list)

        if top_score < 3:
            logger.debug(
                "Auto-link: best score: %d below threshold for message %s: skipping",
                top_score, message.id,
                extra={
                    "top_score": top_score,
                    "message_id": message.id
                }
            )
            return AutoLinkResult(linked=False)

        top_matches = [app for score, app in score_list if score == top_score]

        # ── stage 2: single winner ──
        if len(top_matches) == 1:
            matched = top_matches[0]
            message.job_application_id = matched.id
            db.session.commit()
            logger.info(
                "Auto-linked message %s to application %s with score %d",
                message.id,
                matched.id,
                top_score,
                extra={
                    "message_id": message.id,
                    "application_id": matched.id,
                    "score": top_score,
                    "linked": True,
                }
            )
            return AutoLinkResult(linked=True, application_id=matched.id)

        # ── stage 3: tie — sender history as tie-breaker only ──
        logger.debug(
            "Auto-link: tie at score %d for message %s — checking sender history",
            top_score,
            message.id,
            extra={
                "top_score": top_score,
                "message_id": message.id
            }
        )

        if sender_email:
            # Single batched query — distinct application_ids that have prior
            # messages from this sender. Replaces N+1 loop and explicitly scopes
            # by user_id for defense-in-depth.
            top_match_ids = [app.id for app in top_matches]
            prior_app_ids = set(db.session.execute(
                db.select(Message.job_application_id)
                .where(
                    Message.user_id == user_id,
                    Message.job_application_id.in_(top_match_ids),
                    Message.sender_email == sender_email,
                    Message.id != message.id,
                )
                .distinct()
            ).scalars().all())

            history_matches = [app for app in top_matches if app.id in prior_app_ids]

            if len(history_matches) == 1:
                matched = history_matches[0]
                message.job_application_id = matched.id
                db.session.commit()
                logger.info(
                    "Auto-linked message %s → application %s via sender history tie-break",
                    message.id,
                    matched.id,
                    extra={
                        "message_id": message.id,
                        "application_id": matched.id,
                        "linked": True,
                        "tie_break": "sender_history"
                    }
                )
                return AutoLinkResult(linked=True, application_id=matched.id)

            if len(history_matches) > 1:
                logger.debug(
                    "Auto-link: sender history matched %d applications for message %s still ambiguous",
                    len(history_matches),
                    message.id,
                    extra={
                        "history_matches_count": len(history_matches),
                        "message_id": message.id
                    }
                )

        # tie unresolved — return top candidates for the user to pick
        candidates = sorted(score_list, key=lambda x: x[0], reverse=True)[:3]
        logger.info(
            "Auto-link: returning %d candidates for manual selection (message %s)",
            len(candidates),
            message.id,
            extra={
                "message_id": message.id,
                "candidate_count": len(candidates),
                "linked": False
            }
        )
        return AutoLinkResult(linked=False, candidates=candidates)

    except Exception as e:
        db.session.rollback()
        logger.error(
            "Auto-link failed for message %s rolling back error_type: %s",
            message.id,
            type(e).__name__,
            extra= {
                "message_id": message.id,
                "error_type": type(e).__name__
            }
        )
        return AutoLinkResult(linked=False)
