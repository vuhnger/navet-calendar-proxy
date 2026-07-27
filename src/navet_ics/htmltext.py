"""Convert the rich-text HTML stored on Navet events into readable plain text.

Calendar clients render DESCRIPTION as plain text, so raw HTML would leak markup
into the feed. This uses the stdlib parser rather than a regex so malformed or
hostile markup cannot produce surprising output.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section"})
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})
_LIST_ITEM_PREFIX = "- "

# Collapse runs of 3+ newlines down to a paragraph break.
_EXCESS_NEWLINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_INLINE_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "li":
            self._parts.append("\n" + _LIST_ITEM_PREFIX)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self._skip_depth:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._parts.append(_INLINE_WHITESPACE.sub(" ", data))

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str, *, max_length: int = 8000) -> str:
    """Return readable plain text for `html`, truncated to `max_length` characters."""
    if not html:
        return ""

    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Deliberately broad: malformed markup in one event must never break the
        # whole feed. Fall back to whatever was parsed before the failure.
        pass

    text = parser.text()
    text = _TRAILING_SPACE.sub("\n", text)
    text = _EXCESS_NEWLINES.sub("\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()

    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text
