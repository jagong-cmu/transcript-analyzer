#!/usr/bin/env python3
"""Retitle existing insight notes to descriptive one-liners + long dates.

Updates each note's `headline` frontmatter and H1 to:
  Pricing deck review with Angela, July 1st, 2026

By default uses Claude for accurate headlines. Pass --cheap to derive from
the existing summary (no API calls). Renames files (and matching audio) to
match the new headline slug, then reindexes.

Usage:
    python scripts/retitle_notes.py
    python scripts/retitle_notes.py --cheap
    python scripts/retitle_notes.py --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frontmatter  # noqa: E402
from slugify import slugify  # noqa: E402

from transcript_analyzer.config import load_config  # noqa: E402
from transcript_analyzer.db import canonical_note_path, get_conn  # noqa: E402
from transcript_analyzer.obsidian import writer  # noqa: E402
from transcript_analyzer.pipeline.indexer import (  # noqa: E402
    _extract_h1,
    _extract_summary,
    reindex_all,
)
from transcript_analyzer.pipeline.llm import LLM  # noqa: E402
from transcript_analyzer.titles import (  # noqa: E402
    clean_headline,
    compose_display_title,
    format_long_date,
    headline_from_summary,
    parse_date,
)

HEADLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Short specific one-line title, 5–12 words, no date.",
        }
    },
    "required": ["title"],
}

HEADLINE_SYSTEM = """You write short, specific titles for meeting transcripts.
Return JSON only. No dates in the title. Prefer concrete topics and people
over generic words like Meeting, Sync, or Call."""


def _set_h1(body: str, title: str) -> str:
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            lines[i] = f"# {title}"
            return "\n".join(lines)
    return f"# {title}\n\n{body}"


def _llm_headline(llm: LLM, *, old_title: str, summary: str, people: list, source: str) -> str:
    user = (
        f"Source title: {old_title}\n"
        f"Source: {source}\n"
        f"People: {', '.join(people) or '(unknown)'}\n"
        f"Summary:\n{summary or '(none)'}\n\n"
        "Write the title."
    )
    data = llm.chat_json(HEADLINE_SYSTEM, user, schema=HEADLINE_SCHEMA)
    return clean_headline(str(data.get("title") or ""))


def _target_path(cfg, when: date, headline: str, transcript_id: str, current: Path) -> Path:
    slug = slugify(headline, max_length=80) or "untitled"
    base = cfg.vault.insights_path / f"{when.isoformat()} {slug}.md"
    if base.resolve() == current.resolve():
        return current
    return writer.claim_note_path(base, transcript_id)


def _rename_with_audio(cfg, old: Path, new: Path, *, dry_run: bool) -> Path:
    if old.resolve() == new.resolve():
        return old
    if dry_run:
        print(f"  would rename: {old.name} → {new.name}")
        return new
    new.parent.mkdir(parents=True, exist_ok=True)
    old_audio = writer.audio_path_for(cfg, old)
    old.rename(new)
    new_audio = writer.move_audio_with_note(cfg, old, new)
    if new_audio is not None:
        # Fix embed reference inside the note if present.
        text = new.read_text(encoding="utf-8")
        text = text.replace(f"![[{old_audio.name}]]", f"![[{new_audio.name}]]")
        new.write_text(text, encoding="utf-8")
    with get_conn(cfg.db_path) as conn:
        conn.execute(
            "UPDATE sync_state SET note_path = ? WHERE note_path = ?",
            (canonical_note_path(new), canonical_note_path(old)),
        )
    return new


def _needs_llm_polish(headline: str) -> bool:
    h = (headline or "").strip()
    if not h or h.endswith("…") or h.endswith("..."):
        return True
    low = h.lower()
    weak = (
        "the conversation",
        "the meeting",
        "the call was",
        "the transcript",
        "a brief conversation",
        "a conversation between",
        "a message is left",
        "a voicemail",
        "untitled",
    )
    return any(low.startswith(w) for w in weak)


def retitle(
    *,
    cheap: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    only_weak: bool = False,
) -> dict:
    cfg = load_config()
    llm = None if cheap else LLM(cfg)
    root = cfg.vault.insights_path
    hub = f"{cfg.vault.insights_folder}.md"
    notes = sorted(p for p in root.glob("*.md") if p.name != hub)
    updated = skipped = errors = 0

    for path in notes:
        if limit is not None and updated >= limit:
            break
        try:
            post = frontmatter.load(str(path))
        except Exception as e:  # noqa: BLE001
            print(f"  ! parse {path.name}: {e}")
            errors += 1
            continue
        if post.get("synth") or not post.get("transcript_id"):
            skipped += 1
            continue

        tid = str(post.get("transcript_id"))
        date_val = post.get("date")
        try:
            when = parse_date(date_val)
        except Exception:
            print(f"  ! bad date on {path.name}")
            errors += 1
            continue

        existing = clean_headline(str(post.get("headline") or ""))
        if only_weak and existing and not _needs_llm_polish(existing) and not force:
            skipped += 1
            continue

        summary = _extract_summary(post.content)
        people = []
        for p in post.get("people") or []:
            people.append(re.sub(r"[\[\]]", "", str(p)).strip())
        old_title = _extract_h1(post.content) or path.stem

        if existing and not force and not only_weak:
            headline = existing
        elif cheap or llm is None:
            headline = headline_from_summary(summary, fallback=old_title)
        else:
            try:
                headline = _llm_headline(
                    llm,
                    old_title=old_title,
                    summary=summary,
                    people=people,
                    source=str(post.get("source") or ""),
                )
            except Exception as e:  # noqa: BLE001
                print(f"  ! llm {path.name}: {e}; using summary fallback")
                headline = headline_from_summary(summary, fallback=old_title)
                errors += 1

        if not headline:
            headline = "Untitled conversation"
        # Always run cheap cleanup on final headline.
        if _needs_llm_polish(headline):
            polished = headline_from_summary(summary, fallback=headline)
            if polished and not _needs_llm_polish(polished):
                headline = polished
            else:
                headline = clean_headline(headline)
        display = compose_display_title(headline, when)

        post["headline"] = headline
        post.content = _set_h1(post.content, display)
        post.content = re.sub(
            r"(\*\*Date:\*\*\s*).+",
            rf"\1{format_long_date(when)}",
            post.content,
            count=1,
        )

        target = _target_path(cfg, when, headline, tid, path)
        print(f"  {path.name}")
        print(f"    → {display}")

        if dry_run:
            updated += 1
            continue

        dumped = frontmatter.dumps(post)
        path.write_text(dumped if dumped.endswith("\n") else dumped + "\n", encoding="utf-8")
        _rename_with_audio(cfg, path, target, dry_run=False)
        updated += 1

    if not dry_run and updated:
        n = reindex_all(cfg)
        writer.rebuild_indexes(cfg)
        print(f"[retitle] reindexed {n} notes")

    summary = {"updated": updated, "skipped": skipped, "errors": errors}
    print(f"[retitle] {summary}")
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cheap", action="store_true", help="Derive titles from summaries (no LLM).")
    p.add_argument("--force", action="store_true", help="Overwrite notes that already have a headline.")
    p.add_argument(
        "--only-weak",
        action="store_true",
        help="Only retitle notes whose headline still looks like a summary fallback.",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    retitle(
        cheap=args.cheap,
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
        only_weak=args.only_weak,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
