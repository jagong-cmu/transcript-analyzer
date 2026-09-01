#!/usr/bin/env python3
"""Backfill detailed summaries — and study notes for lectures — into existing notes.

ON DEMAND ONLY. Nothing in sync calls this: re-summarizing the whole vault is
an expensive, irreversible-looking operation, so it is a flag you type.

    python scripts/backfill_summaries.py                    # dry run (the default)
    python scripts/backfill_summaries.py --limit 5 --apply  # write five notes
    python scripts/backfill_summaries.py --apply --batch    # whole vault, Batch API
    python scripts/backfill_summaries.py --apply --lectures-only

Safety, in the order it matters:

  * DRY RUN IS THE DEFAULT. `--apply` is the only thing that writes.
  * A BACKUP PASS runs first. Every note about to be rewritten is copied to
    `data/backfill-backups/<timestamp>/` — outside the vault — before the
    first write, so a bad run is recoverable by copying files back.
  * HAND EDITS SURVIVE. Notes are rewritten through `writer.write_note`,
    which splices the managed region and keeps whatever the vault owner wrote
    below the end marker, plus every action-item box they had ticked.
  * OWNERSHIP IS PROVEN, not assumed: a note whose `transcript_id` does not
    match the record being rewritten is skipped and named.
  * THE TRANSCRIPT IS NOT REFETCHED. It is read back out of the note itself,
    through the same extractor the index uses, so this cannot put another
    recording's words into a note.

Cost: one extraction call per note, plus one study-notes call per lecture.
`--batch` sends the extractions through the Message Batches API at half price
(Sonnet 5 by default; see `[anthropic.stage_models] backfill`). The per-run
call budget still applies — raise it with `--max-calls` when the plan needs it.
"""
from __future__ import annotations

import argparse
import dataclasses
import shutil
import sys
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frontmatter  # noqa: E402

from transcript_analyzer.config import Config, load_config  # noqa: E402
from transcript_analyzer.courses import index_courses  # noqa: E402
from transcript_analyzer.db import (  # noqa: E402
    all_transcripts,
    get_conn,
    known_course_rows,
)
from transcript_analyzer.models import Insight, NoteRecord, Transcript  # noqa: E402
from transcript_analyzer.obsidian import writer  # noqa: E402
from transcript_analyzer.pipeline import batch as batch_api  # noqa: E402
from transcript_analyzer.pipeline import insights as insights_mod  # noqa: E402
from transcript_analyzer.pipeline import lecture as lecture_mod  # noqa: E402
from transcript_analyzer.pipeline.indexer import extract_transcript, index_note  # noqa: E402
from transcript_analyzer.pipeline.llm import (  # noqa: E402
    LLM,
    LLMError,
    LLMResponseError,
)
from transcript_analyzer.titles import clean_headline  # noqa: E402

BACKFILL_STAGE = "backfill"


def _backup_root(cfg: Config) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return cfg.data_dir / "backfill-backups" / stamp


def _already_backfilled(path: Path) -> bool:
    """Whether this note already carries the new shape.

    `abstract:` in frontmatter is the marker: it exists only once a note has
    been written with the two-summary split, so a re-run skips what it did
    last time instead of paying for it again.
    """
    try:
        post = frontmatter.load(str(path))
    except Exception:  # noqa: BLE001 - an unreadable note is not ours to rewrite
        return True
    return bool(str(post.metadata.get("abstract") or "").strip())


def _transcript_for(rec: NoteRecord, path: Path) -> Optional[Transcript]:
    """Rebuild the Transcript a note was written from, out of the note itself.

    `native_id` is deliberately empty: this script never touches `sync_state`,
    and resolving a native id from a path is the ambiguous lookup that the
    timestamp backfill has to guard against. Nothing downstream of a write at
    an explicit path reads it.
    """
    text = extract_transcript(path.read_text(encoding="utf-8"))
    if not text.strip():
        return None
    try:
        when = _date.fromisoformat(rec.date[:10])
    except ValueError:
        return None
    return Transcript(
        id=rec.transcript_id,
        source=rec.source if rec.source in ("granola", "pocket") else "pocket",
        native_id="",
        title=rec.title,
        date=when,
        participants=list(rec.people),
        attendees=list(rec.attendees),
        text=text,
    )


def _headline_of(rec: NoteRecord) -> str:
    """The note's current headline — its indexed title without the date.

    Through `clean_headline`, the one definition that composes and strips that
    suffix: splitting on the last comma looks equivalent and is not, because a
    headline may contain commas of its own ("Pricing chat with Angela, July
    1st, 2026" -> "Pricing chat with Angela, July 1st").
    """
    return clean_headline(rec.title)


def _insight_for(rec: NoteRecord, data: dict, transcript: Transcript, known: dict) -> Insight:
    """The new insight, keeping the note's existing headline.

    A backfill re-summarizes; it does not rename. The filename is derived from
    the headline, and churning titles across the whole vault would break every
    wikilink the owner has written by hand. `scripts/retitle_notes.py` is the
    tool that renames, and it moves the audio and study notes with the note.
    """
    insight = insights_mod.insight_from_payload(data, transcript, known_courses=known)
    return insight.model_copy(update={"headline": _headline_of(rec) or insight.headline})


def _write_one(
    cfg: Config,
    rec: NoteRecord,
    path: Path,
    transcript: Transcript,
    insight: Insight,
    llm: LLM,
    *,
    study_notes: bool,
    backup_dir: Path,
) -> dict:
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)

    study = None
    if study_notes and insight.is_lecture and cfg.lecture.enabled:
        try:
            study = lecture_mod.produce(cfg, transcript, insight, path, llm)
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001 - the summary is still worth writing
            print(f"    ! study notes failed: {e}")
        else:
            if study.notes.overview:
                insight = insight.model_copy(
                    update={"detailed_summary": study.notes.overview}
                )

    written = writer.write_note(
        cfg,
        transcript,
        insight,
        audio_name=_existing_audio_name(cfg, path),
        path=path,
        study_stem_name=study.stem if study else None,
        has_study_pdf=bool(study and study.pdf_path),
        asr_repairs=study.notes.asr_repairs if study else None,
    )
    index_note(cfg, written)
    return {
        "note": str(written),
        "kind": insight.kind,
        "study_notes": str(study.study_path) if study and study.study_path else None,
        "study_pdf": str(study.pdf_path) if study and study.pdf_path else None,
    }


def _existing_audio_name(cfg: Config, note_path: Path) -> Optional[str]:
    """Keep the note's recording embedded across a rewrite.

    The embed names a file in Attachments/ keyed on this note's stem, and the
    rewrite does not move the note — so if the mp3 is there, it is still this
    note's and the player must not disappear from the rewritten note.
    """
    audio = writer.audio_path_for(cfg, note_path)
    return audio.name if audio.exists() else None


def _plan(cfg: Config, *, lectures_only: bool, limit: Optional[int], force: bool):
    """The notes this run would touch, with everything needed to rewrite them."""
    with get_conn(cfg.db_path) as conn:
        records = all_transcripts(conn)
        known = index_courses(known_course_rows(conn))

    plan = []
    skipped = {"missing": 0, "not_ours": 0, "done": 0, "no_transcript": 0, "not_lecture": 0}
    for rec in records:
        if limit is not None and len(plan) >= limit:
            break
        path = Path(rec.note_path or "")
        if not path.exists():
            skipped["missing"] += 1
            continue
        # Ownership before anything else: a note whose id does not match this
        # record belongs to another recording (or to the vault owner).
        if not writer.owns_note(path, rec.transcript_id):
            print(f"  · skip {path.name}: transcript_id does not prove it is {rec.transcript_id}")
            skipped["not_ours"] += 1
            continue
        if lectures_only and not rec.is_lecture:
            skipped["not_lecture"] += 1
            continue
        if not force and _already_backfilled(path):
            skipped["done"] += 1
            continue
        transcript = _transcript_for(rec, path)
        if transcript is None:
            print(f"  · skip {path.name}: no transcript in the note to summarize")
            skipped["no_transcript"] += 1
            continue
        plan.append((rec, path, transcript))
    return plan, known, skipped


def backfill(
    *,
    apply: bool,
    limit: Optional[int],
    force: bool,
    lectures_only: bool,
    study_notes: bool,
    use_batch: bool,
    max_calls: Optional[int],
    resume_batch: Optional[str],
) -> dict:
    cfg = load_config()
    if max_calls is not None:
        # An explicit, typed-in raise of the per-run guard for THIS run only.
        cfg = dataclasses.replace(
            cfg,
            anthropic=dataclasses.replace(cfg.anthropic, max_calls_per_run=max_calls),
        )
    llm = LLM(cfg)

    plan, known, skipped = _plan(
        cfg, lectures_only=lectures_only, limit=limit, force=force
    )
    lectures = sum(1 for rec, _p, _t in plan if rec.is_lecture)
    calls = len(plan) + (lectures if study_notes else 0)
    print(
        f"[backfill] {len(plan)} note(s) to re-summarize "
        f"({lectures} lecture(s)); ~{calls} API call(s); skipped {skipped}"
    )
    if not plan and not resume_batch:
        return {"planned": 0, "written": 0, "skipped": skipped, "errors": 0}

    if not apply:
        for rec, path, _t in plan[:20]:
            print(f"  would rewrite {path.name}  [{rec.kind}]")
        if len(plan) > 20:
            print(f"  … and {len(plan) - 20} more")
        print("[backfill] DRY RUN — nothing written. Re-run with --apply.")
        return {"planned": len(plan), "written": 0, "skipped": skipped, "errors": 0}

    if calls > cfg.anthropic.max_calls_per_run:
        print(
            f"[backfill] this run needs ~{calls} calls but the per-run budget is "
            f"{cfg.anthropic.max_calls_per_run}. Re-run with --max-calls {calls} "
            f"(or raise [anthropic] max_calls_per_run).",
            file=sys.stderr,
        )
        return {"planned": len(plan), "written": 0, "skipped": skipped, "errors": 1}

    by_id = {rec.transcript_id: (rec, path, t) for rec, path, t in plan}
    payloads: dict[str, dict] = {}
    errors: dict[str, str] = {}

    if resume_batch:
        print(f"[backfill] collecting batch {resume_batch}")
        outcome = batch_api.collect(llm, resume_batch, stage=BACKFILL_STAGE)
        payloads, errors = outcome.results, outcome.errors
    elif use_batch:
        requests = []
        for tid, (_rec, _path, transcript) in by_id.items():
            system, user = insights_mod.extraction_prompt(transcript, known)
            requests.append(
                batch_api.BatchRequest(
                    custom_id=tid,
                    system=system,
                    user=user,
                    schema=insights_mod.INSIGHT_SCHEMA,
                    max_tokens=insights_mod.MAX_TOKENS,
                )
            )
        outcome = batch_api.run_batch(
            llm, requests, stage=BACKFILL_STAGE, on_progress=lambda m: print(f"  {m}")
        )
        payloads, errors = outcome.results, outcome.errors

    backup_dir = _backup_root(cfg)
    written, failed = 0, len(errors)
    for tid, (rec, path, transcript) in by_id.items():
        if tid in errors:
            print(f"  ! {path.name}: batch request {errors[tid]}")
            continue
        try:
            if use_batch or resume_batch:
                data = payloads.get(tid)
                if data is None:
                    continue
                insight = _insight_for(rec, data, transcript, known)
            else:
                system, user = insights_mod.extraction_prompt(transcript, known)
                data = llm.chat_json(
                    system, user,
                    schema=insights_mod.INSIGHT_SCHEMA,
                    max_tokens=insights_mod.MAX_TOKENS,
                    stage=BACKFILL_STAGE,
                )
                insight = _insight_for(rec, data, transcript, known)
            res = _write_one(
                cfg, rec, path, transcript, insight, llm,
                study_notes=study_notes, backup_dir=backup_dir,
            )
        except LLMResponseError as e:
            # Truncated or unparseable output is one NOTE's problem, not the
            # run's. Stopping here would discard every remaining payload a
            # --batch run has already been billed for.
            print(f"  ! {path.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        except LLMError as e:
            print(f"[backfill] STOPPING: {e}", file=sys.stderr)
            failed += 1
            break
        except Exception as e:  # noqa: BLE001 - one bad note must not end the run
            print(f"  ! {path.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        written += 1
        extra = " + study notes" if res["study_notes"] else ""
        print(f"  + {path.name} [{res['kind']}]{extra}")

    if written:
        print(f"[backfill] backups of the originals: {backup_dir}")
    summary = {
        "planned": len(plan),
        "written": written,
        "skipped": skipped,
        "errors": failed,
    }
    print(f"[backfill] {summary}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="Actually rewrite notes. Without it this is a dry run.")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after the first N eligible notes (newest first).")
    p.add_argument("--force", action="store_true",
                   help="Re-summarize notes that already have a detailed summary.")
    p.add_argument("--lectures-only", action="store_true",
                   help="Only notes already classified as lectures.")
    p.add_argument("--skip-study-notes", action="store_true",
                   help="Summaries only: do not generate study notes or PDFs for lectures.")
    p.add_argument("--batch", action="store_true",
                   help="Send extractions through the Batch API (half price, slower).")
    p.add_argument("--max-calls", type=int, default=None,
                   help="Raise the per-run API call budget for this run only.")
    p.add_argument("--resume-batch", default=None, metavar="BATCH_ID",
                   help="Collect a batch submitted by an earlier interrupted run.")
    args = p.parse_args(argv)

    summary = backfill(
        apply=args.apply,
        limit=args.limit,
        force=args.force,
        lectures_only=args.lectures_only,
        study_notes=not args.skip_study_notes,
        use_batch=args.batch,
        max_calls=args.max_calls,
        resume_batch=args.resume_batch,
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
