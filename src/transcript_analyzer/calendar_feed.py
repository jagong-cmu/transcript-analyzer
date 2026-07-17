"""Read-only calendar feed for meeting prep.

Fetches the secret ICS URL from [calendar] ics_url (Google Calendar:
Settings -> your calendar -> "Secret address in iCal format") and expands
recurring events for a single day. Attendee emails are the join key back to
conversation history — the same key granola.py persists at ingest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import httpx

from .config import Config


@dataclass
class CalEvent:
    title: str
    start: str  # ISO datetime or date
    attendee_emails: list[str] = field(default_factory=list)
    attendee_names: list[str] = field(default_factory=list)


def _attendee_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def events_for_day(cfg: Config, day: date) -> list[CalEvent]:
    import icalendar
    import recurring_ical_events

    r = httpx.get(cfg.calendar.ics_url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    cal = icalendar.Calendar.from_ical(r.content)

    out: list[CalEvent] = []
    for ev in recurring_ical_events.of(cal).at(day):
        title = str(ev.get("SUMMARY") or "Untitled event").strip()
        start = ev.get("DTSTART")
        start_iso = start.dt.isoformat() if start is not None else day.isoformat()
        emails: list[str] = []
        names: list[str] = []
        for att in _attendee_list(ev.get("ATTENDEE")):
            addr = str(att)
            if addr.lower().startswith("mailto:"):
                emails.append(addr[7:].strip().lower())
            params = getattr(att, "params", {}) or {}
            cn = str(params.get("CN", "")).strip()
            if cn and "@" not in cn:
                names.append(cn)
        # The organizer is an attendee too on most feeds.
        org = ev.get("ORGANIZER")
        if org is not None:
            addr = str(org)
            if addr.lower().startswith("mailto:"):
                emails.append(addr[7:].strip().lower())
        out.append(
            CalEvent(
                title=title,
                start=start_iso,
                attendee_emails=sorted(set(emails)),
                attendee_names=sorted(set(names)),
            )
        )
    out.sort(key=lambda e: e.start)
    return out
