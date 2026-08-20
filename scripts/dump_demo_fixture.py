"""
Dump completed analyses into the JSON fixture used by the demo.

The demo uses saved analysis results instead of running the pipeline again.
This script reads completed Messages from the database and copies only the
fields that show_message.html needs.

Preview the samples first without writing the file:

    FLASK_ENV=development .venv/bin/python scripts/dump_demo_fixture.py \
        --sample 12:interview-invitation \
        --sample 15:rejection \
        --sample 18:application-received \
        --dry-run

If the report looks right, run it again without --dry-run to create the
fixture:

    FLASK_ENV=development .venv/bin/python scripts/dump_demo_fixture.py \
        --sample 12:interview-invitation \
        --sample 15:rejection \
        --sample 18:application-received \
        --out app/data/demo_samples.json \
        --force

The database is read-only here. The script only runs SELECT queries and never
changes any database rows. A normal run writes demo_samples.json; --dry-run
only prints the report.

Important:

* Keep NULL values as NULL. Some confidence fields use None to tell the
  template why a label was shown, so they must not be changed to "" or 0.

* Before dumping, check that the fields in this script still match the fields
  used by show_message.html. If the template starts using a new attribute,
  stop the dump instead of creating an incomplete fixture.
"""


import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jinja2 import nodes  # noqa: E402


SCHEMA_VERSION = 1
TEMPLATE = "messages/show_message.html"


# ── Captured application payload ────────────────────────────────────────────
# Only fields needed to render the saved analysis belong here. Database fields
# that happen to exist alongside them are deliberately not copied.

MESSAGE_FIELDS = (
    "status",
    "subject",
    "sender_email",
    "created_at",
    "raw_text",
    "note",
)

ANALYSIS_FIELDS = (
    "message_category",
    "category_confidence",
    "urgency_level",
    "urgency_confidence",
    "job_field",
    "job_field_confidence",
    "company_name",
    "role_title",
    "interview_stage",
    "interview_format",
    "date_text",
    "time_text",
    "location_text",
    "processed_at",
    "preparation_guidance",
    "suggested_questions",
    "reply_suggestions",
    "role_summary",
)

TASK_FIELDS = (
    "task_name",
    "priority",
    "is_completed",
    "due_date",
    "due_text",
)


# Attributes touched by the template but deliberately not stored in the
# fixture. Anything omitted must have an explicit reason.
ACKNOWLEDGED_EXCLUSIONS = {
    "message.analysis_result":
        "structural — emitted as the nested 'analysis_result' object",

    "message.tasks":
        "structural — emitted as the nested 'tasks' list",

    "message.id":
        "only builds url_for() targets for owner actions such as archive, "
        "delete, notes, and status polling; the demo exposes none of them",

    "message.quota_refunded":
        "read only inside the failed-analysis branch; demo samples are "
        "required to have completed status",

    "task.is_overdue":
        "derived from due_date at render time rather than stored as a boolean "
        "that could become stale",
}


# ── Template/schema preflight ───────────────────────────────────────────────

def _dotted_path(node):
    """Return ('root', 'a.b.c') for a Getattr chain rooted at a Jinja Name."""
    attrs = []

    while isinstance(node, nodes.Getattr):
        attrs.append(node.attr)
        node = node.node

    if isinstance(node, nodes.Name):
        return node.name, ".".join(reversed(attrs))

    return None


def template_attribute_inventory(app):
    """
    Return every attribute access found in the template.

    The Jinja AST is used instead of text searching so multi-level attribute
    access is discovered reliably. Template aliases such as `ar` and `task`
    are accounted for by check_schema_covers_template().
    """
    source = app.jinja_env.loader.get_source(
        app.jinja_env,
        TEMPLATE,
    )[0]

    ast = app.jinja_env.parse(source)

    found = set()

    for node in ast.find_all(nodes.Getattr):
        path = _dotted_path(node)

        if path:
            found.add(f"{path[0]}.{path[1]}")

    return found


def check_schema_covers_template(app):
    """
    Fail if the template reads an attribute the fixture cannot provide.

    `ar` is the template's alias for message.analysis_result and `task` is the
    loop variable over message.tasks.
    """
    emitted = (
        {f"message.{field}" for field in MESSAGE_FIELDS}
        | {f"ar.{field}" for field in ANALYSIS_FIELDS}
        | {f"message.analysis_result.{field}" for field in ANALYSIS_FIELDS}
        | {f"task.{field}" for field in TASK_FIELDS}
    )

    touched = template_attribute_inventory(app)
    exclusions = set(ACKNOWLEDGED_EXCLUSIONS)

    unaccounted = sorted(touched - emitted - exclusions)

    if unaccounted:
        print(
            f"\nERROR: {TEMPLATE} reads attributes this fixture does not carry:",
            file=sys.stderr,
        )

        for path in unaccounted:
            print(f"    {path}", file=sys.stderr)

        print(
            "\nAdd each one to MESSAGE_FIELDS / ANALYSIS_FIELDS / TASK_FIELDS, "
            "or to ACKNOWLEDGED_EXCLUSIONS with the reason it is safe to omit.\n"
            "Rendering the demo without it would raise AttributeError.",
            file=sys.stderr,
        )

        raise SystemExit(2)

    excluded = touched & exclusions
    captured = touched - excluded

    print(f"  preflight: {len(touched)} attributes read by {TEMPLATE}")
    print(
        f"             {len(captured)} captured, "
        f"{len(excluded)} excluded by design"
    )


# ── Capture ─────────────────────────────────────────────────────────────────

def _serialise(value, to_iso):
    """
    Convert datetimes to ISO-8601 UTC strings.

    Every non-datetime value passes through untouched so None remains None.
    """
    if isinstance(value, datetime):
        return to_iso(value)

    return value


def _enum_value(value):
    """Return an enum's stored value, otherwise return the value unchanged."""
    return value.value if hasattr(value, "value") else value


def capture_message(message, to_iso):
    """Convert one Message into the plain-data payload used by the fixture."""
    payload = {
        field: _serialise(getattr(message, field), to_iso)
        for field in MESSAGE_FIELDS
    }

    # The template compares status against values such as "completed".
    payload["status"] = _enum_value(payload["status"])

    analysis = message.analysis_result

    payload["analysis_result"] = (
        None
        if analysis is None
        else {
            field: _serialise(getattr(analysis, field), to_iso)
            for field in ANALYSIS_FIELDS
        }
    )

    payload["tasks"] = [
        {
            field: _serialise(getattr(task, field), to_iso)
            for field in TASK_FIELDS
        }
        for task in message.tasks
    ]

    return payload


# ── Human-readable verification ─────────────────────────────────────────────

def describe_field_badge(job_field, confidence):
    """Describe how show_message.html will render the Field prediction."""
    if not job_field:
        return 'badge "Unknown"'

    if job_field == "unclassified" and confidence is None:
        return 'badge "Unclassified" + hint "not enough signal"'

    if confidence is None:
        return f'badge "{job_field}" + hint "from earlier emails"'

    return f'badge "{job_field}" (raw score {confidence}, no percentage shown)'


def report_sample(slug, payload):
    """Print the important rendering behavior of one captured sample."""
    analysis = payload["analysis_result"] or {}

    category_confidence = analysis.get("category_confidence")
    urgency_confidence = analysis.get("urgency_confidence")

    category_display = (
        "no percentage (NULL confidence)"
        if category_confidence is None
        else f"{int(category_confidence * 100)}%"
    )

    urgency_display = (
        "no percentage (NULL confidence)"
        if urgency_confidence is None
        else f"{int(urgency_confidence * 100)}%"
    )

    print(f"\n  [{slug}]")

    print(
        f"    category  {analysis.get('message_category')!r:>26}  "
        f"{category_display}"
    )

    print(
        f"    urgency   {analysis.get('urgency_level')!r:>26}  "
        f"{urgency_display}"
    )

    print(
        "    field     -> "
        + describe_field_badge(
            analysis.get("job_field"),
            analysis.get("job_field_confidence"),
        )
    )

    optional_sections = (
        "preparation_guidance",
        "suggested_questions",
        "reply_suggestions",
        "role_summary",
    )

    present = [
        field
        for field in optional_sections
        if analysis.get(field)
    ]

    absent = [
        field
        for field in optional_sections
        if not analysis.get(field)
    ]

    print(f"    sections rendered:  {', '.join(present) or '(none)'}")
    print(f"    sections omitted:   {', '.join(absent) or '(none)'}")

    for task in payload["tasks"]:
        if task["due_date"]:
            due = f"due_date {task['due_date']}"
        elif task["due_text"]:
            due = f"due_text {task['due_text']!r} (unparsed)"
        else:
            due = "no deadline"

        print(f"    task: {task['task_name']!r} — {due}")

    # Empty strings and NULL may look the same in the template but mean
    # different things in the stored result. Report them instead of silently
    # normalizing one into the other.
    blanks = []

    for field in MESSAGE_FIELDS:
        if payload.get(field) == "":
            blanks.append(f"message.{field}")

    for field in ANALYSIS_FIELDS:
        if analysis.get(field) == "":
            blanks.append(f"analysis_result.{field}")

    for index, task in enumerate(payload["tasks"]):
        for field in TASK_FIELDS:
            if task.get(field) == "":
                blanks.append(f"tasks[{index}].{field}")

    if blanks:
        print(
            "    NOTE: stored as empty string, not NULL: "
            + ", ".join(blanks)
        )


# ── Command-line parsing ────────────────────────────────────────────────────

def parse_sample(spec):
    """Convert '12:interview-invitation' into (12, 'interview-invitation')."""
    try:
        raw_id, slug = spec.split(":", 1)
        message_id = int(raw_id)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--sample expects <message_id>:<slug>, got {spec!r}"
        )

    slug = slug.strip()

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise argparse.ArgumentTypeError(
            f"invalid demo slug {slug!r}; use lowercase words separated "
            "by hyphens"
        )

    return message_id, slug


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--sample",
        action="append",
        type=parse_sample,
        required=True,
        metavar="ID:SLUG",
        help="message id and demo URL slug; repeat for each sample",
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "app/data/demo_samples.json",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing fixture",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the capture report without writing the fixture",
    )

    args = parser.parse_args()

    slugs = [slug for _, slug in args.sample]

    if len(slugs) != len(set(slugs)):
        raise SystemExit("demo sample slugs must be unique")

    if args.out.exists() and not args.force and not args.dry_run:
        raise SystemExit(
            f"{args.out} already exists. Re-run with --force to replace it "
            "(diff it afterwards — the samples are reviewed content)."
        )

    os.environ.setdefault("FLASK_ENV", "development")

    from app import create_app
    from app.extensions import db
    from app.models import Message

    app = create_app()

    print(f"  database: {app.config['SQLALCHEMY_DATABASE_URI']}")

    with app.app_context():
        check_schema_covers_template(app)

        # Reuse the application's timestamp serializer so the fixture follows
        # the same UTC rules as the rest of the app.
        to_iso = app.jinja_env.filters["utc_iso"]

        samples = []

        for message_id, slug in args.sample:
            message = db.session.get(Message, message_id)

            if message is None:
                raise SystemExit(f"message {message_id} not found")

            status = _enum_value(message.status)

            if status != "completed":
                raise SystemExit(
                    f"message {message_id} has status {status!r}; "
                    "only completed analyses can be captured"
                )

            if message.analysis_result is None:
                raise SystemExit(
                    f"message {message_id} has no analysis_result"
                )

            payload = capture_message(message, to_iso)
            report_sample(slug, payload)

            samples.append(
                {
                    # Demo-page metadata written by us, not produced by the
                    # analysis pipeline.
                    "demo": {
                        "slug": slug,
                        "label": "TODO: picker card title",
                        "caption": (
                            "TODO: one line on what this sample demonstrates"
                        ),
                    },

                    # Everything here came from the saved analysis.
                    "message": payload,
                }
            )

    document = {
        "schema_version": SCHEMA_VERSION,
        "samples": samples,
    }

    if args.dry_run:
        print("\n  dry run: fixture not written")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)

    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(
            document,
            fh,
            indent=2,
            ensure_ascii=False,
        )
        fh.write("\n")

    print(f"\n  wrote {len(samples)} samples to {args.out}")

    print(
        "  Fill in the TODO label/caption fields, then read each sample's "
        "raw_text once more before committing — it will be public."
    )


if __name__ == "__main__":
    main()