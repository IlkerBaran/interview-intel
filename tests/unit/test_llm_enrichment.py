"""
Stage 3 regression proof: the 5 LLM enrichments now populate (non-None), and
llm_service._call() is Flask-context-free.

On the old code the enrichments ran in ThreadPoolExecutor worker threads, where
_call() read current_app and raised RuntimeError, which a bare `except` swallowed to
None. This test runs the sequential enrichment runner with a fake client and NO app
context — it would fail on the old code and passes after the root-cause fix.
"""


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def create(self, **kwargs):
        return _FakeResp("REAL OUTPUT")


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _load_fake_llm():
    """Wire a fake Anthropic client onto the singleton without needing an API key."""
    from app.services import llm_service as mod
    svc = mod.llm_service
    svc._client = _FakeClient()   # is_loaded → True
    svc._model = "test-model"
    svc._fake = False             # exercise the REAL _call path against a fake client
    return svc


def test_enrichments_populate_without_flask_context():
    _load_fake_llm()
    from app.services.workflow_service import _run_llm_enrichments

    # Deliberately NOT inside any app.app_context() — proves _call is context-free.
    out = _run_llm_enrichments(
        normalized_text="some interview email text",
        category="interview_invitation",
        role_title="Software Engineer",
        company_name="Acme",
        interview_stage="final",
        interview_format="onsite",
        job_field="software",
    )

    assert set(out) == {
        "preparation_guidance", "suggested_questions",
        "reply_suggestions", "role_summary", "archive_summary",
    }
    assert all(v is not None for v in out.values())   # the bug is fixed
