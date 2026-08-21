"""
Tests for app.markup.render_llm_text.

This renderer turns model output into HTML on a public page, so the main thing
these tests protect is that model text never becomes raw markup.

The renderer escapes the input first, then adds only the small set of tags it
creates itself. The security tests parse the rendered HTML and verify two things:
only expected tags are present, and none of them have attributes.

Parsing matters here because escaped text can still contain words like `onerror`
or `src` without being executable HTML. Checking substrings alone would give false
positives.

Keeping attributes out of the output also means there is no `href`, `src`, or
event handler for a payload to use.
"""

import pytest
from bs4 import BeautifulSoup
from markupsafe import Markup

from app.markup import render_llm_text

# Everything the renderer is allowed to construct. Deliberately no <a>: model output
# reaches a public page, and a generated hyperlink there is a phishing surface for no
# benefit, since the prompts never ask for URLs.
ALLOWED_TAGS = {"p", "br", "hr", "strong", "ul", "ol", "li", "h3", "h4", "h5", "h6"}

HOSTILE = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg onload=alert(1)>",
    "<style>body{display:none}</style>",
    "<form action='/steal'><input name=p></form>",
    "<div onclick='x'>y</div>",
    "<iframe src='https://evil.example'></iframe>",
    "**<b>bold</b>**",
    "# <iframe src='evil'></iframe>",
    "- <a href='https://evil.example'>click</a>",
    "[link](https://evil.example)",
    "<!-- comment -->",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "</p><script>alert(1)</script><p>",
    "<a href=\"javascript:alert(1)\">x</a>",
]


def _tags(html):
    return {tag.name for tag in BeautifulSoup(str(html), "html.parser").find_all()}


def _attributes(html):
    found = set()
    for tag in BeautifulSoup(str(html), "html.parser").find_all():
        found |= set(tag.attrs)
    return found


# ≈≈≈≈ the safety property ≈≈≈≈

@pytest.mark.parametrize("hostile", HOSTILE)
def test_only_allowlisted_tags_are_ever_emitted(hostile):
    assert _tags(render_llm_text(hostile)) <= ALLOWED_TAGS


@pytest.mark.parametrize("hostile", HOSTILE)
def test_no_tag_ever_carries_an_attribute(hostile):
    """No href, no src, no on* — nothing for a payload to ride on."""
    assert _attributes(render_llm_text(hostile)) == set()


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_input_survives_as_readable_text(hostile):
    """Escaped, not dropped. Silently deleting content would hide what the model
    actually said, which is worse than showing it inert."""
    text = BeautifulSoup(str(render_llm_text(hostile)), "html.parser").get_text()
    assert "script" in text or "<" in text or "link" in text or "comment" in text


def test_a_link_is_left_as_literal_text():
    rendered = render_llm_text("See [our careers page](https://evil.example) for more.")
    assert "<a" not in str(rendered)
    assert "https://evil.example" in str(rendered)


def test_returns_markup_so_jinja_does_not_double_escape():
    assert isinstance(render_llm_text("hello"), Markup)


# ≈≈≈≈ the syntax the enrichments actually emit ≈≈≈≈

def test_headings_start_at_h3():
    """h1 is the message title and h2 the section headings, so model headings must
    stay subordinate to both rather than competing with them."""
    assert "<h3>Interview Prep</h3>" in render_llm_text("# Interview Prep")
    assert "<h4>System Design</h4>" in render_llm_text("## System Design")
    assert "<h5>Detail</h5>" in render_llm_text("### Detail")


def test_heading_levels_are_clamped():
    assert "<h6>" in render_llm_text("###### Deep")


def test_bold_renders():
    assert "<strong>Timeline:</strong>" in render_llm_text("**Timeline:** 7 days")


def test_bullets_become_one_list():
    rendered = str(render_llm_text("- first\n- second\n- third"))
    assert rendered.count("<ul>") == 1
    assert rendered.count("<li>") == 3


@pytest.mark.parametrize("marker", ["-", "*", "+"])
def test_every_bullet_marker_is_recognised(marker):
    assert "<ul><li>item</li></ul>" == str(render_llm_text(f"{marker} item"))


def test_numbered_lists_become_ordered_lists():
    rendered = str(render_llm_text("1. first\n2. second"))
    assert rendered.startswith("<ol>")
    assert rendered.count("<li>") == 2


def test_switching_marker_style_starts_a_new_list():
    """Mixing them into one list would silently misrepresent the model's structure."""
    rendered = str(render_llm_text("- bullet\n1. numbered"))
    assert "<ul><li>bullet</li></ul>" in rendered
    assert "<ol><li>numbered</li></ol>" in rendered


def test_blank_lines_separate_paragraphs():
    rendered = str(render_llm_text("First para.\n\nSecond para."))
    assert rendered == "<p>First para.</p><p>Second para.</p>"


def test_single_newlines_inside_a_paragraph_become_breaks():
    """Load-bearing for the reply draft: "Best regards," and "[Your name]" are
    separate lines, and joining them corrupts an email the user is about to send."""
    rendered = str(render_llm_text("Best regards,\n[Your name]"))
    assert rendered == "<p>Best regards,<br>[Your name]</p>"


def test_plain_prose_with_no_markdown_still_renders():
    """The renderer exists because a prompt cannot guarantee Markdown either way."""
    assert str(render_llm_text("Just a sentence.")) == "<p>Just a sentence.</p>"


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\n"])
def test_empty_input_returns_empty_markup(empty):
    """Callers keep using `{% if ar.preparation_guidance %}` to decide whether a
    whole section renders, so empty in must mean empty out."""
    assert str(render_llm_text(empty)) == ""


def test_no_markdown_characters_survive_a_realistic_guidance_block():
    source = (
        "# Interview Prep: Backend Engineer\n\n"
        "**Timeline:** You have 7 days.\n\n"
        "## System Design\n"
        "- Review distributed systems\n"
        "- Study **Kafka** specifically\n\n"
        "## Live Coding\n"
        "1. Practise in Go\n"
        "2. Explain your approach aloud\n"
    )
    rendered = str(render_llm_text(source))

    assert "**" not in rendered
    assert "# " not in rendered
    assert rendered.count("<h3>") == 1
    assert rendered.count("<h4>") == 2
    assert rendered.count("<ul>") == 1
    assert rendered.count("<ol>") == 1
    assert _tags(rendered) <= ALLOWED_TAGS


def test_blank_lines_between_items_keep_one_list():
    """A "loose list" in Markdown — items separated by blank lines — is still ONE
    list. Closing it on the blank line split suggested_questions into four separate
    <ol>s, so every question rendered as item "1." on the demo."""
    rendered = str(render_llm_text("1. first\n\n2. second\n\n3. third"))

    assert rendered.count("<ol>") == 1
    assert rendered.count("<li>") == 3


def test_a_paragraph_after_a_list_still_closes_it():
    """The list must not swallow the prose that follows it."""
    rendered = str(render_llm_text("- item\n\nA following sentence."))

    assert rendered == "<ul><li>item</li></ul><p>A following sentence.</p>"


def test_a_heading_after_a_list_still_closes_it():
    rendered = str(render_llm_text("- item\n\n## Next section"))

    assert rendered == "<ul><li>item</li></ul><h4>Next section</h4>"


@pytest.mark.parametrize("rule", ["---", "***", "___", "- - -", "  ----  "])
def test_a_thematic_break_becomes_an_hr(rule):
    """The model puts `---` between sections. Without this it fell through to the
    paragraph branch and rendered as a literal "---" on the page — three of them in
    the re-captured guidance."""
    assert str(render_llm_text(rule)) == "<hr>"


def test_a_rule_is_not_mistaken_for_a_bullet():
    """"- - -" matches the bullet pattern too; the rule check has to run first."""
    assert "<li>" not in str(render_llm_text("- - -"))


def test_a_dash_bullet_is_still_a_bullet():
    assert str(render_llm_text("- item")) == "<ul><li>item</li></ul>"
