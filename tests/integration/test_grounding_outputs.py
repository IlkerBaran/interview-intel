"""
Integration test: the grounding rules hold against the REAL model, not just in the
prompt text.

WHY THIS IS AN INTEGRATION TEST
--------------------------------------------------------
The claim under test is about what the model writes. The unit tests in
tests/unit/test_llm_enrichment.py assert that each rule reaches the prompt, which is
necessary and NOT sufficient: both failures reproduced here shipped while a rule
forbidding them was already in the prompt. Only a live call can show whether the
wording actually changes the output, so this makes real, billed requests to the
Anthropic API and asserts on the prose that comes back.

THE TWO FAILURES BEING PINNED
--------------------------------------------------------
Both come from one captured interview email (EMAIL below, kept verbatim).

  1. The email offers, conditionally: "if you have a GitHub profile, portfolio, or
     anything else you'd like the panel to review beforehand, feel free to send it
     over." The reply resolved that condition on the candidate's behalf — "I don't
     currently have a GitHub profile or portfolio to share" — inventing a fact about
     someone it knows nothing about. A denial is an invention.

  2. The email states a 75-minute interview "split between system design and live
     coding" and gives no allocation. The preparation guidance published "System
     Design (40 min of the 75)" and "Live Coding (35 min)": arithmetic that sums to
     the stated total and is entirely fabricated.

The assertions are deliberately shaped, not literal. Matching the exact sentences
that shipped would pass the moment the model invents 45/30 instead, so these look for
the SHAPE of each failure — a first-person denial of possession, and a duration
attached to a section the email never apportioned.

Non-determinism is inherent: temperature is 0 in _call(), but a model revision can
still move the output. A failure here is a signal to re-examine the prompt, not
necessarily a code defect.

HOW TO RUN (from the project root)
--------------------------------------------------------
    FLASK_ENV=development RUN_LLM_GROUNDING_TESTS=1 \
        .venv/bin/pytest tests/integration/test_grounding_outputs.py -m integration -v

Excluded from the default suite twice over: the `integration` marker (pytest.ini
addopts is -m "not integration") and the RUN_LLM_GROUNDING_TESTS opt-in, so a plain
`pytest` run never spends money. Skips rather than fails when either is absent, or
when the config carries no real API key.
"""

import os
import re

import pytest

pytestmark = pytest.mark.integration


EMAIL = """Backend Engineer — technical interview

Hi Adam,

Good news — the team enjoyed reviewing your application and would like to invite you
to the next stage of the process for the Backend Engineer role.

We'd like to schedule a 75-minute technical interview over Zoom with two of our
platform engineers, Priya and Dan. The interview will be split between system design
and live coding. We use Go, Postgres, and Kafka quite heavily, so you can expect some
of the discussion to focus on those areas.

We've pencilled you in for Thursday, 27 August at 11:00 AM PT. Please confirm your
availability by Tuesday, 25 August so I can send over the calendar invite. If that
time doesn't work, send me two or three alternatives and I'll see what we can arrange.

Also, if you have a GitHub profile, portfolio, or anything else you'd like the panel
to review beforehand, feel free to send it over.

Looking forward to hearing from you.

Best,
Susan Miller
Technical Recruiting
A.B.C Data"""


# A first-person claim about NOT having something. The email establishes nothing
# about what the candidate owns or has built, in either direction.
DENIAL_RE = re.compile(
    r"\bI\s+(?:do\s+not|don'?t|dont)\s+(?:currently\s+|presently\s+)?have\b"
    r"|\bI\s+have\s+(?:no|none)\b"
    r"|\bI\s+(?:do\s+not|don'?t)\s+(?:currently\s+)?maintain\b",
    re.IGNORECASE,
)

# The two sections the email named but never apportioned.
SECTION_RE = re.compile(r"system\s+design|live\s+coding", re.IGNORECASE)

# A duration or share: "40 min", "35 minutes", "60%". The number and the unit are
# captured separately so the ONE duration the email actually states can be allowed
# through while any share of it stays forbidden.
QUANTITY_RE = re.compile(r"\b(\d{1,3})\s*(min\b|mins\b|minutes\b|%)", re.IGNORECASE)

# The only quantity the email gives. Restating it is grounded; apportioning it is not.
STATED_TOTAL_MINUTES = "75"

# Time the CANDIDATE is told to spend preparing is advice, not a claim about how the
# interview is divided. "Spend 45 minutes reviewing Kafka before the live coding
# round" is grounded; "Live coding (45 min)" is not. Without this the proximity check
# flags ordinary coaching, and a test that cries wolf stops being read.
PREP_VERB_RE = re.compile(
    r"\b(?:spend|spending|practi[cs]e|practi[cs]ing|study|studying|review|reviewing|"
    r"devote|devoting|dedicate|dedicating|budget|set aside|allow)\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def live_llm():
    """The real client, or a skip. Never the fake — the fake echoes the prompt."""
    if not os.getenv("RUN_LLM_GROUNDING_TESTS"):
        pytest.skip("set RUN_LLM_GROUNDING_TESTS=1 to make real, billed API calls")

    os.environ.setdefault("FLASK_ENV", "development")

    from app import create_app
    from app.services.llm_service import llm_service

    app = create_app()
    with app.app_context():
        if app.config.get("LLM_FAKE"):
            pytest.skip("LLM_FAKE is on — this test needs the real model")
        if not app.config.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY is not configured")

        llm_service.load(force=True)
        yield llm_service


def _sections_with_quantities(text):
    """Every place the output attaches a duration or share to a named section.

    Proximity rather than a bare number search: "practise for 30 minutes a day" is
    ordinary coaching advice, while "System Design (40 min of the 75)" is a claim
    about an interview the email never broke down.
    """
    found = []
    for match in SECTION_RE.finditer(text):
        window = text[max(0, match.start() - 60):match.end() + 60]
        for quantity in QUANTITY_RE.finditer(window):
            number, unit = quantity.group(1), quantity.group(2)

            # Repeating the stated total is faithful; a percentage never is, because
            # the email expresses nothing as a share.
            if unit != "%" and number == STATED_TOTAL_MINUTES:
                continue

            # A study-time instruction just before the figure means it is prep advice
            # rather than an assertion about the session's shape.
            lead_in = window[max(0, quantity.start() - 30):quantity.start()]
            if PREP_VERB_RE.search(lead_in):
                continue

            found.append((match.group(), quantity.group(), window.strip()))
    return found


def test_reply_does_not_resolve_the_conditional_offer(live_llm):
    """The email's "if you have a GitHub profile, portfolio, or anything else" is a
    decision for the candidate. The draft must leave it open, not settle it either
    way."""
    reply = live_llm.generate_reply_suggestions(
        raw_text=EMAIL,
        category="interview_invitation",
        role_title="Backend Engineer",
        company_name="A.B.C Data",
    )
    assert reply, "no reply returned"

    denial = DENIAL_RE.search(reply)
    assert denial is None, (
        "the draft resolved a conditional offer by denying the candidate has "
        f"something: {denial.group()!r}\n\n{reply}"
    )


def test_reply_leaves_the_optional_materials_as_a_placeholder(live_llm):
    """The positive half of the same rule: if the draft addresses the offer at all,
    it has to hand the choice back rather than answer it."""
    reply = live_llm.generate_reply_suggestions(
        raw_text=EMAIL,
        category="interview_invitation",
        role_title="Backend Engineer",
        company_name="A.B.C Data",
    )
    assert reply, "no reply returned"

    mentions_materials = re.search(
        r"github|portfolio|materials|review beforehand", reply, re.IGNORECASE
    )
    if mentions_materials:
        assert "[" in reply and "]" in reply, (
            "the draft raised the optional materials without leaving a bracketed "
            f"choice for the candidate:\n\n{reply}"
        )


def test_prep_does_not_apportion_the_unallocated_session(live_llm):
    """75 minutes "split between system design and live coding" is the whole of what
    the email says. Any per-section duration or share is invented."""
    guidance = live_llm.generate_preparation_guidance(
        role_title="Backend Engineer",
        company_name="A.B.C Data",
        interview_format="video",
        job_field="software_engineering",
        stage_note="An interview has been offered or scheduled.",
        date_text="Thursday, 27 August",
        time_text="11:00 AM PT",
        raw_text=EMAIL,
    )
    assert guidance, "no guidance returned"

    invented = _sections_with_quantities(guidance)
    assert not invented, (
        "the guidance attached durations or shares to sections the email never "
        f"apportioned: {invented}\n\n{guidance}"
    )
