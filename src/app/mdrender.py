"""Render assistant replies as Markdown, safely.

The model answers in Markdown (set tables, bold weights, lists), so the chat would
otherwise show raw `**text**` and pipe-tables.

Model output is untrusted (anyone with the passphrase can steer it, and a logged
message could carry an injection), so this NEVER passes HTML through: the text is
HTML-escaped *first* and only then converted, which turns any `<script>` in a reply
into visible text rather than markup. Keep that order — it is the whole safety
property.
"""

from __future__ import annotations

import html

import markdown as _markdown

# tables: the model's set breakdowns. nl2br: chat text uses single newlines as
# line breaks, which plain Markdown would otherwise join into one paragraph.
_EXTENSIONS = ["tables", "nl2br", "sane_lists"]


def render_markdown(text: str) -> str:
    """Escaped-then-converted Markdown → HTML safe to drop into a chat bubble."""
    if not text:
        return ""
    return _markdown.markdown(html.escape(text), extensions=_EXTENSIONS, output_format="html")
