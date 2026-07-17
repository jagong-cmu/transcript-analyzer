"""Build the derived SQLite index by parsing the vault insight notes.

The Obsidian notes are the source of truth. This reads them back (so
hand-edits are respected too) and upserts rows. No embeddings: retrieval is
agentic (Claude reads every summary and pulls whole notes on demand), so the
index only needs the parsed notes themselves.

FEEDBACK-LOOP GUARD: synthesis writes Digests/, People/, Studies/, and Prep/
into the same vault folder this indexer reads. Three defenses keep the system
from summarizing its own summaries in an unattended 20-minute loop:
  1. the glob is non-recursive (transcript notes are flat under the root),
  2. EXCLUDED_SUBDIRS is skipped explicitly even if that ever changes,
  3. parse_note() requires a transcript_id and rejects `synth: true` notes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import frontmatter

from ..config import Config
from ..db import get_conn, upsert_transcript
from ..models import Attendee, NoteRecord

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")

# Subfolders of the insights folder that are never transcript notes.
EXCLUDED_SUBDIRS = frozenset(
    {"Categories", "Digests", "People", "Studies", "Prep", "Attachments"}
)


def _strip_wikilink(s: str) -> str:
    m = _WIKILINK_RE.search(s)
    return m.group(1).strip() if m else s.strip()


def _extract_transcript(body: str) -> str:
    """Pull the transcript text out of the '## Transcript' callout block."""
    lines = body.splitlines()
    out: list[str] = []
    in_section = False
    for ln in lines:
        if ln.strip().lower() == "## transcript":
            in_section = True
            continue
        if in_section:
            if ln.startswith(">"):
                # strip callout marker; skip the "[!note]- ..." header line
                stripped = ln.lstrip(">").strip()
                if stripped.startswith("[!"):
                    continue
                out.append(ln.lstrip(">")[1:] if ln.startswith("> ") else ln.lstrip(">"))
            elif ln.strip() == "":
                out.append("")
            else:
                break
    return "\n".join(out).strip()


def _extract_summary(body: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    in_section = False
    for ln in lines:
        if ln.strip().lower() == "## summary":
            in_section = True
            continue
        if in_section:
            if ln.startswith("## "):
                break
            out.append(ln)
    return "\n".join(out).strip()


def _extract_action_items(body: str) -> list[tuple[str, bool]]:
    """(text, done) pairs from the '## Action Items' checkbox list. The note
    is the source of truth: ticking a box in Obsidian closes the commitment."""
    lines = body.splitlines()
    out: list[tuple[str, bool]] = []
    in_section = False
    for ln in lines:
        if ln.strip().lower() == "## action items":
            in_section = True
            continue
        if in_section:
            if ln.startswith("## "):
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
    try:
        post = frontmatter.load(str(path))
    except Exception:  # noqa: BLE001
        return None
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

    return NoteRecord(
        transcript_id=str(tid),
        source=str(meta.get("source", "unknown")),
        title=path.stem,
        date=date_str,
        category="",  # categories are tracked separately (note_categories), not in note frontmatter
        people=people,
        topics=topics,
        action_items=action_items,
        open_action_items=open_items,
        attendees=_parse_attendees(meta),
        summary=_extract_summary(post.content),
        note_path=str(path.resolve()),
        transcript_text=_extract_transcript(post.content),
    )


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
