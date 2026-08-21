"""
Render the small Markdown subset produced by LLM enrichments, safely.

The enrichment prompts ask for prose, but the model often returns light Markdown
such as `# headings`, `**bold**`, `- bullets`, and numbered lists. Without a
renderer, those characters show up literally on both the message page and the
public demo.

WHY RENDER INSTEAD OF ASKING FOR PLAIN TEXT

A prompt rule cannot guarantee that the model will always return plain text.
The app has already had cases where explicit grounding rules were present but the
model still ignored them. Rendering the small subset we actually use is therefore
more reliable, and it also fixes existing saved demo output without re-running the
analysis pipeline.

WHY NOT USE A MARKDOWN LIBRARY

There is no Markdown renderer or HTML sanitiser declared as a production
dependency. More importantly, this renderer keeps the security model simple:
the LLM output is escaped first, and only then are a few known formatting patterns
converted into HTML. That means every real HTML tag in the result is created by
this module rather than by the model.

tests/unit/test_markup.py verifies that rendered output contains only the allowed
tags and never contains HTML attributes. This prevents things such as `href`,
`src`, `onclick`, or `onerror` from becoming an execution or phishing surface.

This is intentionally not a full Markdown implementation. It supports only the
syntax currently produced by the enrichment output and leaves unsupported markup
as literal text.

Links are deliberately not rendered. Model output is shown on a public,
unauthenticated page, and generated hyperlinks would create an unnecessary
phishing surface when the prompts do not require links in the first place.
"""


import re

from markupsafe import Markup, escape


# Applied AFTER escaping, so these only ever match the literal characters the model
# wrote — never anything that came out of an HTML entity.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

# The page owns h1 (the message title) and h2 (section headings), so model headings
# start below those rather than competing with them.
_HEADING_BASE = 3
_HEADING_MAX = 6


def _inline(text: str) -> str:
    """Bold only. `text` is already escaped; the tags added here are ours."""
    return _BOLD_RE.sub(r"<strong>\1</strong>", text)


def _flush_paragraph(buffer: list, out: list) -> None:
    """
    Close an open paragraph.

    Single newlines inside a paragraph become <br>, which matters for the reply
    draft: "Best regards," and "[Your name]" are separate lines, and collapsing them
    into one would corrupt an email the user is about to send.
    """
    if buffer:
        out.append("<p>" + "<br>".join(_inline(line) for line in buffer) + "</p>")
        buffer.clear()


def _flush_list(items: list, out: list, ordered: bool) -> None:
    if items:
        tag = "ol" if ordered else "ul"
        rendered = "".join(f"<li>{_inline(item)}</li>" for item in items)
        out.append(f"<{tag}>{rendered}</{tag}>")
        items.clear()


def render_llm_text(text) -> Markup:
    """
    Model prose -> safe HTML.

    Returns an empty Markup for empty input so callers can keep using the plain
    `{% if ar.preparation_guidance %}` guards that decide whether a section renders
    at all.
    """
    if not text:
        return Markup("")

    # Everything downstream operates on escaped text. Nothing the model wrote can
    # become markup from this point on.
    escaped = str(escape(text))

    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_ordered = False

    for raw_line in escaped.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()

        if not line.strip():
            # Close a paragraph but NOT an open list. Markdown calls a list whose
            # items are separated by blank lines a "loose list" — it is still one
            # list. Flushing here split `suggested_questions` into four separate
            # <ol>s, so every question rendered as item "1.".
            _flush_paragraph(paragraph, out)
            continue

        heading = _HEADING_RE.match(line.strip())
        if heading:
            _flush_paragraph(paragraph, out)
            _flush_list(list_items, out, list_ordered)
            level = min(_HEADING_BASE + len(heading.group(1)) - 1, _HEADING_MAX)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        ordered = _ORDERED_RE.match(line)
        bullet = _BULLET_RE.match(line)
        if ordered or bullet:
            _flush_paragraph(paragraph, out)
            is_ordered = ordered is not None
            # A list that switches marker style starts a new list rather than
            # silently mixing the two.
            if list_items and is_ordered != list_ordered:
                _flush_list(list_items, out, list_ordered)
            list_ordered = is_ordered
            list_items.append((ordered or bullet).group(1).strip())
            continue

        _flush_list(list_items, out, list_ordered)
        paragraph.append(line.strip())

    _flush_paragraph(paragraph, out)
    _flush_list(list_items, out, list_ordered)

    return Markup("".join(out))
