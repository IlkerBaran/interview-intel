"""
Load the saved demo fixture into plain objects the shared message template can use.

The demo renders saved analyses through messages/_analysis.html, which expects
message, analysis result, and task attributes. We provide those with dataclasses
instead of SQLAlchemy models so the public demo stays separate from ORM state.

That separation is also a security/safety boundary for this endpoint. The demo is
supposed to be read-only, and using plain objects means there is no session, mapper,
or relationship cascade that could accidentally turn fixture data into a database
write later.

The fixture is loaded from the committed JSON file and cached after the first read.
Because the file ships with the code, changing a sample requires changing the
fixture and redeploying rather than mutating data at runtime.

See scripts/dump_demo_fixture.py for how the fixture is generated and which fields
it must contain.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional

from flask import current_app


logger = logging.getLogger(__name__)


FIXTURE_RELATIVE_PATH = Path("data") / "demo_samples.json"

# Keep the demo replay short. Real analyses can take around 15–30 seconds,
# but the demo only needs enough time to show the real processing state
# before revealing the saved result.
REPLAY_SECONDS = 5

# The demo poller runs faster than production's 3000ms so the reveal lands ON the
# five seconds rather than overshooting to the next 3s tick.
DEMO_POLL_MS = 1000

_samples: Optional[dict] = None


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """
    ISO-8601 with a trailing Z -> an aware UTC datetime.

    The macros hand these to localtime(), which calls .strftime() and .day and pipes
    them through the utc_iso filter. A string would raise, so this is not optional
    politeness — it is what makes the fixture renderable.
    """
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class DemoTask:
    task_name: str
    priority: str
    is_completed: bool
    due_date: Optional[datetime]
    due_text: Optional[str]

    @property
    def is_overdue(self) -> bool:
        """
        Mirror of models.Task.is_overdue, recomputed rather than stored.

        Freezing a boolean into the fixture would make it wrong the moment the
        calendar moved past it. The rule itself must match the model exactly —
        compare calendar DATES in UTC, never instants, because due_date is anchored
        at noon UTC and an instant comparison flips a task to overdue at midday on
        its own due day. tests/unit/test_demo.py asserts the two agree.
        """
        if self.due_date is None or self.is_completed:
            return False
        return self.due_date.astimezone(UTC).date() < datetime.now(UTC).date()


@dataclass(frozen=True)
class DemoAnalysisResult:
    message_category: Optional[str] = None
    category_confidence: Optional[float] = None
    urgency_level: Optional[str] = None
    urgency_confidence: Optional[float] = None
    job_field: Optional[str] = None
    job_field_confidence: Optional[float] = None
    company_name: Optional[str] = None
    role_title: Optional[str] = None
    interview_stage: Optional[str] = None
    interview_format: Optional[str] = None
    date_text: Optional[str] = None
    time_text: Optional[str] = None
    location_text: Optional[str] = None
    processed_at: Optional[datetime] = None
    preparation_guidance: Optional[str] = None
    suggested_questions: Optional[str] = None
    reply_suggestions: Optional[str] = None
    role_summary: Optional[str] = None


@dataclass(frozen=True)
class DemoMessage:
    status: str
    subject: Optional[str]
    sender_email: Optional[str]
    created_at: Optional[datetime]
    raw_text: str
    note: Optional[str]
    analysis_result: Optional[DemoAnalysisResult]
    tasks: list = field(default_factory=list)


@dataclass(frozen=True)
class DemoSample:
    slug: str
    label: str
    caption: str
    message: DemoMessage

    def pending(self) -> DemoMessage:
        """
        The same message in its pre-reveal state.

        The banner macro switches on message.status, so the replay's "Analyzing…"
        state is the real one driven by the real value — not a second banner written
        to look like it.
        """
        return replace(self.message, status="pending")


def _build_sample(entry: dict) -> DemoSample:
    demo, message = entry["demo"], entry["message"]
    analysis = message.get("analysis_result")

    return DemoSample(
        slug=demo["slug"],
        label=demo["label"],
        caption=demo["caption"],
        message=DemoMessage(
            status=message["status"],
            subject=message.get("subject"),
            sender_email=message.get("sender_email"),
            created_at=_parse_timestamp(message.get("created_at")),
            raw_text=message.get("raw_text") or "",
            note=message.get("note"),
            analysis_result=None if analysis is None else DemoAnalysisResult(
                **{
                    key: (_parse_timestamp(value) if key == "processed_at" else value)
                    for key, value in analysis.items()
                }
            ),
            tasks=[
                DemoTask(
                    task_name=task["task_name"],
                    priority=task["priority"],
                    is_completed=task["is_completed"],
                    due_date=_parse_timestamp(task.get("due_date")),
                    due_text=task.get("due_text"),
                )
                for task in message.get("tasks", [])
            ],
        ),
    )


def load_samples() -> dict:
    """Every sample, keyed by slug, in fixture order. Cached after the first read."""
    global _samples

    if _samples is None:
        path = Path(current_app.root_path) / FIXTURE_RELATIVE_PATH
        document = json.loads(path.read_text(encoding="utf-8"))
        _samples = {
            entry["demo"]["slug"]: _build_sample(entry)
            for entry in document["samples"]
        }
        logger.info("Demo fixture loaded: %s samples", len(_samples))

    return _samples


def get_sample(slug: str) -> Optional[DemoSample]:
    """
    One sample, or None.

    The slug is looked up in this dict and never used to build a path, so a demo URL
    cannot address anything but the samples that were committed.
    """
    return load_samples().get(slug)
