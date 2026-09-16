"""Tests for Telegram HTML rendering of chat summaries."""

import pytest

from src.tg_render import render_html, render_plain, split_parts, strip_html

LINKS = {12: "https://t.me/c/42/12", 15: "https://t.me/chan/15?x=1&y=2"}


@pytest.mark.unit
def test_citations_become_link_icons():
    out = render_html("- Released v2 [#12]", LINKS)
    assert out == '• Released v2 <a href="https://t.me/c/42/12">🔗</a>'


@pytest.mark.unit
def test_multiple_and_parenthesized_citations():
    out = render_html("Point (#12, #15)", LINKS)
    assert out.count("<a ") == 2
    assert 'href="https://t.me/chan/15?x=1&amp;y=2"' in out


@pytest.mark.unit
def test_unknown_citations_are_dropped():
    assert render_html("Point [#99]", LINKS) == "Point"
    assert render_html("Point [#12, #99]", LINKS).count("<a ") == 1


@pytest.mark.unit
def test_citations_dropped_when_chat_has_no_links():
    assert render_html("A [#12]\nB [#15]", {}) == "A\nB"


@pytest.mark.unit
def test_citation_at_line_start_keeps_line_break():
    assert render_html("A\n[#12] B", LINKS).startswith("A\n")


@pytest.mark.unit
def test_untrusted_text_is_escaped():
    out = render_html("<script>x</script> & a_b *c*", {})
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    assert "a_b *c*" in out


@pytest.mark.unit
def test_bold_headings_and_bullets():
    out = render_html("## Title\n**Topic** text\n- one\n  * two", {})
    assert out.splitlines() == ["<b>Title</b>", "<b>Topic</b> text", "• one", "  • two"]


@pytest.mark.unit
def test_markdown_links_kept_and_escaped():
    out = render_html('See [docs <v2>](https://example.com/a?b=1&c="2")', {})
    assert out == 'See <a href="https://example.com/a?b=1&amp;c=&quot;2&quot;">docs &lt;v2&gt;</a>'


@pytest.mark.unit
def test_non_http_links_are_not_turned_into_anchors():
    out = render_html("[x](javascript:alert(1))", {})
    assert "<a" not in out


@pytest.mark.unit
def test_render_plain_uses_urls():
    assert render_plain("A [#12, #15]", LINKS) == (
        "A https://t.me/c/42/12 https://t.me/chan/15?x=1&y=2"
    )
    assert render_plain("A [#1]", {}) == "A"


@pytest.mark.unit
def test_strip_html_round_trip():
    html_text = render_html("**A** & [#12]", LINKS)
    assert strip_html(html_text) == "A & 🔗"


@pytest.mark.unit
def test_split_parts_respects_limit_and_drops_blanks():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
    parts = split_parts(text, limit=500)
    assert len(parts) > 1
    assert all(len(p) <= 500 for p in parts)
    assert split_parts("   \n  ") == []
