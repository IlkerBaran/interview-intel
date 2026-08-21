"""
Tests for the public demo.

The demo explicitly promises that replaying a saved analysis does not run the
analysis pipeline, write to the database, or trigger a paid API call. These tests
verify that behavior rather than relying on the implementation to stay read-only
by convention.

The database-write test covers both anonymous and logged-in visitors. The logged-in
case matters because authenticated requests run context processors that use
db.session and may autoflush. If the demo loader ever started returning ORM objects
instead of plain dataclasses, that request path could expose an accidental write.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models import Message, MessageStatus, Task, User
from app.services import demo_service
from app.services.demo_service import REPLAY_SECONDS, get_sample, load_samples


@pytest.fixture(autouse=True)
def _clear_fixture_cache():
    """The loader caches at module level; keep tests independent of each other."""
    demo_service._samples = None
    yield
    demo_service._samples = None


@pytest.fixture
def client(app):
    """An anonymous client. Kept local rather than added to conftest so this file
    does not change the shared fixtures every other test builds on."""
    return app.test_client()


@pytest.fixture
def slugs(app):
    with app.app_context():
        return list(load_samples().keys())


# ≈≈≈≈ the fixture loads and covers what the template reads ≈≈≈≈

def test_every_sample_loads(app, slugs):
    assert len(slugs) == 3
    with app.app_context():
        for slug in slugs:
            sample = get_sample(slug)
            assert sample.label and sample.caption
            assert sample.message.status == "completed"
            assert sample.message.raw_text


def test_unknown_slug_returns_none(app):
    """The slug indexes a dict of committed samples; it never builds a path."""
    with app.app_context():
        assert get_sample("nope") is None
        assert get_sample("../../etc/passwd") is None


def test_timestamps_load_as_aware_datetimes(app, slugs):
    """localtime() calls .strftime() and .day and pipes through utc_iso — a string
    would raise at render time, not here."""
    with app.app_context():
        for slug in slugs:
            ar = get_sample(slug).message.analysis_result
            if ar and ar.processed_at:
                assert isinstance(ar.processed_at, datetime)
                assert ar.processed_at.tzinfo is not None


def test_nulls_survive_the_load(app):
    """A NULL confidence is the signal that a label was substituted rather than
    predicted, and show_message.html renders a different explanation for it."""
    with app.app_context():
        ar = get_sample("rejection").message.analysis_result
        assert ar.job_field_confidence is None
        assert ar.preparation_guidance is None
        assert ar.suggested_questions is None
        assert ar.reply_suggestions is None


def test_pending_view_only_changes_the_status(app):
    """The replay's "Analyzing…" state is the real banner driven by the real status
    value, not a second banner written to look like it."""
    with app.app_context():
        sample = get_sample("interview-invitation")
        pending = sample.pending()
        assert pending.status == "pending"
        assert sample.message.status == "completed"
        assert pending.raw_text == sample.message.raw_text
        assert pending.analysis_result is sample.message.analysis_result


# ≈≈≈≈ is_overdue is recomputed, and must not drift from the model ≈≈≈≈

@pytest.mark.parametrize("delta_days,completed", [
    (-2, False), (-1, False), (0, False), (1, False), (2, False), (-2, True),
])
def test_demo_task_overdue_matches_the_model(app, delta_days, completed):
    """DemoTask reimplements models.Task.is_overdue because a frozen boolean would
    go stale. Two implementations of one rule drift; this is what catches it —
    including the calendar-date comparison, where an instant comparison would flip a
    task to overdue at noon on its own due day."""
    due = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) \
        + timedelta(days=delta_days)

    demo_task = demo_service.DemoTask(
        task_name="t", priority="high", is_completed=completed,
        due_date=due, due_text=None,
    )
    model_task = Task(task_name="t", priority="high", is_completed=completed, due_date=due)

    assert demo_task.is_overdue is model_task.is_overdue


def test_demo_task_without_a_due_date_is_never_overdue():
    task = demo_service.DemoTask(
        task_name="t", priority="low", is_completed=False,
        due_date=None, due_text="before the call",
    )
    assert task.is_overdue is False


# ≈≈≈≈ the routes ≈≈≈≈

def test_index_renders_without_login(client, slugs):
    response = client.get("/demo/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for slug in slugs:
        assert f"/demo/{slug}" in body


def test_index_states_what_the_page_is(client):
    """The honesty copy is a requirement, not decoration. If it is ever edited away
    this fails rather than quietly shipping a page that implies live analysis."""
    body = client.get("/demo/").get_data(as_text=True)
    assert "Nothing runs live on this page." in body
    assert "runs the analysis pipeline, writes to the database, or\n            triggers a paid API call." in body
    assert "do not invoke the ML or LLM services" in body
    assert "rate limited per IP" in body


def test_replay_shows_the_real_processing_banner(client, slugs):
    """Same banner markup, same poller file, same status-value strings as the real
    message page."""
    response = client.get(f"/demo/{slugs[0]}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="analysis-status"' in body
    assert "⏳ Analyzing this email…" in body
    assert 'data-poll-ms="1000"' in body
    assert f"/demo/{slugs[0]}/status" in body
    assert f"data-reload-url=\"/demo/{slugs[0]}?ready=1\"" in body
    assert "js/analysis-poll.js" in body


def test_ready_renders_the_stored_result(client):
    body = client.get("/demo/interview-invitation?ready=1").get_data(as_text=True)
    assert "Classification" in body
    assert "Interview Details" in body
    assert "Preparation Guidance" in body
    assert "js/analysis-poll.js" not in body      # nothing left to poll for


def test_sparse_sample_omits_the_sections_it_has_no_data_for(client):
    """The rejection sample is the point of the demo: the app decides what is
    appropriate rather than generating everything always."""
    body = client.get("/demo/rejection?ready=1").get_data(as_text=True)
    assert "Classification" in body
    assert "Preparation Guidance" not in body
    assert "Questions to Ask" not in body
    assert "Suggested Reply" not in body


def test_every_page_carries_the_disclosure(client, slugs):
    """A deep link straight to ?ready=1 must still say what it is."""
    for slug in slugs:
        body = client.get(f"/demo/{slug}?ready=1").get_data(as_text=True)
        assert "Saved example — not a live analysis." in body


def test_no_page_offers_a_login_gate(client, slugs):
    """"No login required, and it must not appear to require one." The owner-only
    actions are absent, not disabled."""
    for slug in slugs:
        body = client.get(f"/demo/{slug}?ready=1").get_data(as_text=True)
        assert "Danger Zone" not in body
        assert "Personal Note" not in body
        assert "Archive" not in body
        assert "View all" not in body


def test_unknown_slug_is_404(client):
    assert client.get("/demo/not-a-sample").status_code == 404
    assert client.get("/demo/not-a-sample/status").status_code == 404


# ≈≈≈≈ the replay clock ≈≈≈≈

def test_status_reports_processing_then_completed(client, slugs):
    import time

    now = time.time()
    fresh = client.get(f"/demo/{slugs[0]}/status?started={now}")
    assert fresh.get_json() == {"status": "processing"}

    elapsed = client.get(f"/demo/{slugs[0]}/status?started={now - REPLAY_SECONDS - 1}")
    assert elapsed.get_json() == {"status": "completed"}


def test_status_without_a_start_is_already_complete(client, slugs):
    """`started` is client-supplied and unvalidated on purpose — there is nothing
    here to protect, and skipping the wait is allowed."""
    assert client.get(f"/demo/{slugs[0]}/status").get_json() == {"status": "completed"}


def test_status_returns_the_literal_value_strings(client, slugs):
    """analysis-poll.js compares against these exactly; str(MessageStatus.COMPLETED)
    would be 'MessageStatus.COMPLETED' and break the whole flow (ADR-0009)."""
    payload = client.get(f"/demo/{slugs[0]}/status").get_json()
    assert payload["status"] == MessageStatus.COMPLETED.value == "completed"


# ≈≈≈≈ the promise printed on the page ≈≈≈≈

def _writes_during(app, fn):
    """Every INSERT/UPDATE/DELETE the ORM flushes while fn() runs."""
    written = []

    def record(session, flush_context, instances):
        written.extend(("new", o) for o in session.new)
        written.extend(("dirty", o) for o in session.dirty)
        written.extend(("deleted", o) for o in session.deleted)

    event.listen(db.session, "before_flush", record)
    try:
        fn()
    finally:
        event.remove(db.session, "before_flush", record)
    return written


def test_demo_writes_nothing_anonymously(app, client, slugs):
    def visit():
        client.get("/demo/")
        for slug in slugs:
            client.get(f"/demo/{slug}")
            client.get(f"/demo/{slug}?ready=1")
            client.get(f"/demo/{slug}/status")

    assert _writes_during(app, visit) == []


def test_demo_writes_nothing_for_a_logged_in_visitor(app, client_logged_in, slugs):
    """The case that actually exercises the risk: an authenticated request runs both
    context processors, which query db.session and can autoflush. Plain dataclasses
    are what make this structurally impossible rather than merely unlikely."""
    def visit():
        client_logged_in.get("/demo/")
        for slug in slugs:
            client_logged_in.get(f"/demo/{slug}")
            client_logged_in.get(f"/demo/{slug}?ready=1")
            client_logged_in.get(f"/demo/{slug}/status")

    assert _writes_during(app, visit) == []


def test_demo_creates_no_rows(app, client, slugs):
    """Belt and braces on the flush listener: count the tables afterwards."""
    with app.app_context():
        before = (
            db.session.query(Message).count(),
            db.session.query(Task).count(),
            db.session.query(User).count(),
        )

    client.get("/demo/")
    for slug in slugs:
        client.get(f"/demo/{slug}?ready=1")

    with app.app_context():
        after = (
            db.session.query(Message).count(),
            db.session.query(Task).count(),
            db.session.query(User).count(),
        )

    assert before == after == (0, 0, 0)


def test_loader_returns_no_orm_objects(app, slugs):
    """The claim is structural, so assert the structure: nothing the demo hands the
    template is a mapped class, so no session can ever adopt it."""
    with app.app_context():
        for slug in slugs:
            sample = get_sample(slug)
            assert not isinstance(sample.message, db.Model)
            if sample.message.analysis_result:
                assert not isinstance(sample.message.analysis_result, db.Model)
            for task in sample.message.tasks:
                assert not isinstance(task, db.Model)
