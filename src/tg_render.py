"""
Turns the model's Markdown-ish summary into Telegram HTML.

HTML is used instead of Telegram's legacy Markdown because every piece of
untrusted text is escaped first, so underscores in usernames or stray asterisks
cannot break parsing. The only tags produced are <b> and <a>.
"""

import html
import re

from src.utils import split_message

TELEGRAM_PART_CHARS = 3500  # visible characters per message, below Telegram's 4096 limit
LINK_ICON = "🔗"

_REF_GROUP_RE = re.compile(r"[ \t]*[\[(][ \t]*(#\d+(?:[ \t]*[,;][ \t]*#?\d+)*)[ \t]*[\])]")
_REF_ID_RE = re.compile(r"\d+")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+", re.MULTILINE)
_TAG_RE = re.compile(r"<[^>]+>")
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def _attr(url: str) -> str:
    return html.escape(url, quote=True)


def render_html(text: str, links: dict[int, str]) -> str:
    """Render one summary part as Telegram HTML.

    ``[#id]`` / ``(#id, #id)`` citations become link icons for ids found in ``links``;
    citations of unknown ids (or in chats without message links) are dropped.
    """
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def replace_refs(match: re.Match) -> str:
        anchors = [
            f'<a href="{_attr(links[int(i)])}">{LINK_ICON}</a>'
            for i in _REF_ID_RE.findall(match.group(1))
            if int(i) in links
        ]
        return keep(" " + "".join(anchors)) if anchors else ""

    def replace_link(match: re.Match) -> str:
        label = html.escape(match.group(1))
        return keep(f'<a href="{_attr(match.group(2))}">{label}</a>')

    out = _REF_GROUP_RE.sub(replace_refs, text)
    out = _MD_LINK_RE.sub(replace_link, out)
    out = html.escape(out, quote=False)
    out = _HEADING_RE.sub(r"<b>\1</b>", out)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _BULLET_RE.sub(r"\1• ", out)
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], out)


def render_plain(text: str, links: dict[int, str]) -> str:
    """Render a summary for a terminal: citations become bare URLs."""

    def replace_refs(match: re.Match) -> str:
        urls = [links[int(i)] for i in _REF_ID_RE.findall(match.group(1)) if int(i) in links]
        return (" " + " ".join(urls)) if urls else ""

    return _REF_GROUP_RE.sub(replace_refs, text)


def strip_html(text: str) -> str:
    """Plain-text fallback for when Telegram rejects the HTML."""
    return html.unescape(_TAG_RE.sub("", text))


def split_parts(text: str, limit: int = TELEGRAM_PART_CHARS) -> list[str]:
    """Split Markdown source into Telegram-sized parts on line boundaries.

    Split before rendering: tags and escapes do not count toward Telegram's limit.
    """
    return [p for p in split_message(text.strip(), max_length=limit) if p.strip()]
