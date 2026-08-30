"""
LLM credential failures (401/403) must fail honestly instead of reporting success.

Before this behavior existed, a 401 was classified alongside 400/404/422 as
"permanent — degrade", so an analysis with a bad API key saved every LLM column NULL
and still finished COMPLETED. The user got a hollow page, no explanation, and a spent
quota slot.

The distinction under test is NARROW and the narrowness is the point:

    401 / 403  → our credentials are rejected  → FAILED + refund   (new)
    400 / 422  → this email is unprocessable   → ML-only COMPLETED (unchanged)
    429 / 529  → provider is busy              → retry             (unchanged)

test_bad_request_still_degrades_to_ml_only_completed and its 422 twin are the
regression guard: they fail if anyone widens the new branch to cover all permanent
anthropic.APIError subclasses.
"""

import anthropic
import httpx
import pytest

import app.celery_tasks as tasks
from app.extensions import db
from app.models import (
    AgentRun,
    AnalysisResult,
    Message,
    MessageStatus,
    User,
)
from app.services.llm_service import llm_service
from app.services.ml_service import ml_service
from app.services.workflow_service import LLM_CONFIG_FAILURE_REASON

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

# A realistic-looking secret. Asserted ABSENT from logs and rendered pages — if the
# real key ever reached either, this is the shape it would take.
FAKE_KEY = "sk-ant-api03-DO-NOT-LEAK-THIS-VALUE"


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_user(email="auth@example.com", analyses_used=1):
    user = User()
    user.email = email
    user.password = "T3st-secret*"
    user.is_verified = True
    user.analyses_used = analyses_used   # pretend the submission already spent a slot
    db.session.add(user)
    db.session.commit()
    return user


def _make_message(user=None, raw="Interview invite for the Backend Engineer role."):
    user = user or _make_user(email=f"u{db.session.query(User).count()}@example.com")
    msg = Message(user_id=user.id, raw_text=raw, status=MessageStatus.PENDING)
    db.session.add(msg)
    db.session.commit()
    return msg


def _raiser(exc):
    def _fn(*a, **k):
        raise exc
    return _fn


def _auth_error():
    return anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx.Response(401, request=REQ), body=None)


def _permission_error():
    return anthropic.PermissionDeniedError(
        "forbidden", response=httpx.Response(403, request=REQ), body=None)


def _bad_request():
    return anthropic.BadRequestError(
        "bad", response=httpx.Response(400, request=REQ), body=None)


def _unprocessable():
    return anthropic.UnprocessableEntityError(
        "unprocessable", response=httpx.Response(422, request=REQ), body=None)


@pytest.fixture
def llm_loaded(monkeypatch):
    """Make llm_service.is_loaded True without constructing a real client."""
    monkeypatch.setattr(llm_service, "_client", object())
    return llm_service


@pytest.fixture
def ml_loaded(monkeypatch):
    """
    Make the ML half succeed.

    Required for the degradation tests: unit config leaves ML unloaded, and with both
    engines dead the pipeline takes the nothing-to-show path instead of the ML-only
    path we want to pin down.
    """
    monkeypatch.setattr(ml_service, "_loaded", True)
    monkeypatch.setattr(ml_service, "predict", lambda text: {
        "category": "interview_invitation",
        "category_conf": 0.91,
        "urgency": "high",
        "urgency_conf": 0.77,
        "job_field": "software_engineering",
        "job_field_conf": 0.65,
    })
    return ml_service


# ══════════════════════════════════════════════════════════════════════════════
# 401 / 403 → FAILED, refunded, no result row
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("exc_factory", [_auth_error, _permission_error])
def test_credential_error_marks_failed(app, monkeypatch, llm_loaded, ml_loaded, exc_factory):
    """401 and 403 both end FAILED — never COMPLETED — even though ML succeeded."""
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(exc_factory()))

    msg = _make_message()
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    db.session.refresh(msg)
    assert msg.status == MessageStatus.FAILED


@pytest.mark.parametrize("exc_factory", [_auth_error, _permission_error])
def test_credential_error_saves_no_analysis_result(
        app, monkeypatch, llm_loaded, ml_loaded, exc_factory):
    """
    No half-written result survives.

    A saved ML-only row is exactly the hollow artifact this change exists to prevent:
    it would render as a normal, merely-short analysis page.
    """
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(exc_factory()))

    msg = _make_message()
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    rows = db.session.execute(
        db.select(AnalysisResult).where(AnalysisResult.message_id == msg.id)
    ).scalars().all()
    assert rows == []


def test_credential_error_does_not_retry(app, monkeypatch, llm_loaded, ml_loaded):
    """
    Exactly one attempt. A rejected credential will be rejected identically three more
    times; retrying only delays the terminal state and burns the retry budget.
    """
    calls = []

    def counting_raise(*a, **k):
        calls.append(1)
        raise _auth_error()

    monkeypatch.setattr(llm_service, "extract_interview_details", counting_raise)

    msg = _make_message()
    result = tasks.analyze_message.apply(args=[msg.id], throw=False)

    assert len(calls) == 1                    # not 1 + max_retries
    assert result.successful()                # terminal-marked, not a Celery FAILURE


# ══════════════════════════════════════════════════════════════════════════════
# quota: refunded, uncapped, idempotent
# ══════════════════════════════════════════════════════════════════════════════

def test_credential_error_refunds_the_slot(app, monkeypatch, llm_loaded, ml_loaded):
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(_auth_error()))

    user = _make_user(analyses_used=1)
    msg = _make_message(user=user)
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    db.session.refresh(user)
    db.session.refresh(msg)
    assert user.analyses_used == 0
    assert msg.quota_refunded is True


def test_credential_refund_is_uncapped(app, monkeypatch, llm_loaded, ml_loaded):
    """
    Every credential failure refunds, unlike the capped nothing-to-show path.

    The cap exists to stop users farming refunds by submitting garbage. A 401 is the
    operator's fault and is not a billed call, so the cap must not apply — and its
    budget must stay untouched, or a broken key would quietly exhaust the user's
    ability to be refunded for genuine nothing-to-show results later.
    """
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(_auth_error()))

    user = _make_user(analyses_used=3)
    for _ in range(3):
        msg = _make_message(user=user)
        tasks.analyze_message.apply(args=[msg.id], throw=True)

    db.session.refresh(user)
    assert user.analyses_used == 0               # all three came back
    assert user.nothing_to_show_refunds_used == 0  # capped budget never spent


def test_credential_refund_is_idempotent_under_redelivery(
        app, monkeypatch, llm_loaded, ml_loaded):
    """acks_late can redeliver the task; the slot must come back once, not twice."""
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(_auth_error()))

    user = _make_user(analyses_used=2)
    msg = _make_message(user=user)

    tasks.analyze_message.apply(args=[msg.id], throw=True)
    tasks.analyze_message.apply(args=[msg.id], throw=True)   # redelivery

    db.session.refresh(user)
    assert user.analyses_used == 1               # decremented once


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION GUARD — 400/422 must still degrade to ML-only COMPLETED
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "exc_factory, label",
    [(_bad_request, "400"), (_unprocessable, "422")],
)
def test_bad_request_still_degrades_to_ml_only_completed(
        app, monkeypatch, llm_loaded, ml_loaded, exc_factory, label):
    """
    THE narrowness guard.

    400 and 422 are permanent too, but they are properties of THIS email, not of the
    deployment — so the long-standing behavior stands: keep the ML classification,
    finish COMPLETED, leave the LLM columns NULL.

    If someone widens the credential branch to catch anthropic.APIError (or
    APIStatusError) generally, these two cases start failing and refunding, and this
    test goes red.
    """
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(exc_factory()))

    user = _make_user(analyses_used=1)
    msg = _make_message(user=user)
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    db.session.refresh(msg)
    db.session.refresh(user)

    # still a success
    assert msg.status == MessageStatus.COMPLETED, f"{label} must not become FAILED"

    # the ML half was kept
    result = db.session.execute(
        db.select(AnalysisResult).where(AnalysisResult.message_id == msg.id)
    ).scalar_one()
    assert result.message_category == "interview_invitation"
    assert result.urgency_level == "high"
    assert result.job_field == "software_engineering"

    # the LLM half is absent, not invented
    assert not result.company_name
    assert not result.role_title
    assert not result.preparation_guidance
    assert not result.reply_suggestions

    # a delivered result keeps its slot
    assert user.analyses_used == 1
    assert msg.quota_refunded is False

    # and no credential-failure marker was written
    assert not _config_failure_rows(msg.id)


def test_transient_error_still_retries_and_is_not_a_config_failure(
        app, monkeypatch, llm_loaded, ml_loaded):
    """
    429/529/timeouts keep the original retry-then-FAILED behavior, and must not be
    mislabeled as a credential problem in the UI.
    """
    calls = []

    def always_timeout(*a, **k):
        calls.append(1)
        raise anthropic.APITimeoutError(request=REQ)

    monkeypatch.setattr(llm_service, "extract_interview_details", always_timeout)

    msg = _make_message()
    tasks.analyze_message.apply(args=[msg.id], throw=False)

    assert len(calls) == 4                       # 1 + max_retries(3), unchanged
    db.session.refresh(msg)
    assert msg.status == MessageStatus.FAILED
    assert not _config_failure_rows(msg.id)      # generic banner, not "service down"


# ══════════════════════════════════════════════════════════════════════════════
# enrichment-stage credential loss
# ══════════════════════════════════════════════════════════════════════════════

def test_credential_error_during_enrichment_fails_and_refunds(
        app, monkeypatch, llm_loaded, ml_loaded):
    """
    Key revoked mid-run: extraction succeeded, enrichment gets 401.

    Best-effort degradation is right for a one-off enrichment failure but wrong here —
    every remaining call fails the same way, leaving extracted fields with no guidance.
    That is the same hollow result, so it fails and refunds too.
    """
    monkeypatch.setattr(llm_service, "extract_interview_details", lambda *a, **k: {
        "company_name": "Acme",
        "role_title": "Backend Engineer",
        "action_items": [],
    })
    for name in (
        "generate_preparation_guidance",
        "suggest_candidate_questions",
        "generate_reply_suggestions",
        "generate_role_summary",
        "generate_archive_summary",
    ):
        monkeypatch.setattr(llm_service, name, _raiser(_auth_error()))

    user = _make_user(analyses_used=1)
    msg = _make_message(user=user)
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    db.session.refresh(msg)
    db.session.refresh(user)
    assert msg.status == MessageStatus.FAILED
    assert user.analyses_used == 0
    # the successful extraction is deliberately discarded rather than half-saved
    assert db.session.execute(
        db.select(AnalysisResult).where(AnalysisResult.message_id == msg.id)
    ).scalars().all() == []


def test_single_enrichment_failure_still_degrades(app, monkeypatch, llm_loaded, ml_loaded):
    """
    Non-credential enrichment failures stay best-effort: that one field goes NULL and
    the analysis still completes. Guards the other side of the new except clause.
    """
    monkeypatch.setattr(llm_service, "extract_interview_details", lambda *a, **k: {
        "company_name": "Acme",
        "role_title": "Backend Engineer",
        "action_items": [],
    })
    monkeypatch.setattr(
        llm_service, "generate_preparation_guidance", _raiser(RuntimeError("boom")))
    for name in (
        "suggest_candidate_questions",
        "generate_reply_suggestions",
        "generate_role_summary",
        "generate_archive_summary",
    ):
        monkeypatch.setattr(llm_service, name, lambda *a, **k: "canned text")

    msg = _make_message()
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    db.session.refresh(msg)
    assert msg.status == MessageStatus.COMPLETED
    result = db.session.execute(
        db.select(AnalysisResult).where(AnalysisResult.message_id == msg.id)
    ).scalar_one()
    assert result.preparation_guidance is None   # the one that failed
    assert result.company_name == "Acme"         # the rest survived


# ══════════════════════════════════════════════════════════════════════════════
# audit marker + rendered UI
# ══════════════════════════════════════════════════════════════════════════════

def _config_failure_rows(message_id):
    return db.session.execute(
        db.select(AgentRun).where(
            AgentRun.message_id == message_id,
            AgentRun.decision_reason == LLM_CONFIG_FAILURE_REASON,
        )
    ).scalars().all()


def test_credential_error_writes_the_audit_marker(app, monkeypatch, llm_loaded, ml_loaded):
    """The marker is what lets the page distinguish 'service down' from 'bad email'."""
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(_auth_error()))

    msg = _make_message()
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    rows = _config_failure_rows(msg.id)
    assert len(rows) == 1
    # the CONSTANT, never the provider's text
    assert rows[0].decision_reason == LLM_CONFIG_FAILURE_REASON


def test_failed_page_explains_without_leaking(app, client_logged_in, monkeypatch,
                                              llm_loaded, ml_loaded):
    """
    What the reviewer actually sees: an explanation, and nothing sensitive.

    The page must not carry the API key, the SDK exception name, or the status code —
    those belong in the worker log, not in a user-facing page.
    """
    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(_auth_error()))

    user = db.session.execute(db.select(User)).scalars().first()
    user.analyses_used = 1               # the submission spent a slot
    db.session.commit()
    msg = _make_message(user=user)
    tasks.analyze_message.apply(args=[msg.id], throw=True)

    # In production the web server is a different process from the worker and always
    # reads fresh rows. Here both share one session, whose identity map would hand the
    # route the pre-task copy of the message. Expiring reproduces the real read.
    db.session.expire_all()

    body = client_logged_in.get(f"/messages/{msg.id}").get_data(as_text=True)

    assert "the analysis service isn't available" in body
    assert "This didn&#39;t use one of your analyses." in body or \
           "This didn't use one of your analyses." in body

    assert FAKE_KEY not in body
    assert "sk-ant" not in body
    assert "AuthenticationError" not in body
    assert "401" not in body
    assert "invalid x-api-key" not in body


def test_ordinary_failure_keeps_the_generic_banner(app, client_logged_in, monkeypatch,
                                                   llm_loaded, ml_loaded):
    """
    A failure with no credential marker must NOT claim the service is down — that
    would misdirect a user whose email simply could not be analyzed.
    """
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(anthropic.APITimeoutError(request=REQ)))

    user = db.session.execute(db.select(User)).scalars().first()
    msg = _make_message(user=user)
    tasks.analyze_message.apply(args=[msg.id], throw=False)

    db.session.expire_all()   # see note in the test above

    body = client_logged_in.get(f"/messages/{msg.id}").get_data(as_text=True)

    assert "Analysis failed for this email" in body
    assert "the analysis service isn't available" not in body


def test_no_credentials_reach_the_log(app, monkeypatch, caplog, llm_loaded, ml_loaded):
    """The operator gets a pointer to the cause; the log never gets the key itself."""
    import logging

    monkeypatch.setitem(app.config, "ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr(
        llm_service, "extract_interview_details", _raiser(_auth_error()))

    msg = _make_message()
    with caplog.at_level(logging.ERROR):
        tasks.analyze_message.apply(args=[msg.id], throw=True)

    text = caplog.text
    assert "check ANTHROPIC_API_KEY" in text        # actionable for the operator
    assert "AuthenticationError" in text            # the class name is fine in logs
    assert FAKE_KEY not in text                     # the value is not
    assert "sk-ant" not in text
