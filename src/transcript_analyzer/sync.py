"""Sync orchestrator: pull new transcripts -> insights -> Obsidian note -> index.

Idempotent: each (source, native_id) is tracked with a content hash in sync_state,
so re-running only reprocesses changed transcripts.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Iterable, Optional

import os

from .config import Config, load_config
from .connectors import pocket
from .db import (
    get_conn,
    get_meta,
    get_sync_hash,
    get_sync_note_path,
    record_sync,
    set_meta,
)
from .models import Transcript


def _high_water_key(source: str) -> str:
    return f"{source}_last_created_at"
from .obsidian import writer
from .pipeline.indexer import index_note
from .pipeline.insights import extract_insight
from .pipeline.llm import LLM


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_source(
    cfg: Config,
    source: str,
    limit: Optional[int],
    created_after: Optional[str] = None,
) -> Iterable[Transcript]:
    if source == "pocket":
        if cfg.pocket.api_enabled:
            from .connectors import pocket_api  # lazy (needs API key)

            yield from pocket_api.iter_transcripts(cfg, limit=limit, created_after=created_after)
        else:
            yield from _limited(pocket.iter_transcripts(cfg), limit)
    elif source == "granola":
        from .connectors import granola  # imported lazily (needs token)

        yield from granola.iter_transcripts(cfg, limit=limit, created_after=created_after)
    else:
        raise ValueError(f"unknown source: {source}")


def _limited(it: Iterable[Transcript], limit: Optional[int]) -> Iterable[Transcript]:
    if limit is None:
        yield from it
        return
    for i, x in enumerate(it):
        if i >= limit:
            return
        yield x


def _maybe_download_audio(cfg: Config, transcript: Transcript, insight) -> Optional[str]:
    """Download a Pocket recording's audio into the vault. Returns the filename to embed."""
    if transcript.source != "pocket" or not cfg.pocket.download_audio:
        return None
    from .connectors.pocket_api import PocketClient  # lazy (needs key)

    prospective = writer.note_path_for(cfg, transcript, insight)
    dest = writer.audio_path_for(cfg, prospective)
    try:
        with PocketClient(cfg) as pc:
            got = pc.download_audio(transcript.native_id, dest)
    except Exception:  # noqa: BLE001 - audio is best-effort, never fail the note
        return None
    return dest.name if got else None


def process_transcript(
    cfg: Config,
    transcript: Transcript,
    llm: LLM,
    *,
    dry_run: bool = False,
) -> dict:
    insight = extract_insight(transcript, cfg, llm=llm)
    result = {
        "id": transcript.id,
        "title": transcript.title,
        "source": transcript.source,
        "note_path": None,
    }
    if dry_run:
        return result

    # If this transcript was previously written under a different category/name,
    # remove the stale note file so we don't leave duplicates in the vault.
    with get_conn(cfg.db_path) as conn:
        prev_path = get_sync_note_path(conn, transcript.source, transcript.native_id)

    # Download the recording's audio into the vault (Pocket only) and embed it.
    audio_name = _maybe_download_audio(cfg, transcript, insight)

    note_path = writer.write_note(cfg, transcript, insight, audio_name=audio_name)
    result["note_path"] = str(note_path)

    if prev_path and prev_path != str(note_path) and os.path.exists(prev_path):
        try:
            os.remove(prev_path)
        except OSError:
            pass

    # Index the note we just wrote (parses it back → sqlite + embeddings).
    index_note(cfg, note_path, llm)
    with get_conn(cfg.db_path) as conn:
        record_sync(
            conn, transcript.source, transcript.native_id,
            transcript.hash, str(note_path), _now(),
        )
    return result


def sync(
    cfg: Optional[Config] = None,
    *,
    sources: Optional[list[str]] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = True,
) -> dict:
    cfg = cfg or load_config()
    sources = sources or _default_sources(cfg)
    llm = LLM(cfg)

    health = llm.health()
    if not health["ok"]:
        msg = health.get("error") or f"missing models: {health.get('missing')}"
        print(f"[sync] WARNING: Ollama not ready ({msg}).", file=sys.stderr)

    processed, skipped, errors = [], 0, []
    for source in sources:
        if verbose:
            print(f"[sync] source: {source}")

        # Incremental pull using a per-source created_at high-water mark (unless forced).
        created_after = None
        if not force:
            with get_conn(cfg.db_path) as conn:
                created_after = get_meta(conn, _high_water_key(source))
        max_sort = created_after or ""

        try:
            for t in _iter_source(cfg, source, limit, created_after):
                if t.remote_sort_key and t.remote_sort_key > max_sort:
                    max_sort = t.remote_sort_key
                if not force and not dry_run:
                    with get_conn(cfg.db_path) as conn:
                        prev = get_sync_hash(conn, t.source, t.native_id)
                    if prev == t.hash:
                        skipped += 1
                        continue
                try:
                    res = process_transcript(cfg, t, llm, dry_run=dry_run)
                    processed.append(res)
                    if verbose:
                        print(f"  + {res['title']}")
                except Exception as e:  # noqa: BLE001 - one bad transcript shouldn't stop sync
                    errors.append({"id": t.id, "title": t.title, "error": str(e)})
                    print(f"  ! error on {t.title}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - a whole source failing (e.g. Granola auth)
            errors.append({"source": source, "error": str(e)})
            print(f"[sync] source {source} failed: {e}", file=sys.stderr)

        # Advance the source's high-water mark after a successful, non-dry pass.
        if not dry_run and max_sort and max_sort != (created_after or ""):
            with get_conn(cfg.db_path) as conn:
                set_meta(conn, _high_water_key(source), max_sort)

    if not dry_run and processed:
        writer.rebuild_indexes(cfg)

    summary = {
        "processed": len(processed),
        "skipped": skipped,
        "errors": len(errors),
        "items": processed,
        "error_details": errors,
    }
    if verbose:
        print(f"[sync] done: {summary['processed']} processed, "
              f"{summary['skipped']} skipped, {summary['errors']} errors")
    return summary


def _default_sources(cfg: Config) -> list[str]:
    sources = ["pocket"]
    if cfg.granola.enabled:
        sources.append("granola")
    return sources


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Sync transcripts into Obsidian + index.")
    parser.add_argument("--source", choices=["pocket", "granola"], action="append",
                        help="Limit to a source (repeatable). Default: all configured.")
    parser.add_argument("--limit", type=int, default=None, help="Max transcripts per source.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract insights + print, but don't write notes or index.")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess even if unchanged.")
    args = parser.parse_args(argv)

    cfg = load_config()
    summary = sync(
        cfg,
        sources=args.source,
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
