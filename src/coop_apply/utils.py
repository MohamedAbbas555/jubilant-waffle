from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """Convert (possibly entity-escaped) HTML into readable plain text."""
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)  # Greenhouse double-escapes its content field
    parser = _TextExtractor()
    parser.feed(raw)
    text = html.unescape("".join(parser.parts))
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len].rstrip("-") or "item"


def short_hash(*parts: str, length: int = 10) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:length]


def file_hash(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def contains_any(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"\b{re.escape(n.lower())}\b", low) for n in needles if n)


_DEADLINE_FORMATS = [
    "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d, %Y", "%B %d, %Y",
    "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d %b %Y", "%d %B %Y",
]


def parse_deadline(text: str) -> datetime | None:
    """'Oct 21, 2026 9:00 AM', 'October 21, 2026', '2026-10-21'... -> datetime, else None."""
    if not text:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", t)  # "Oct 21st" -> "Oct 21"
    t = re.sub(r"\s*\(.*?\)\s*$", "", t)  # trailing "(EST)" etc.
    t = re.sub(r"\s+(EST|EDT|ET|PST|PDT|UTC)$", "", t, flags=re.I)
    for fmt in _DEADLINE_FORMATS:
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def deadline_passed(text: str, now: datetime | None = None) -> bool:
    d = parse_deadline(text)
    if d is None:
        return False
    if d.hour == 0 and d.minute == 0:  # date only: open until the end of that day
        d = d.replace(hour=23, minute=59)
    return d < (now or datetime.now())


def fetch_page_text(url: str, timeout: float = 30) -> str:
    """Download a web page and return its readable text ('' if it can't be read)."""
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(3_000_000)
        charset = resp.headers.get_content_charset() or "utf-8"
    return html_to_text(raw.decode(charset, errors="replace"))
