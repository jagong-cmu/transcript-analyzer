"""Human-readable conversation titles.

Display form: ``{headline}, July 26th, 2026``
Filenames stay date-prefixed + slugified headline for vault stability.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Union

DateLike = Union[date, datetime, str]

_LEADING_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+")
_TRAILING_LONG_DATE = re.compile(
    r",?\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:st|nd|rd|th),\s+\d{4}\s*$",
    re.IGNORECASE,
)


def ordinal_day(day: int) -> str:
    if 11 <= day <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def parse_date(when: DateLike) -> date:
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, date):
        return when
    return date.fromisoformat(str(when).strip()[:10])


def format_long_date(when: DateLike) -> str:
    """e.g. July 26th, 2026"""
    d = parse_date(when)
    return f"{d.strftime('%B')} {ordinal_day(d.day)}, {d.year}"


def clean_headline(headline: str) -> str:
    text = " ".join((headline or "").split()).strip().strip("\"'")
    text = _LEADING_ISO_DATE.sub("", text)
    text = _TRAILING_LONG_DATE.sub("", text)
    return text.rstrip(".,;:").strip()


def compose_display_title(headline: str, when: DateLike) -> str:
    """One-liner description, then the long date."""
    h = clean_headline(headline)
    if not h:
        h = "Untitled conversation"
    return f"{h}, {format_long_date(when)}"


def headline_from_summary(summary: str, fallback: str = "") -> str:
    """Cheap non-LLM headline: first sentence of the summary, shortened."""
    text = " ".join((summary or "").split()).strip()
    if not text:
        return clean_headline(fallback) or "Untitled conversation"
    # Split on sentence end; keep it short.
    for sep in (". ", "! ", "? "):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    text = text.rstrip(".,;:")
    # Common summary openers that make weak titles.
    for prefix in (
        "The conversation was about ",
        "The conversation discusses ",
        "The conversation focused on ",
        "The conversation revolves around ",
        "The conversation between ",
        "The conversation transcript is empty, containing only ",
        "The conversation ",
        "The meeting focused on ",
        "The meeting covered ",
        "The call was about ",
        "The transcript indicates ",
        "The transcript starts with ",
        "A conversation between two individuals about ",
        "A brief conversation where ",
        "During this conversation, ",
        "This conversation ",
    ):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :].lstrip()
            # Capitalize first letter after stripping.
            if text:
                text = text[0].upper() + text[1:]
            break
    if len(text) > 90:
        cut = text[:87].rsplit(" ", 1)[0]
        text = cut.rstrip(".,;:") + "…"
    return clean_headline(text) or clean_headline(fallback) or "Untitled conversation"
