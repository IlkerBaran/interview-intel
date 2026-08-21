"""
Public demo for replaying saved analyses through the real message UI.

A visitor can pick one of three saved examples, see the same "Analyzing..."
state used by the real app, and then view the stored result after about five
seconds. The demo does not run the analysis pipeline: no LLM call, ML call,
worker, queued job, or database write. The short wait only replays the real
async flow against an analysis that already exists.

The endpoints here are GET-only and load committed fixture data through
demo_service. That service returns plain dataclasses rather than ORM objects,
so the demo does not create or modify database state. The sample slug is
matched against the fixed set of committed examples and is never used to
construct a file path.

Because the demo is public, its rate limits are explicit. Page routes allow
60 requests per hour per IP, which is enough for normal browsing without
leaving the endpoint unbounded.

The status endpoint is also rate limited. Unlike the authenticated
messages.message_status endpoint, it is not protected by login_required, so
it uses a higher but still bounded limit of 120 requests per minute.
"""

import time

from flask import Blueprint, abort, render_template, request, url_for

from app.extensions import limiter
from app.services.demo_service import (
    DEMO_POLL_MS,
    REPLAY_SECONDS,
    get_sample,
    load_samples,
)

demo_bp = Blueprint("demo", __name__, url_prefix="/demo")


@demo_bp.route("/", methods=["GET"])
@limiter.limit("60 per hour")
def index():
    """The picker, plus the explanation of what this page is and is not."""
    return render_template(
        "demo/index.html",
        samples=list(load_samples().values()),
        replay_seconds=REPLAY_SECONDS,
    )


@demo_bp.route("/<slug>", methods=["GET"])
@limiter.limit("60 per hour")
def show(slug):
    """
    Render either the demo replay state or the finished result.

    Without ?ready=1, the page shows the same processing banner used by the real
    message page and lets analysis-poll.js handle the transition. Once the demo
    status endpoint reports completion, the poller navigates back with ?ready=1
    and the server renders the saved result.

    The replay start time is passed in the status URL instead of stored in a
    session, so the demo does not need any server-side state. It is intentionally
    not validated because it only controls the simulated wait, and skipping that
    wait does not expose anything sensitive.
    """
    sample = get_sample(slug)
    if sample is None:
        abort(404)

    if request.args.get("ready") == "1":
        return render_template("demo/show.html", sample=sample, ready=True)

    return render_template(
        "demo/show.html",
        sample=sample,
        ready=False,
        status_url=url_for("demo.status", slug=slug, started=time.time()),
        reload_url=url_for("demo.show", slug=slug, ready=1),
        poll_ms=DEMO_POLL_MS,
    )


@demo_bp.route("/<slug>/status", methods=["GET"])
@limiter.limit("120 per minute")
def status(slug):
    """
    The replay clock.

    Returns the exact MessageStatus VALUE strings the real endpoint returns, because
    analysis-poll.js compares against them literally (ADR-0009). No database lookup —
    the only state is the elapsed time the caller brought with it.
    """
    if get_sample(slug) is None:
        abort(404)

    started = request.args.get("started", type=float)
    if started is None:
        return {"status": "completed"}

    elapsed = time.time() - started
    return {"status": "processing" if elapsed < REPLAY_SECONDS else "completed"}
