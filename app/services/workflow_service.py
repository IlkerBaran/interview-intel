import logging
from datetime import datetime, UTC
from typing import Optional
from dataclasses import dataclass, field

from app.extensions import db
from app.models import (
    Message,
    AnalysisResult,
    Task,
    AgentRun,
    MessageStatus,
    AgentRunStatus,
    TaskPriority,
    JobApplication,
    ApplicationStatus,
    ACTIVE_APPLICATION_STATUSES,
    # Reserved for route guards and UI status checks
    LOCKED_APPLICATION_STATUSES,
    NotificationType,
    Notification
)
import anthropic
from app.services.ml_service import ml_service, JOB_FIELD_UNCLASSIFIED
from app.services.llm_service import llm_service, TRANSIENT_LLM_ERRORS
from app.services.preprocess_language import normalize_text

# Safe at module level: quota_service imports only extensions + models, so there is
# no workflow_service <-> quota_service cycle (unlike celery_tasks, which must stay
# function-local).
from app.services.quota_service import refund_nothing_to_show


logger = logging.getLogger(__name__)


# Structured result for auto-linking (success or fallback candidates)
@dataclass
class LinkCandidate:
    application: JobApplication
    score: int


@dataclass
class AutoLinkResult:
    linked: bool
    application_id: Optional[int] = None
    candidates: list[LinkCandidate] = field(default_factory=list)
    application: Optional[JobApplication] = None
    status_updated: bool = False
    status_reason: str = ""


# ≈≈≈≈ backend urgency rules ≈≈≈≈
# A FLOOR, not an override. Only lifts when the model says LOW; a model reading of
# medium or high stands untouched.
#
# The old rule forced "high" on every invitation and offer regardless of the text,
# which made the badge mean "interview" rather than "act soon" — and now that
# urgency is derived from a stated deadline in the email, a blanket override would
# discard the model's answer for exactly the two categories where it varies most.
URGENCY_FLOOR_CATEGORIES = {"interview_invitation", "offer"}
URGENCY_FLOOR_LEVEL = "medium"

# ≈≈≈≈ Context Sharing ≈≈≈≈
# similar categories for inheritance check
# from previous messages and from this SAME sender then reuse the job_field for consistency
SIMILAR_CATEGORY_GROUPS = {
    "interview_invitation": {"interview_invitation", "scheduling", "offer", "follow_up"},
    "scheduling":           {"interview_invitation", "scheduling", "offer", "follow_up"},
    "offer":                {"interview_invitation", "scheduling", "offer", "follow_up"},
    "follow_up":            {"interview_invitation", "scheduling", "offer", "follow_up",
                             "application_received"},
    # An acknowledgement and a later check-in from the same sender are one process,
    # so the field learned from either should carry to the other.
    "application_received": {"application_received", "follow_up"},
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


# ≈≈≈≈ guidance stage rules ≈≈≈≈
# What to produce for each category, and which moment in the process to write for.
# Keys are the six classes the model can emit — there is no "application_received",
# so a confirmation lands on follow_up at best.
@dataclass(frozen=True)
class GuidancePolicy:
    prep: bool
    questions: bool
    reply: bool
    stage: str


GUIDANCE_POLICY = {
    "interview_invitation": GuidancePolicy(
        prep=True, questions=True, reply=True,
        stage="An interview has been offered or scheduled. Write for someone preparing "
              "to attend it.",
    ),
    "scheduling": GuidancePolicy(
        prep=True, questions=True, reply=True,
        stage="An interview is being scheduled. Write for someone preparing to attend it.",
    ),
    "recruiter_outreach": GuidancePolicy(
        prep=True, questions=False, reply=True,
        stage="A recruiter has made first contact. Nothing is scheduled and there is no "
              "interviewer yet. Write about what to do NOW — research the company, get "
              "their own projects and numbers straight, be ready if a call comes. Do NOT "
              "write interview-day logistics or a study plan aimed at a date.",
    ),
    "follow_up": GuidancePolicy(
        prep=True, questions=False, reply=True,
        stage="The application is in progress and nothing is scheduled. Write about what "
              "to do while waiting. Do NOT write interview-day logistics.",
    ),
    "application_received": GuidancePolicy(
        prep=True, questions=False, reply=False,
        stage="An application has just been submitted and acknowledged. Nothing is "
              "scheduled and there is no interviewer yet. Write about what to do now "
              "that they have applied — research the company, get their own projects "
              "and numbers straight, be ready if a call comes. Do NOT write "
              "interview-day logistics or a study plan aimed at a date.",
    ),
    "offer": GuidancePolicy(
        prep=True, questions=False, reply=True,
        stage="An offer has been made and no interview is pending. Write for someone "
              "evaluating and negotiating it. Do NOT suggest technical study.",
    ),
    "rejection": GuidancePolicy(prep=False, questions=False, reply=False, stage=""),
}

# ML unavailable or an unknown label — stage-neutral fallback rather than a guess.
UNKNOWN_STAGE_POLICY = GuidancePolicy(
    prep=True, questions=False, reply=True,
    stage="The stage of the process is unknown. Write stage-neutral preparation advice "
          "and do not assume an interview has been scheduled.",
)


# ≈≈≈≈ status auto-update rules ≈≈≈≈
# maps email category → the application status it should trigger
STATUS_MAP = {
    "interview_invitation": ApplicationStatus.INTERVIEWING,
    "offer":                ApplicationStatus.OFFERED,
    "rejection":            ApplicationStatus.REJECTED,
}

# forward-only rank for normal progression
# Active applications can progress into OFFERED
# SAVED and WITHDRAWN are intentionally absent and never auto-updated
# REJECTED is intentionally absent and handled as a special case above rank logic
STATUS_RANK = {
    ApplicationStatus.APPLIED:      1,
    ApplicationStatus.INTERVIEWING: 2,
    ApplicationStatus.OFFERED:      3,
}

APPLICATION_FETCH_LIMIT = 50

# ≈≈≈≈ notification rules ≈≈≈≈
# maps EMAIL CATEGORY → (notification_type, headline)
#
# Keyed on the detected category, not on an application status change. The
# notifications empty state promises a notification whenever an interview
# invitation or an offer is detected, and analysis never creates applications —
# so gating on a successful link made that promise false for every company the
# user had not already entered by hand, and gating on status_updated silenced
# every interview email after the first for an application already INTERVIEWING.
#
# rejection is deliberately absent: the empty state promises interviews and
# offers only. STATUS_MAP still maps it for the application-status update.
# TASK_DUE and APPLICATION_UPDATED are separate future features.
NOTIFICATION_MAP = {
    "interview_invitation": (NotificationType.INTERVIEW_DETECTED, "Interview invitation detected"),
    "offer":                (NotificationType.OFFER_DETECTED,     "Offer detected")
}


def queue_message_analysis(message_id: int) -> bool:
    """
    Enqueue the analysis task by id (pass IDs, not objects). Returns True if
    enqueued, False on failure (e.g. broker unreachable) so the route can mark the
    message FAILED instead of leaving it PENDING forever. Function-local import
    avoids a workflow_service <-> celery_tasks import cycle.
    """
    try:
        from app.celery_tasks import analyze_message
        analyze_message.delay(message_id)
        return True
    except Exception as e:
        logger.error(
            "Failed to enqueue analysis message_id=%s error_type=%s",
            message_id, type(e).__name__,
            extra={"message_id": message_id, "error_type": type(e).__name__},
        )
        return False


def analysis_already_done(message: Message) -> bool:
    """
    Skip-if-done guard (Stage 3 idempotency): True if the work already completed, so
    a redundant acks_late redelivery does not re-run and re-charge the LLM.
    """
    if message.status == MessageStatus.COMPLETED:
        return True
    exists = db.session.execute(
        db.select(AnalysisResult.id).where(AnalysisResult.message_id == message.id)
    ).scalar_one_or_none()
    return exists is not None


def mark_analysis_failed(message_id: int) -> bool:
    """
    Force a message to the terminal FAILED state and commit. Used whenever a message
    must not be left in a non-terminal state: the route (enqueue failed) and the
    worker (permanent error, transient-retry exhaustion, soft time limit).

    Robustness:
      1. Rollback FIRST — a soft-limit or error may fire mid-transaction; clearing
         the half-open txn stops it poisoning this write.
      2. Re-fetch by id — the previously loaded object may be expired after rollback.
      3. Does NOT overwrite a message that already reached COMPLETED.
      4. Never raises; completes the commit before control returns, so it finishes
         before the FlaskTask app-context teardown runs session.remove().
    """
    try:
        db.session.rollback()
        message = db.session.get(Message, message_id)
        if message is None:
            logger.warning(
                "mark_analysis_failed: message_id=%s not found", message_id,
                extra={"message_id": message_id},
            )
            return False
        if message.status == MessageStatus.COMPLETED:
            return True   # already terminal-success — do not clobber
        message.status = MessageStatus.FAILED
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        logger.error(
            "mark_analysis_failed: could not mark message %s FAILED — %s",
            message_id, type(e).__name__,
            extra={"message_id": message_id, "error_type": type(e).__name__},
        )
        return False


def run_message_analysis(message: Message) -> None:
    """
    Worker side (Stage 3): the full pipeline, run SEQUENTIALLY in the task's single
    app context / single session (no ThreadPoolExecutor).

    Contract with the task:
      * A TRANSIENT failure of the CRITICAL extraction call bubbles out unchanged so
        the task's retry retries the whole pipeline.
      * A PERMANENT extraction failure degrades (details stays {}) — no retry.
      * The 5 enrichments are best-effort: each degrades to None, never retries.
      * All result rows commit in ONE transaction (atomic writes).
    """
    user_id = message.user_id
    sender_email = message.sender_email
    raw_text = message.raw_text

    # ── language normalization ──
    normalized_text, detected_lang, translation_success = normalize_text(raw_text)

    if detected_lang != "en" and not translation_success:
        # COMPLETED-degraded: raw email + detected language are still shown.
        logger.warning(
            "Translation failed. Skipping ML/LLM pipeline.",
            extra={"message_id": message.id, "detected_lang": detected_lang},
        )
        link_result = _auto_link_applications(
            message=message, company_name=None, role_title=None,
            sender_email=sender_email, user_id=user_id, category=None,
        )
        _save_pipeline_results(
            message=message, ml_result={}, details={}, detected_lang=detected_lang,
            llm_outputs={}, tools_used=[], link_result=link_result, user_id=user_id,
        )
        return

    if detected_lang != "en" and translation_success:
        logger.warning(
            "Message %s translated to English from %s", message.id, detected_lang,
            extra={"message_id": message.id, "detected_lang": detected_lang},
        )

    # ── ML predictions ──
    ml_result = {}
    ml_ran = False
    if ml_service.is_loaded:
        ml_result = ml_service.predict(normalized_text) or {}
        ml_ran = True
    else:
        logger.warning(
            "ML service not loaded — skipping predictions for message %s", message.id,
            extra={"message_id": message.id, "service": "ml_service"},
        )

    category = ml_result.get("category")
    if not ml_ran:
        ml_result["urgency_source"] = "unavailable"
        ml_result["job_field_source"] = "unavailable"
    else:
        # "rule_based" is retired for urgency: under a floor the model is always
        # consulted, so naming a rule when the floor did not fire would put a false
        # claim in the audit log. Only name it when it actually lifted something.
        if (category in URGENCY_FLOOR_CATEGORIES
                and ml_result.get("urgency") == "low"):
            ml_result["urgency"] = URGENCY_FLOOR_LEVEL
            ml_result["urgency_source"] = "floor_applied"
        else:
            ml_result["urgency_source"] = "ml_predicted"

        if ml_result.get("job_field") == JOB_FIELD_UNCLASSIFIED and sender_email:
            inherited = _inherit_job_field(user_id, sender_email, category)
            if inherited:
                ml_result["job_field"] = inherited
                ml_result["job_field_conf"] = None   # score belonged to the label
                ml_result["job_field_source"] = "inherited"
            else:
                ml_result["job_field_source"] = "ml_predicted"
        else:
            ml_result["job_field_source"] = "ml_predicted"

    # ── CRITICAL extraction: transient bubbles → task retry; permanent degrades ──
    details = {}
    llm_ran = False
    if llm_service.is_loaded:
        try:
            details = llm_service.extract_interview_details(
                normalized_text,
                idempotency_key=f"msg-{message.id}-extract",
            )
            llm_ran = True
        except TRANSIENT_LLM_ERRORS:
            # Ordered BEFORE the generic handler: let the task retry the pipeline.
            raise
        except anthropic.APIError as e:
            # Permanent (400/401/403/404/422 …) — degrade, do NOT retry.
            logger.error(
                "LLM extraction permanently failed for message %s — %s",
                message.id, type(e).__name__,
                extra={"message_id": message.id, "error_type": type(e).__name__},
            )
            details, llm_ran = {}, False
    else:
        logger.warning(
            "LLM service not loaded — skipping extraction for message %s", message.id,
            extra={"message_id": message.id, "service": "llm_service"},
        )

    # ── partial-failure gate: nothing to show → FAILED (atomic) ──
    if not ml_ran and not llm_ran:
        _mark_failed_nothing_to_show(message, detected_lang)
        return

    final_category = ml_result.get("category")

    link_result = _auto_link_applications(
        message=message, company_name=details.get("company_name"),
        role_title=details.get("role_title"), sender_email=sender_email,
        user_id=user_id, category=final_category,
    )

    matched_application = link_result.application
    llm_company_name = (matched_application and matched_application.company) or details.get("company_name")
    llm_role_title = (matched_application and matched_application.role) or details.get("role_title")
    llm_job_field = (matched_application and matched_application.job_field) or ml_result.get("job_field")
    llm_interview_stage = details.get("interview_stage")
    llm_interview_format = details.get("interview_format")

    # ── LLM enrichment: SEQUENTIAL, best-effort, STAGE-GATED ──
    llm_outputs = {}
    if llm_ran:
        llm_outputs = _run_llm_enrichments(
            normalized_text=normalized_text, category=final_category,
            role_title=llm_role_title, company_name=llm_company_name,
            interview_stage=llm_interview_stage, interview_format=llm_interview_format,
            job_field=llm_job_field,
            date_text=details.get("date_text"),
            time_text=details.get("time_text"),
        )

    tools_used = []
    if ml_ran:
        tools_used.append("ml_service")
    if llm_ran:
        tools_used.append("llm_service")
    if final_category in TASK_RULES:
        tools_used.append("task_generator")

    # ── single transaction: AnalysisResult + Tasks + AgentRun + Notification + status ──
    _save_pipeline_results(
        message=message, ml_result=ml_result, details=details,
        detected_lang=detected_lang, llm_outputs=llm_outputs,
        tools_used=tools_used, link_result=link_result, user_id=user_id,
    )
    logger.info(
        "Pipeline completed for message %s", message.id,
        extra={"message_id": message.id},
    )


# ≈≈≈≈ Private Helpers ≈≈≈≈

def _save_pipeline_results(
        message: Message,
        ml_result: dict,
        details: dict,
        detected_lang: str,
        llm_outputs: dict,
        tools_used: list,
        link_result: AutoLinkResult,
        user_id: int,
) -> None:
    """
    Single transaction: save AnalysisResult + Tasks + AgentRun + Notification + status.

    detected_lang is included in AgentRun.decision_reason for auditing.
    tools_used reflects what actually ran — not what was available.
    The notification is staged into THIS transaction (atomic writes) — a crash
    leaves nothing partially written.

    Raises on failure — the task marks the message FAILED.
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

        # ≈≈≈≈ notification (SAME transaction — atomic writes) ≈≈≈≈
        _stage_notification(
            message_id=message.id,
            category=final_category,
            link_result=link_result,
            details=details,
            user_id=user_id
        )

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


def _run_llm_enrichments(
        normalized_text: str,
        category: Optional[str],
        role_title: Optional[str],
        company_name: Optional[str],
        interview_stage: Optional[str],
        interview_format: Optional[str],
        job_field: Optional[str],
        date_text: Optional[str] = None,
        time_text: Optional[str] = None,
) -> dict:
    """
    Run the enrichment calls SEQUENTIALLY in the task's single app context (no threads).

    GUIDANCE_POLICY decides which run. A skipped call is never built, so it costs no
    tokens; its column stays NULL and show_message.html omits the whole section.

    date_text/time_text go to the prep call raw — nothing is parsed here.

    Best-effort: a genuine failure of any single enrichment degrades that field to None
    and is logged; it never propagates (only the critical extraction retries).
    """
    if not llm_service.is_loaded:
        return {}

    # All five keys stay seeded — a skipped enrichment and a failed one both land as NULL.
    outputs = {
        "preparation_guidance": None,
        "suggested_questions": None,
        "reply_suggestions": None,
        "role_summary": None,
        "archive_summary": None,
    }

    policy = GUIDANCE_POLICY.get(category, UNKNOWN_STAGE_POLICY)

    calls = {}

    if policy.prep:
        calls["preparation_guidance"] = lambda: llm_service.generate_preparation_guidance(
            role_title=role_title,
            company_name=company_name,
            interview_format=interview_format,
            job_field=job_field,
            stage_note=policy.stage,
            date_text=date_text,
            time_text=time_text,
        )

    if policy.questions:
        calls["suggested_questions"] = lambda: llm_service.suggest_candidate_questions(
            role_title=role_title,
            company_name=company_name,
            interview_stage=interview_stage,
            job_field=job_field,
        )

    if policy.reply:
        calls["reply_suggestions"] = lambda: llm_service.generate_reply_suggestions(
            raw_text=normalized_text,
            category=category,
            role_title=role_title,
            company_name=company_name,
        )

    # Always run: meaningful at every stage.
    calls["role_summary"] = lambda: llm_service.generate_role_summary(
        role_title=role_title,
        company_name=company_name,
        job_field=job_field,
        raw_text=normalized_text,
    )
    calls["archive_summary"] = lambda: llm_service.generate_archive_summary(
        role_title=role_title,
        company_name=company_name,
        category=category,
        raw_text=normalized_text,
    )

    skipped = sorted(k for k in outputs if k not in calls)
    if skipped:
        logger.info(
            "Stage gating: skipped %s for category=%s",
            ", ".join(skipped), category,
            extra={"category": category, "skipped_enrichments": skipped},
        )

    for key, fn in calls.items():
        try:
            outputs[key] = fn()
        except Exception as e:
            logger.error(
                "LLM enrichment failed: %s — %s",
                key,
                type(e).__name__,
                extra={
                    "task_key": key,
                    "error_type": type(e).__name__
                }
            )
            outputs[key] = None

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
                AnalysisResult.job_field != JOB_FIELD_UNCLASSIFIED,
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
        user_id: int,
        category: Optional[str]
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
        # Verified to compile to LIKE with || concatenation on PostgresSQL and SQLite.
        company_lower = db.func.lower(JobApplication.company)
        applications = db.session.execute(
            db.select(JobApplication).where(
                JobApplication.user_id == user_id,
                db.or_(
                    company_lower.contains(needle_company),
                    db.literal(needle_company).contains(company_lower),
                ),
            ).limit(APPLICATION_FETCH_LIMIT)
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
            status_updated, status_reason = _auto_update_status(matched, category)
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
            return AutoLinkResult(
                linked=True,
                application_id=matched.id,
                application=matched,
                status_updated=status_updated,
                status_reason=status_reason
            )

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
                status_updated, status_reason = _auto_update_status(matched, category)
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
                return AutoLinkResult(
                    linked=True,
                    application_id=matched.id,
                    application=matched,
                    status_updated=status_updated,
                    status_reason=status_reason
                )

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
        candidates = [
            LinkCandidate(application=app, score=score)
            for score, app in sorted(score_list, key=lambda x: x[0], reverse=True)[:3]
        ]
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


def _auto_update_status(
        application: JobApplication,
        category: Optional[str]
) -> tuple[bool, str]:
    """
    Auto-update a JobApplication status based on the linked email category.

    Gate: skip entirely if application is not active:
        SAVED       = bookmarked only, user hasn't engaged yet
        OFFERED     = terminal and locked
        WITHDRAWN   = user-only action, never touched by the pipeline

    REJECTED special case:
        Always applies on active applications regardless of current rank.
        A company can reject at any stage (APPLIED or INTERVIEWING).

    Forward-only rank check for everything else:
        APPLIED → INTERVIEWING allowed (rank 1 → rank 2)
        APPLIED → OFFERED allowed (rank 1 → rank 3)
        INTERVIEWING → OFFERED allowed (rank 2 → rank 3)
        INTERVIEWING → INTERVIEWING blocked (same rank)

    Returns:
        (True,  "updated")      status was changed
        (False, "no_mapping")   category not in STATUS_MAP
        (False, "not_active")   application is SAVED, OFFERED, or WITHDRAWN
        (False, "same_rank")    new rank does not exceed current rank
    """
    new_status = STATUS_MAP.get(category)

    if not new_status:
        return False, "no_mapping"

    if application.status not in ACTIVE_APPLICATION_STATUSES:
        return False, "not_active"

    if new_status == ApplicationStatus.REJECTED:
        application.status = ApplicationStatus.REJECTED
        return True, "updated"

    current_rank = STATUS_RANK.get(application.status, 0)
    new_rank = STATUS_RANK.get(new_status, 0)

    if new_rank > current_rank:
        application.status = new_status
        return True, "updated"

    return False, "same_rank"


def _notification_descriptor(link_result: AutoLinkResult, details: dict) -> str:
    """
    Build the "Company — Role" suffix, preferring user-entered values.

    Two sources, in trust order:
      1. the linked JobApplication's company/role — typed by the user;
      2. the LLM's extracted company_name/role_title — model output.

    (2) is why the notification records message_id: an extracted claim has to be
    checkable by opening the email it came from. Returns "" when neither source
    names anything, and the caller falls back to the bare headline.
    """
    application = link_result.application if link_result.linked else None

    if application:
        company, role = application.company, application.role
    else:
        company, role = details.get("company_name"), details.get("role_title")

    parts = [p.strip() for p in (company, role) if p and p.strip()]
    return " — ".join(parts)


def _stage_notification(
        message_id: int,
        category: Optional[str],
        link_result: AutoLinkResult,
        details: dict,
        user_id: int,
) -> None:
    """
    Stage an in-app notification onto the session (NO commit) so it is written in
    the SAME transaction as AnalysisResult/Tasks/AgentRun (atomic writes).

    Fires on the detected email category, regardless of whether the message linked to a
    tracked application — see NOTIFICATION_MAP for why.

    Trust: prefers the linked application's user-entered company/role and falls
    back to LLM-extracted values. That is a deliberate relaxation of the previous
    "never LLM text" rule, paid for by recording message_id so the claim can be
    verified against the source email. Rendered text is Jinja-autoescaped.

    Deduplicated on (message_id, notification_type) with a SELECT rather than a
    unique constraint: this row is staged into the pipeline's single transaction,
    where an IntegrityError would roll back the entire analysis and mark the
    message FAILED. first() rather than scalar_one_or_none() for the same reason —
    nothing at the database level stops duplicates predating this guard, and the
    probe must not raise on them.

    Truncates to Notification.message = String(300).
    """
    mapping = NOTIFICATION_MAP.get(category)
    if not mapping:
        return

    notification_type, headline = mapping

    already = db.session.execute(
        db.select(Notification.id).where(
            Notification.message_id == message_id,
            Notification.notification_type == notification_type
        )
        .limit(1)
    ).scalars().first()

    if already is not None: return

    descriptor = _notification_descriptor(link_result, details)
    message_text = f"{headline} — {descriptor}" if descriptor else headline

    if len(message_text) > 300:
        message_text = message_text[:297] + "..."

    db.session.add(Notification(
        user_id=user_id,
        message_id=message_id,
        message=message_text,
        notification_type=notification_type,
        is_read=False
    ))


def _mark_failed_nothing_to_show(message: Message, detected_lang: str) -> None:
    """
    Neither ML nor LLM produced output → nothing to show → FAILED (atomic).

    Writes an AgentRun audit row (status FAILED) and sets the message FAILED in one
    commit. Raises on DB failure — the task's outer handler marks the message FAILED.

    Refunds the analysis slot, but UNLIKE the other five FAILED paths this one is
    CAPPED. It still costs one billed extraction call, so an uncapped refund would
    let someone loop on garbage input and reset their quota forever. The first
    NOTHING_TO_SHOW_REFUND_CAP nothing-to-show results per user refund; after that
    the slot is consumed. The terminal FAILED state is unaffected either way — the
    cap only decides whether the slot comes back.
    """
    message_id = message.id  # capture before commit expires the instance

    try:
        db.session.add(AgentRun(
            message_id=message.id,
            agent_name="workflow_agent",
            selected_tools=[],
            decision_reason=f"No ML/LLM output | Detected language: {detected_lang}",
            status=AgentRunStatus.FAILED,
        ))
        message.status = MessageStatus.FAILED
        db.session.commit()
    except Exception:
        # Re-raised: analyze_message's generic handler then marks FAILED and calls
        # the UNCAPPED refund_analysis(). That is deliberate — a failure to commit
        # is a database problem, not a nothing-to-show result, so it is treated as
        # path D. The per-message marker keeps it safe either way.
        db.session.rollback()
        raise

    # After the commit: the refund helper opens with a rollback, which would discard
    # the FAILED write above if it ran first.
    refunded = refund_nothing_to_show(message_id)

    logger.warning(
        "Pipeline produced nothing to show for message %s — marked FAILED (refunded=%s)",
        message_id, refunded,
        extra={
            "message_id": message_id,
            "refunded": refunded
        }
    )
