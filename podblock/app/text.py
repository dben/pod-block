"""Text from feeds, made safe to show: show notes as plain text, and links that are only ever web links."""

from __future__ import annotations

import re
from html.parser import HTMLParser


class _Text(HTMLParser):
    BREAKS = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Show notes as plain text, one paragraph per line. Safe to show anywhere."""
    if not html:
        return ""
    for _ in range(2):  # some feeds escape their HTML twice
        parser = _Text()
        parser.feed(html)
        parser.close()
        html = "".join(parser.parts)
        if not re.search(r"<(p|br|a|div|span|li)\b", html, re.I):
            break
    lines = (re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in html.split("\n"))
    return "\n".join(line for line in lines if line)


def web_url(url: str | None) -> str | None:
    """A link from a feed, if it is an http(s) one; anything else (javascript:, data:, a file) is dropped."""
    url = (url or "").strip()
    return url if url.split(":", 1)[0].lower() in {"http", "https"} else None
