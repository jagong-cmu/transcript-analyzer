"""Pocket AI connector.

Pocket AI writes recordings as markdown files into a folder inside the Obsidian
vault (default: "Pocket AI Recordings"). We read those files and normalize them
into Transcript objects. No API or credentials needed.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import frontmatter

from ..config import Config
from ..models import Transcript, stable_id

# We skip files our own pipeline generates (insight notes live elsewhere, but be safe).
_DATE_RE = re.compile(r"(\d{4})[-_/](\d{2})[-_/](\d{2})")


def _guess_date(post: frontmatter.Post, path: Path) -> date:
    for key in ("date", "created", "recorded", "recorded_at", "start"):
        val = post.get(key)
        if val:
            d = _coerce_date(val)
            if d:
                return d
    m = _DATE_RE.search(path.stem)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return date.fromtimestamp(path.stat().st_mtime)


def _coerce_date(val) -> date | None:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        m = _DATE_RE.search(val)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    return None


def _guess_participants(post: frontmatter.Post) -> list[str]:
    for key in ("participants", "people", "attendees", "speakers"):
        val = post.get(key)
        if isinstance(val, list):
            return [str(v).strip() for v in val if str(v).strip()]
        if isinstance(val, str) and val.strip():
            return [p.strip() for p in re.split(r"[,;]", val) if p.strip()]
    return []


def _guess_title(post: frontmatter.Post, path: Path) -> str:
    for key in ("title", "name"):
        val = post.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # First markdown heading, else filename.
    for line in post.content.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def iter_transcripts(cfg: Config) -> Iterator[Transcript]:
    folder = cfg.vault.path / cfg.pocket.folder
    if not folder.exists():
        return
    for path in sorted(folder.rglob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001 - a malformed file shouldn't kill the whole sync
            continue
        text = post.content.strip()
        if not text:
            continue
        native_id = str(path.resolve())
        yield Transcript(
            id=stable_id("pocket", native_id),
            source="pocket",
            native_id=native_id,
            title=_guess_title(post, path),
            date=_guess_date(post, path),
            participants=_guess_participants(post),
            text=text,
            source_ref=native_id,
        )
