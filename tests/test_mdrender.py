"""Markdown rendering of assistant replies (app/mdrender.py) — formatting works,
and untrusted model output can never inject HTML."""

from __future__ import annotations

from app.mdrender import render_markdown


def test_renders_markdown_formatting():
    out = render_markdown("**Back Squat** 4x7\n\n| Exercise | Total |\n|---|---|\n| Squat | 165 |")
    assert "<strong>Back Squat</strong>" in out
    assert "<table>" in out and "<td>165</td>" in out
    # chat text uses single newlines as line breaks (nl2br)
    assert "<br" in render_markdown("line one\nline two")


def test_html_in_model_output_is_escaped_not_executed():
    # The model is untrusted (prompt injection): HTML must come out as visible
    # text, never as markup. Escape-then-convert is the safety property.
    out = render_markdown('<script>alert("x")</script> and <img src=x onerror=y>')
    assert "<script>" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "&lt;img src=x onerror=y&gt;" in out
