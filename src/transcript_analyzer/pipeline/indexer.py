"""Build the derived SQLite index by parsing the vault insight notes.

The Obsidian notes are the source of truth. This reads them back (so
hand-edits are respected too) and upserts rows. No embeddings: retrieval is
agentic (Claude reads every summary and pulls whole notes on demand), so the
index only needs the parsed notes themselves.

FEEDBACK-LOOP GUARD: synthesis writes Digests/, People/, Studies/, Prep/, and
Categories/ into the same vault folder this indexer reads. Three defenses keep
the system from summarizing its own summaries in an unattended 20-minute loop:
  1. the glob is non-recursive (transcript notes are flat under the root),
  2. EXCLUDED_SUBDIRS is skipped explicitly even if that ever changes,
  3. parse_note() requires a transcript_id and rejects `synth: true` notes.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import frontmatter

from ..config import Config
from ..db import get_conn, upsert_transcript
from ..models import DEFAULT_KIND, Attendee, NoteRecord, coerce_kind
from ..obsidian.writer import (
    STUDY_SUBDIR,
    heading_level,
    is_section_end,
    is_section_start,
    opens_section,
    parse_action_items,
    transcript_bounds,
)
from ..titles import clean_headline, compose_display_title, headline_from_summary

# Re-exported: `heading_level`, `opens_section`, `is_section_start` and
# `is_section_end` are the writer's definitions, imported here so the reader
# and the writer cannot disagree about what a heading is (AGENTS.md).
__all__ = [
    "EXCLUDED_SUBDIRS",
    "index_note",
    "is_section_end",
    "is_section_start",
    "parse_note",
    "reindex_all",
]

_log = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# Subfolders of the insights folder that are never transcript notes. Every
# namespace synthesis writes into belongs here AND in writer.SYNTH_SUBDIRS;
# a namespace missing from this set is re-ingested as if it were a transcript.
EXCLUDED_SUBDIRS = frozenset(
    {"Categories", "Digests", "People", "Studies", "Prep", "Attachments", STUDY_SUBDIR}
)


def _strip_wikilink(s: str) -> str:
    m = _WIKILINK_RE.search(s)
    return m.group(1).strip() if m else s.strip()


def _extract_transcript(body: str) -> str:
    """Pull the transcript text out of the '## Transcript' callout block.

    Where the callout ends is `writer.transcript_bounds` — the one definition,
    shared with the writer that emits it and the timestamp backfill that
    rewrites it. Reading past that run would fold a callout the vault owner
    appended below the transcript into transcript_text, publishing it in the
    dashboard and the RAG corpus.
    """
    lines = body.splitlines()
    bounds = transcript_bounds(lines)
    if bounds is None:
        return ""
    start, end = bounds
    out: list[str] = []
    for ln in lines[start + 1: end + 1]:
        if not ln.startswith(">"):
            continue
        # skip the "[!note]- ..." callout header line
        if ln.lstrip(">").strip().startswith("[!"):
            continue
        out.append(ln.lstrip(">")[1:] if ln.startswith("> ") else ln.lstrip(">"))
    return "\n".join(out).strip()


def _extract_summary(body: str) -> str:
    """Body text under '## Summary', up to the next section at that level.

    Both boundaries go through the same heading predicate, so they cannot
    disagree about the same line (see AGENTS.md: add call sites, not second
    definitions).
    """
    lines = body.splitlines()
    out: list[str] = []
    in_section = False
    for ln in lines:
        if is_section_start(ln, "## summary"):
            in_section = True
            continue
        if in_section:
            if is_section_end(ln, "## summary"):
                break
            out.append(ln)
    return "\n".join(out).strip()


# The '## Action Items' checkbox scan is `writer.parse_action_items` — one
# definition, because the writer has to read the same ticks back when it
# regenerates a note and must not reopen a commitment the owner closed.
_extract_action_items = parse_action_items


def _parse_attendees(meta: dict) -> list[Attendee]:
    out: list[Attendee] = []
    for a in meta.get("attendees") or []:
        if isinstance(a, dict):
            out.append(
                Attendee(name=str(a.get("name") or ""), email=str(a.get("email") or ""))
            )
        elif isinstance(a, str) and a.strip():
            s = a.strip()
            out.append(Attendee(name="", email=s) if "@" in s else Attendee(name=s))
    return out


def parse_note(path: Path) -> Optional[NoteRecord]:
    """Parse one vault note into a record, or None if it is not indexable.

    Fails soft, and loudly: reindex_all walks every note in a bare loop, so one
    hand-edited note — unloadable frontmatter, or a field whose shape surprises
    us (`people: 42`) — must cost that note alone, not the whole vault index.
    The note still disappears from the index until it is fixed, so the reason is
    logged rather than swallowed.
    """
    try:
        return _parse_note(path)
    except Exception:  # noqa: BLE001
        _log.warning("skipping unparseable note: %s", path, exc_info=True)
        return None


def _parse_note(path: Path) -> Optional[NoteRecord]:
    post = frontmatter.load(str(path))
    meta = post.metadata
    tid = meta.get("transcript_id")
    if not tid or meta.get("synth"):
        # Not a transcript note (or a synthesis output) — never index it.
        return None
    people = [_strip_wikilink(str(p)) for p in (meta.get("people") or [])]
    topics = [str(t) for t in (meta.get("topics") or [])]
    fm_action_items = [str(a) for a in (meta.get("action_items") or [])]
    date_val = meta.get("date")
    date_str = date_val.isoformat() if hasattr(date_val, "isoformat") else str(date_val)

    body_items = _extract_action_items(post.content)
    if body_items:
        action_items = [t for t, _done in body_items]
        open_items = [t for t, done in body_items if not done]
    else:
        action_items = fm_action_items
        open_items = fm_action_items

    # The body's '## Summary' is the LONG summary the reader gets; the short
    # retrieval abstract lives in frontmatter. A note written before that split
    # has no `abstract:` — its body summary was already short, so it becomes
    # both, and the corpus every Ask question carries does not silently grow.
    detailed = _extract_summary(post.content)
    abstract = " ".join(str(meta.get("abstract") or "").split()) or _abstract_from(detailed)
    headline = clean_headline(str(meta.get("headline") or ""))
    if not headline:
        # Legacy notes: prefer H1 with date stripped, else first summary sentence.
        headline = clean_headline(_extract_h1(post.content)) or headline_from_summary(
            abstract, fallback=path.stem
        )
    display_title = _display_title(headline, date_str)

    return NoteRecord(
        transcript_id=str(tid),
        source=str(meta.get("source", "unknown")),
        title=display_title,
        date=date_str,
        category="",  # categories are tracked separately (note_categories), not in note frontmatter
        people=people,
        topics=topics,
        action_items=action_items,
        open_action_items=open_items,
        attendees=_parse_attendees(meta),
        summary=abstract,
        detailed_summary=detailed,
        kind=coerce_kind(str(meta.get("kind") or DEFAULT_KIND)),
        course_code=str(meta.get("course_code") or "").strip(),
        course_name=str(meta.get("course_name") or "").strip(),
        note_path=str(path.resolve()),
        transcript_text=_extract_transcript(post.content),
    )


# An abstract standing in for a legacy note is bounded for the same reason the
# real one is: this is the field Ask sends for every conversation, on every
# question. A long body summary contributes its opening paragraph, not itself.
_ABSTRACT_FALLBACK_CHARS = 900


def _abstract_from(detailed: str) -> str:
    """The retrieval abstract for a note that has no `abstract:` in frontmatter."""
    for block in str(detailed or "").split("\n\n"):
        para = " ".join(block.split())
        if para and not para.startswith("#"):
            return para[:_ABSTRACT_FALLBACK_CHARS].rstrip()
    return " ".join(str(detailed or "").split())[:_ABSTRACT_FALLBACK_CHARS].rstrip()


def _display_title(headline: str, date_str: str) -> str:
    """Compose "{headline}, July 26th, 2026", tolerating a junk `date:`.

    A note with a missing or malformed date must still index: parse_note is
    called in a bare loop by reindex_all, so raising here would take the whole
    vault index down over one bad note.
    """
    try:
        return compose_display_title(headline, date_str) if date_str else headline
    except ValueError:
        return headline


def _extract_h1(body: str) -> str:
    for ln in body.splitlines():
        if ln.startswith("# "):
            return ln[2:].strip()
    return ""


def _iter_note_paths(cfg: Config):
    root = cfg.vault.insights_path
    if not root.exists():
        return
    hub = f"{cfg.vault.insights_folder}.md"
    # Transcript notes are flat under root; the hub and every synthesis /
    # attachment subfolder are excluded (see the feedback-loop guard above).
    for note in sorted(root.glob("*.md")):
        if note.name == hub:
            continue
        if note.parent.name in EXCLUDED_SUBDIRS:
            continue
        yield note


def index_note(cfg: Config, path: Path) -> Optional[NoteRecord]:
    """Index a single note into the DB. Returns the record."""
    if path.parent.name in EXCLUDED_SUBDIRS:
        return None
    rec = parse_note(path)
    if rec is None:
        return None
    with get_conn(cfg.db_path) as conn:
        upsert_transcript(conn, rec)
    return rec


def reindex_all(cfg: Config) -> int:
    """Rebuild the index from every note in the vault. Returns count indexed."""
    count = 0
    for path in _iter_note_paths(cfg):
        if index_note(cfg, path) is not None:
            count += 1
    return count
