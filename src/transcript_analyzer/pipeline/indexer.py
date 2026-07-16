"""Build the derived SQLite + embedding index by parsing the vault insight notes.

The Obsidian notes are the source of truth. This reads them back (so hand-edits
are respected too), chunks the transcript, embeds each chunk, and upserts rows.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import frontmatter

from ..config import Config
from ..db import (
    delete_chunks,
    get_conn,
    insert_chunk,
    upsert_transcript,
)
from ..models import NoteRecord, content_hash
from .llm import LLM

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


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


def parse_note(path: Path) -> Optional[NoteRecord]:
    try:
        post = frontmatter.load(str(path))
    except Exception:  # noqa: BLE001
        return None
    meta = post.metadata
    tid = meta.get("transcript_id")
    if not tid:
        return None
    people = [_strip_wikilink(str(p)) for p in (meta.get("people") or [])]
    topics = [str(t) for t in (meta.get("topics") or [])]
    action_items = [str(a) for a in (meta.get("action_items") or [])]
    date_val = meta.get("date")
    date_str = date_val.isoformat() if hasattr(date_val, "isoformat") else str(date_val)

    return NoteRecord(
        transcript_id=str(tid),
        source=str(meta.get("source", "unknown")),
        title=path.stem,
        date=date_str,
        category=str(meta.get("category", "Uncategorized")),
        people=people,
        topics=topics,
        action_items=action_items,
        summary=_extract_summary(post.content),
        note_path=str(path.resolve()),
        transcript_text=_extract_transcript(post.content),
    )


def _chunk(text: str, size: int = 1600, overlap: int = 200) -> list[str]:
    """Character-based chunking (~400 tokens per chunk) with a small overlap."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def _iter_note_paths(cfg: Config):
    root = cfg.vault.insights_path
    if not root.exists():
        return
    hub = f"{cfg.vault.insights_folder}.md"
    for cat_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for note in sorted(cat_dir.glob("*.md")):
            yield note
    # MOC/hub notes at the top level are skipped (they have no transcript_id).


def index_note(cfg: Config, path: Path, llm: Optional[LLM] = None) -> Optional[NoteRecord]:
    """Index a single note into the DB (with embeddings). Returns the record."""
    rec = parse_note(path)
    if rec is None:
        return None
    llm = llm or LLM(cfg)
    with get_conn(cfg.db_path) as conn:
        upsert_transcript(conn, rec)
        delete_chunks(conn, rec.transcript_id)
        chunks = _chunk(rec.transcript_text) or _chunk(rec.summary)
        for i, ch in enumerate(chunks):
            try:
                emb = llm.embed_one(ch)
            except Exception:  # noqa: BLE001 - store text even if embedding fails
                emb = None
            insert_chunk(conn, rec.transcript_id, i, ch, emb)
    return rec


def reindex_all(cfg: Config, llm: Optional[LLM] = None) -> int:
    """Rebuild the index from every note in the vault. Returns count indexed."""
    llm = llm or LLM(cfg)
    count = 0
    for path in _iter_note_paths(cfg):
        if index_note(cfg, path, llm) is not None:
            count += 1
    return count
