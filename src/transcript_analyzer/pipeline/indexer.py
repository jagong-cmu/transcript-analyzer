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
from ..models import Attendee, NoteRecord
from ..obsidian.writer import opens_section
from ..titles import clean_headline, compose_display_title, headline_from_summary

_log = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")

# Subfolders of the insights folder that are never transcript notes.
EXCLUDED_SUBDIRS = frozenset(
    {"Categories", "Digests", "People", "Studies", "Prep", "Attachments"}
)


def is_section_start(line: str, heading: str) -> bool:
    """Whether `line` opens the named section ('## transcript', lowercase).

    Section detection goes through writer.opens_section so the reader and the
    writer cannot disagree about what a heading is; see AGENTS.md.
    """
    return opens_section(line) and line.strip().lower() == heading


def _strip_wikilink(s: str) -> str:
    m = _WIKILINK_RE.search(s)
    return m.group(1).strip() if m else s.strip()


def _extract_transcript(body: str) -> str:
    """Pull the transcript text out of the '## Transcript' callout block.

    The section is the heading, an optional run of blank lines, and then the
    contiguous run of '>' lines that is the callout — the same grammar the
    writer emits and the timestamp backfill rewrites. Reading past the end of
    that run would fold a callout the vault owner appended below the transcript
    into transcript_text, publishing it in the dashboard and the RAG corpus.
    An interior blank transcript line is written as '> ', so it stays inside.
    """
    lines = body.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if is_section_start(ln, "## transcript")),
        None,
    )
    if start is None:
        return ""
    i = start + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    out: list[str] = []
    while i < len(lines) and lines[i].startswith(">"):
        ln = lines[i]
        i += 1
        # skip the "[!note]- ..." callout header line
        if ln.lstrip(">").strip().startswith("[!"):
            continue
        out.append(ln.lstrip(">")[1:] if ln.startswith("> ") else ln.lstrip(">"))
    return "\n".join(out).strip()


def _extract_summary(body: str) -> str:
    """Body text under '## Summary', up to the next heading.

    A section ENDS where writer.opens_section says a heading begins — the same
    predicate that finds the start, so the two boundaries cannot disagree about
    the same line (see AGENTS.md: add call sites, not second definitions).
    """
    lines = body.splitlines()
    out: list[str] = []
    in_section = False
    for ln in lines:
        if is_section_start(ln, "## summary"):
            in_section = True
            continue
        if in_section:
            if opens_section(ln):
                break
            out.append(ln)
    return "\n".join(out).strip()


def _extract_action_items(body: str) -> list[tuple[str, bool]]:
    """(text, done) pairs from the '## Action Items' checkbox list. The note
    is the source of truth: ticking a box in Obsidian closes the commitment.
    The section ends at the next `opens_section` heading, as above."""
    lines = body.splitlines()
    out: list[tuple[str, bool]] = []
    in_section = False
    for ln in lines:
        if is_section_start(ln, "## action items"):
            in_section = True
            continue
        if in_section:
            if opens_section(ln):
                break
            m = _CHECKBOX_RE.match(ln)
            if m:
                out.append((m.group(2), m.group(1).lower() == "x"))
    return out


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

    summary = _extract_summary(post.content)
    headline = clean_headline(str(meta.get("headline") or ""))
    if not headline:
        # Legacy notes: prefer H1 with date stripped, else first summary sentence.
        headline = clean_headline(_extract_h1(post.content)) or headline_from_summary(
            summary, fallback=path.stem
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
        summary=summary,
        note_path=str(path.resolve()),
        transcript_text=_extract_transcript(post.content),
    )


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
