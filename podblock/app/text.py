"""Text: from feeds, made safe to show (show notes as plain text, links that are only ever web links), and
counts in words."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from ..feed import CONTENT, ITUNES


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


def plural(n: int, one: str, many: str | None = None) -> str:
    """A count and what it counts: "1 episode", "2 episodes", "3 downloaded ones"."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def web_url(url: str | None) -> str | None:
    """A link from a feed, if it is an http(s) one; anything else (javascript:, data:, a file) is dropped."""
    url = (url or "").strip()
    return url if url.split(":", 1)[0].lower() in {"http", "https"} else None


def show_notes(item_xml: str | None) -> str:
    """An episode's full show notes as plain text, from the <item> we kept."""
    if not item_xml:
        return ""
    try:
        item = ET.fromstring(item_xml)
    except ET.ParseError:
        return ""
    for path in (f"{CONTENT}encoded", "description", f"{ITUNES}summary"):
        text = item.findtext(path)
        if text and text.strip():
            return html_to_text(text)
    return ""
