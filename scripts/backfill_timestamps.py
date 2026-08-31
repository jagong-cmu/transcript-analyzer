#!/usr/bin/env python3
"""Backfill [MM:SS] timestamps into existing insight notes from the source APIs.

Rewrites only the ## Transcript section (no new insight LLM calls). Updates
sync_state content hashes so the next sync won't reprocess these as "changed".

Pocket notes with downloaded audio get clickable seek in the dashboard.
Granola timestamps are relative to the start of the note (no audio file).

Usage:
    python scripts/backfill_timestamps.py
    python scripts/backfill_timestamps.py --source pocket --limit 20
    python scripts/backfill_timestamps.py --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frontmatter  # noqa: E402

from transcript_analyzer.config import load_config  # noqa: E402
from transcript_analyzer.db import get_conn, record_sync  # noqa: E402
from transcript_analyzer.obsidian.writer import _quote_block  # noqa: E402
from transcript_analyzer.pipeline.indexer import index_note  # noqa: E402


_HEADING_RE = re.compile(r"## Transcript\s*")


def _replace_transcript_section(content: str, timed_text: str) -> str:
    """Rewrite only the '## Transcript' callout, leaving the rest of the note.

    The vault is the source of truth and hand-edits are respected, so anything
    the owner appended below the callout must survive this migration. The
    callout ends at the first line that is neither blank nor a '>' line — the
    same stop rule indexer._extract_transcript uses.
    """
    block = "## Transcript\n" + _quote_block(timed_text)
    lines = content.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if _HEADING_RE.fullmatch(ln)), None
    )
    if start is None:
        return content.rstrip() + "\n\n" + block + "\n"

    end = start  # index of the last line belonging to the callout
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.startswith(">"):
            end = i
        elif ln.strip():
            break
    # Transcript text is arbitrary content, so it must never be used as a
    # re.sub template — a stray backslash raises "bad escape" (or worse,
    # silently expands a group reference). Splicing the lines is literal.
    rebuilt = lines[:start] + block.splitlines() + lines[end + 1:]
    return "\n".join(rebuilt).rstrip("\n") + "\n"


def _already_timed(text: str) -> bool:
    return bool(re.search(r"^\[\d{1,2}:\d{2}", text or "", re.MULTILINE))


def backfill(*, source: str | None, limit: int | None, dry_run: bool, force: bool) -> dict:
    cfg = load_config()
    updated = skipped = errors = no_timing = 0

    with get_conn(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT transcript_id, source, note_path FROM transcripts ORDER BY date DESC"
        ).fetchall()

    pocket_client = None
    granola_client = None

    for row in rows:
        src = row["source"]
        if source and src != source:
            continue
        if limit is not None and updated >= limit:
            break
        note_path = Path(row["note_path"] or "")
        if not note_path.exists():
            skipped += 1
            continue
        try:
            post = frontmatter.load(str(note_path))
        except Exception as e:  # noqa: BLE001
            print(f"  ! parse {note_path.name}: {e}")
            errors += 1
            continue
        native = None
        # Prefer sync_state native_id
        with get_conn(cfg.db_path) as conn:
            srow = conn.execute(
                "SELECT native_id, source FROM sync_state WHERE note_path = ?",
                (str(note_path.resolve()),),
            ).fetchone()
            if not srow:
                # try by matching any sync row that points near this file
                srow = conn.execute(
                    "SELECT native_id, source FROM sync_state WHERE note_path LIKE ?",
                    (f"%{note_path.name}",),
                ).fetchone()
            if srow:
                native = srow["native_id"]
                src = srow["source"] or src

        if not native:
            print(f"  · skip {note_path.name}: no sync_state native_id")
            skipped += 1
            continue

        # Peek existing transcript for skip
        from transcript_analyzer.pipeline.indexer import _extract_transcript

        existing = _extract_transcript(post.content)
        if _already_timed(existing) and not force:
            skipped += 1
            continue

        try:
            if src == "pocket":
                if not cfg.pocket.api_enabled:
                    skipped += 1
                    continue
                if pocket_client is None:
                    from transcript_analyzer.connectors.pocket_api import PocketClient

                    pocket_client = PocketClient(cfg)
                detail = pocket_client.get_recording(native)
                t = pocket_client.to_transcript(detail)
            elif src == "granola":
                if not cfg.granola.enabled:
                    skipped += 1
                    continue
                if granola_client is None:
                    from transcript_analyzer.connectors.granola import GranolaClient

                    granola_client = GranolaClient(cfg)
                detail = granola_client.get_note(native)
                t = granola_client.to_transcript(detail)
            else:
                skipped += 1
                continue
        except Exception as e:  # noqa: BLE001
            print(f"  ! fetch {note_path.name}: {e}")
            errors += 1
            continue

        if t is None or not t.segments or all(s.start_sec is None for s in t.segments):
            print(f"  · no timing on {note_path.name}")
            no_timing += 1
            continue

        print(f"  {note_path.name}")
        print(f"    → {len(t.segments)} timed segments")
        if dry_run:
            updated += 1
            continue

        post.content = _replace_transcript_section(post.content, t.text)
        dumped = frontmatter.dumps(post)
        note_path.write_text(dumped if dumped.endswith("\n") else dumped + "\n", encoding="utf-8")
        index_note(cfg, note_path)
        from datetime import datetime, timezone

        with get_conn(cfg.db_path) as conn:
            record_sync(
                conn,
                src,
                native,
                t.hash,
                str(note_path.resolve()),
                datetime.now(timezone.utc).isoformat(),
            )
        updated += 1

    if pocket_client is not None:
        try:
            pocket_client.close()
        except Exception:
            pass
    if granola_client is not None:
        try:
            granola_client.close()
        except Exception:
            pass

    summary = {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "no_timing": no_timing,
    }
    print(f"[backfill_timestamps] {summary}")
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", choices=["pocket", "granola"], default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Rewrite even if timestamps already present.")
    args = p.parse_args()
    backfill(source=args.source, limit=args.limit, dry_run=args.dry_run, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
