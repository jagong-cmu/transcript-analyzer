"""Sync orchestrator: pull new transcripts -> insights -> Obsidian note -> index.

Idempotent: each (source, native_id) is tracked with a content hash in sync_state,
so re-running only reprocesses changed transcripts.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Iterable, Optional

from .config import Config, load_config
from .connectors import pocket
from .db import get_conn, get_sync_hash, record_sync
from .models import Transcript
from .obsidian import writer
from .pipeline.categorize import Taxonomy
from .pipeline.indexer import index_note
from .pipeline.insights import extract_insight
from .pipeline.llm import LLM


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_source(cfg: Config, source: str, limit: Optional[int]) -> Iterable[Transcript]:
    if source == "pocket":
        yield from _limited(pocket.iter_transcripts(cfg), limit)
    elif source == "granola":
        from .connectors import granola  # imported lazily (needs token)

        yield from granola.iter_transcripts(cfg, limit=limit)
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


def process_transcript(
    cfg: Config,
    transcript: Transcript,
    llm: LLM,
    taxonomy: Taxonomy,
    *,
    dry_run: bool = False,
) -> dict:
    insight = extract_insight(transcript, cfg, llm=llm, taxonomy=taxonomy)
    result = {
        "id": transcript.id,
        "title": transcript.title,
        "source": transcript.source,
        "category": insight.category,
        "note_path": None,
    }
    if dry_run:
        return result
    note_path = writer.write_note(cfg, transcript, insight)
    result["note_path"] = str(note_path)
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
    taxonomy = Taxonomy(cfg, llm)

    health = llm.health()
    if not health["ok"]:
        msg = health.get("error") or f"missing models: {health.get('missing')}"
        print(f"[sync] WARNING: Ollama not ready ({msg}).", file=sys.stderr)

    processed, skipped, errors = [], 0, []
    for source in sources:
        if verbose:
            print(f"[sync] source: {source}")
        try:
            for t in _iter_source(cfg, source, limit):
                if not force and not dry_run:
                    with get_conn(cfg.db_path) as conn:
                        prev = get_sync_hash(conn, t.source, t.native_id)
                    if prev == t.hash:
                        skipped += 1
                        continue
                try:
                    res = process_transcript(cfg, t, llm, taxonomy, dry_run=dry_run)
                    processed.append(res)
                    if verbose:
                        print(f"  + [{res['category']}] {res['title']}")
                except Exception as e:  # noqa: BLE001 - one bad transcript shouldn't stop sync
                    errors.append({"id": t.id, "title": t.title, "error": str(e)})
                    print(f"  ! error on {t.title}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - a whole source failing (e.g. Granola auth)
            errors.append({"source": source, "error": str(e)})
            print(f"[sync] source {source} failed: {e}", file=sys.stderr)

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
