"""
Unit tests for the detectors used by tests/integration/test_grounding_outputs.py.

Those integration tests only run on an explicit opt-in and cost real money, so their
matching logic would otherwise go unexercised in normal development — and a detector
that has quietly stopped matching anything passes vacuously, reporting success while
proving nothing. These run in the default suite, need no API, and pin both directions:
the outputs that shipped must be caught, and ordinary correct prose must not be.

The corpus below is the point of the file. Each MUST-CATCH entry is either the real
failing output or a plausible variation of it, so the detector cannot be narrowed to
the exact sentence that happened to appear once.
"""

import pytest

from tests.integration.test_grounding_outputs import (
    DENIAL_RE,
    _sections_with_quantities,
)


# ≈≈≈≈ inventing a fact about the candidate ≈≈≈≈

@pytest.mark.parametrize("text", [
    # The sentence that shipped.
    "I don't currently have a GitHub profile or portfolio to share, but please let me"
    " know if there's anything else that would help.",
    "I do not have a portfolio to send over at this time.",
    "I dont have any public repositories to share.",
    "I have no portfolio to share.",
    "I don't maintain a public GitHub profile.",
])
def test_denial_of_possession_is_caught(text):
    assert DENIAL_RE.search(text), f"denial not detected: {text!r}"


@pytest.mark.parametrize("text", [
    # Leaving the choice open is the wanted behaviour and must never trip.
    "[If you have a GitHub profile or portfolio you would like the panel to review,"
    " add it here.]",
    "Please let me know if there is anything else the panel would find useful.",
    "I have attached the materials you asked for.",
    "I don't want to take up more of your time than necessary.",
])
def test_ordinary_reply_prose_is_not_flagged_as_a_denial(text):
    assert not DENIAL_RE.search(text), f"false positive: {text!r}"


# ≈≈≈≈ apportioning a session the email left undivided ≈≈≈≈

@pytest.mark.parametrize("text", [
    # What shipped.
    "**System Design (40 min of the 75)**\n- Review distributed systems fundamentals",
    "**Live Coding (35 min)**\n- Brush up on Go fundamentals",
    # The same invention with different arithmetic — pinning 40/35 alone would let
    # this through on the next run.
    "System Design (45 min), Live Coding (30 min)",
    "Roughly 60% system design and 40% live coding.",
    "Live coding will take about 35 minutes.",
    "Expect around 50 minutes of system design.",
])
def test_invented_allocation_is_caught(text):
    assert _sections_with_quantities(text), f"invention not detected: {text!r}"


@pytest.mark.parametrize("text", [
    # Restating the one duration the email gives is faithful, not invented.
    "The interview is 75 minutes, split between system design and live coding.",
    "Expect a 75 min session covering system design and live coding.",
    # Study time is advice to the candidate, not a claim about the session's shape.
    "Practice 2-3 coding problems for 30 minutes a day.",
    "Spend 45 minutes reviewing Kafka before the live coding round.",
    "Set aside 20 minutes to rehearse a system design walkthrough.",
    "Devote 90 minutes to Go fundamentals ahead of the live coding portion.",
    # Naming the sections without quantifying them is exactly what is wanted.
    "The session is split between system design and live coding; prepare for both.",
])
def test_grounded_guidance_is_not_flagged(text):
    assert not _sections_with_quantities(text), f"false positive: {text!r}"


def test_detector_reports_what_it_matched():
    """The failure message has to name the figure and the section, or a red run
    leaves you re-reading the whole output to find the invention."""
    found = _sections_with_quantities("**System Design (40 min of the 75)**")

    assert len(found) == 1
    section, quantity, window = found[0]
    assert section.lower() == "system design"
    assert quantity.replace(" ", "") == "40min"
    assert "System Design" in window
