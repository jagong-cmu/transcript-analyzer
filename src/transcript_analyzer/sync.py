"""Sync orchestrator: pull new transcripts -> insights -> Obsidian note -> index.

Idempotent: each (source, native_id) is tracked with a content hash in sync_state,
so re-running only reprocesses changed transcripts. Junk transcripts are
filtered before any (billable) LLM call and recorded so they are never
refetched. After a successful sync pass, the synthesis engine runs behind its
own daily cadence guard.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .config import Config, load_config
from .connectors import pocket
from .courses import index_courses
from .db import (
    canonical_note_path,
    get_conn,
    get_meta,
    get_sync_hash,
    get_sync_note_path,
    known_course_rows,
    record_sync,
    set_meta,
)
from .models import Insight, Transcript
from .obsidian import writer
from .pipeline import lecture
from .pipeline.indexer import index_note, insight_from_note
from .pipeline.insights import extract_insight
from .pipeline.llm import (
    LLM,
    LLMBudgetError,
    LLMKillSwitchError,
    LLMResponseError,
    LLMTruncatedError,
)
from .pipeline.quality import junk_reason
from .titles import compose_display_title

_log = logging.getLogger(__name__)

FAILURE_COUNTER_KEY = "insight_failures_total"

# Marks a sync_state row whose note is written but whose recording still has to
# be fetched, so the stored hash cannot match and the next pass reprocesses it.
AUDIO_RETRY_HASH_SUFFIX = ":audio-retry"

# How many times a study-notes response that MIGHT be transient is retried
# before the note is written with a visible failure marker instead. Each retry
# is a full lecture-stage call, so an unbounded one bills a 32k-output Opus 5
# request every sync interval until the monthly ceiling stops all ingestion.
STUDY_NOTE_MAX_ATTEMPTS = 3


def _high_water_key(source: str) -> str:
    return f"{source}_last_created_at"


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


def _maybe_download_audio(
    cfg: Config, transcript: Transcript, note_path: Path
) -> tuple[Optional[str], bool]:
    """Download a Pocket recording's audio into the vault.

    Returns (filename to embed, still owed). Audio is best effort, so an
    unavailable recording or a failed fetch is simply no embed — but a
    download DISCARDED because its stem was taken mid-stream is work this
    transcript still owes, and saying so is what stops the hash-idempotent
    sync from remembering it as done and never fetching it again.
    """
    if transcript.source != "pocket" or not cfg.pocket.download_audio:
        return None, False
    from .connectors.pocket_api import AudioStemTaken, PocketClient  # lazy (needs key)

    dest = writer.audio_path_for(cfg, note_path)
    try:
        with PocketClient(cfg) as pc:
            got = pc.download_audio(transcript.native_id, dest, transcript.id)
    except AudioStemTaken:
        return None, True
    except Exception:  # noqa: BLE001 - audio is best-effort, never fail the note
        return None, False
    return (dest.name if got else None), False


def _owned_prev_note(prev_path: Optional[str], transcript: Transcript) -> Optional[Path]:
    """The note sync_state remembers for this transcript, only when it is OURS.

    sync_state stores a path, not a claim on the file living there: the vault
    owner can delete a note and a later transcript whose headline slugifies the
    same way then legitimately takes that filename. Acting on the remembered
    path alone moves that stranger's recording and deletes their note. So the
    same proof the write and rename targets require — `writer.owns_note`, the
    one definition — gates the destructive paths here, and a path we cannot
    prove is ours is left alone and named in a warning: an orphan costs a
    manual cleanup, a wrong delete costs data this vault cannot recover.
    """
    if not prev_path:
        return None
    prev = Path(prev_path)
    if writer.owns_note(prev, transcript.id):
        return prev
    if prev.exists():
        _log.warning(
            "not touching %s: sync_state records it for %s/%s, but its "
            "transcript_id does not prove it is that transcript's note "
            "(it may now belong to another recording, or to you)",
            prev, transcript.source, transcript.native_id,
        )
    return None


def _is_stale_note(prev: Path, note_path: Path, transcript_id: str) -> bool:
    """Whether `prev` is our own note AND provably a DIFFERENT file from the
    note just written.

    The vault has no backup, so this fails safe twice over: a file we cannot
    prove belongs to this transcript is never deleted, and neither is a path
    that cannot be shown to be another file — a relative or symlinked vault
    path spelling the same note two ways, or a path that cannot be stat'ed.
    """
    if not writer.owns_note(prev, transcript_id):
        return False
    try:
        if not prev.exists() or prev.samefile(note_path):
            return False
    except OSError:
        return False
    return True


def _count_failure(cfg: Config) -> int:
    """Visible failure counter (surfaced in the sync summary and /health-adjacent
    tooling) so silent extraction failures can't hide."""
    with get_conn(cfg.db_path) as conn:
        total = int(get_meta(conn, FAILURE_COUNTER_KEY) or 0) + 1
        set_meta(conn, FAILURE_COUNTER_KEY, str(total))
    return total


def _known_courses(cfg: Config) -> dict:
    """The courses the index already holds, so a lecture rejoins its own.

    Read fresh per transcript rather than once per run: a sync pass that
    ingests week 1 and week 2 of the same course in one go must let the second
    bind to the first (see courses.py).
    """
    with get_conn(cfg.db_path) as conn:
        return index_courses(known_course_rows(conn))


def _insight_for(
    cfg: Config, transcript: Transcript, llm: LLM, previous: Optional[Path] = None
) -> tuple[Insight, str]:
    """(insight, terminal failure reason) for the extraction pass.

    The SAME deterministic-failure rule the lecture stage follows, at the stage
    that runs on every transcript. A response cut off at the output cap is not
    transient: the same transcript against the same cap overflows identically
    forever, so letting it propagate to `sync`'s per-transcript handler means
    `record_sync` is never reached and the next cycle buys the same overflow
    again, every interval, until the monthly ceiling halts ingestion for
    everything else.

    So it is recorded instead of retried: the reason comes back, the note is
    written carrying a visible marker, and `record_sync` is reached, so it
    bills ONCE.

    What the note SAYS in that case is whatever the last complete extraction
    left, read back off the note itself. A degraded pass must never be a
    downgrade: an empty payload would blank the summary, the key points, the
    action items and the `kind` — and, because the headline drives the
    filename, rename the note and strand its recording. There is nothing to
    carry only when this transcript has no note yet, and then the marker
    stands alone.

    Kill-switch and budget errors still propagate untouched, and every other
    failure still reaches the caller's handler unchanged.
    """
    try:
        return extract_insight(
            transcript, cfg, llm=llm, known_courses=_known_courses(cfg)
        ), ""
    except LLMTruncatedError as e:
        _log.warning(
            "extraction for %r overflowed the output cap (%s); marking the note "
            "and keeping what the last complete extraction wrote, rather than "
            "paying for the same overflow every cycle",
            transcript.title, e,
        )
        carried = insight_from_note(previous) if previous is not None else None
        return (carried or Insight()), str(e)


def _study_attempts_key(transcript_id: str) -> str:
    return f"study_attempts:{transcript_id}"


def _bump_study_attempts(cfg: Config, transcript_id: str) -> int:
    with get_conn(cfg.db_path) as conn:
        key = _study_attempts_key(transcript_id)
        n = int(get_meta(conn, key) or 0) + 1
        set_meta(conn, key, str(n))
    return n


def _clear_study_attempts(cfg: Config, transcript_id: str) -> None:
    with get_conn(cfg.db_path) as conn:
        set_meta(conn, _study_attempts_key(transcript_id), "0")


def _study_notes_for(
    cfg: Config, transcript: Transcript, insight: Insight, llm: LLM, note_path: Path
) -> tuple[Optional["lecture.StudyOutcome"], str]:
    """(outcome, terminal failure reason) for one lecture's study-notes pass.

    Three postures, because "the response was bad" is not one failure.

    Budget and kill-switch errors PROPAGATE: they mean stop the whole run.

    A TRUNCATED response is deterministic — the same transcript against the
    same `lecture_max_tokens` overflows identically forever — so retrying it
    buys nothing and re-bills a 32k-output Opus 5 call every sync cycle until
    the monthly ceiling halts ingestion for everything else. It is terminal on
    the first attempt: the reason comes back, the note is written carrying a
    visible marker, and `record_sync` is reached so the transcript is not
    reprocessed. One extract call, one lecture call, then done.

    Any other unusable response MIGHT be transient, so it is retried — but a
    bounded number of times, ending in that same terminal marked note rather
    than looping forever.

    Everything else is contained: a note with a detailed summary and no study
    notes is still the deliverable. A renderer failure never reaches here at
    all — `lecture.produce` handles it and still writes the markdown.
    """
    if not (insight.is_lecture and cfg.lecture.enabled):
        return None, ""
    try:
        outcome = lecture.produce(cfg, transcript, insight, note_path, llm)
    except (LLMKillSwitchError, LLMBudgetError):
        raise
    except LLMTruncatedError as e:
        _clear_study_attempts(cfg, transcript.id)
        _log.warning(
            "study notes for %s overflowed the output cap (%s); writing the "
            "note with a failure marker rather than paying for the same "
            "overflow every cycle", note_path.name, e,
        )
        return None, str(e)
    except LLMResponseError as e:
        attempts = _bump_study_attempts(cfg, transcript.id)
        if attempts < STUDY_NOTE_MAX_ATTEMPTS:
            raise
        _clear_study_attempts(cfg, transcript.id)
        _log.warning(
            "study notes for %s failed %d times (%s); writing the note with a "
            "failure marker", note_path.name, attempts, e,
        )
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - study notes must not cost the note
        _log.warning(
            "study notes failed for %s (%s); writing the note without them",
            note_path.name, e,
        )
        return None, ""
    _clear_study_attempts(cfg, transcript.id)
    return outcome, ""


def process_transcript(
    cfg: Config,
    transcript: Transcript,
    llm: LLM,
    *,
    dry_run: bool = False,
) -> dict:
    # The note this transcript already has, proven ours, resolved BEFORE the
    # extraction: a truncated pass carries its content forward rather than
    # replacing it with a blank.
    with get_conn(cfg.db_path) as conn:
        prev_path = get_sync_note_path(conn, transcript.source, transcript.native_id)
    prev_note = _owned_prev_note(prev_path, transcript)

    insight, extract_error = _insight_for(cfg, transcript, llm, previous=prev_note)
    display = compose_display_title(
        insight.headline or transcript.title, transcript.date
    )
    result = {
        "id": transcript.id,
        "title": display,
        "source": transcript.source,
        "kind": insight.kind,
        "note_path": None,
        "study_notes": None,
        "study_pdf": None,
        "study_error": None,
        "extract_error": extract_error or None,
    }
    if dry_run:
        return result

    # A DEGRADED pass — one whose extraction truncated — never renames and
    # never deletes. It has no new headline to justify moving a note, an mp3
    # and a study stem, and `record_sync` below would make the move permanent.
    # It writes exactly where the note already is; only a transcript with no
    # note yet takes a fresh stem.
    degraded = bool(extract_error) and prev_note is not None
    prospective = (
        prev_note if degraded else writer.note_path_for(cfg, transcript, insight)
    )

    # EVERY step that can propagate runs before EVERY step that mutates the
    # vault. The lecture pass is the propagating one (budget, kill switch, a
    # response worth retrying), so it goes first: a failure after the rename
    # moves or the download would leave an mp3 or a study note on a stem no
    # note ever occupies, `claimable_stem` would refuse that stem forever
    # after, and every retry would land one rung up the ladder and re-fetch
    # the whole recording.
    study, study_error = _study_notes_for(cfg, transcript, insight, llm, prospective)
    if study is not None:
        result["study_notes"] = str(study.study_path) if study.study_path else None
        result["study_pdf"] = str(study.pdf_path) if study.pdf_path else None
        # The study-notes pass reads the whole lecture at the highest-effort
        # model in the system, so its overview is the better detailed summary.
        if study.notes.overview:
            insight = insight.model_copy(
                update={"detailed_summary": study.notes.overview}
            )
    result["study_error"] = study_error or None

    # A re-worded headline renames the note; carry its recording and its study
    # notes across, so nothing below writes a second copy beside them. The
    # study move is skipped when the pass just wrote fresh notes at the
    # destination stem — moving the superseded ones over them would destroy
    # what was generated moments ago.
    if (
        prev_note
        and not degraded
        and canonical_note_path(prev_note) != canonical_note_path(prospective)
    ):
        writer.move_audio_with_note(cfg, prev_note, prospective, transcript.id)
        if study is None:
            writer.move_study_with_note(cfg, prev_note, prospective, transcript.id)

    # Download the recording's audio into the vault (Pocket only) and embed it.
    audio_name, audio_still_owed = _maybe_download_audio(cfg, transcript, prospective)

    # The claim above is the ONE decision: the download wrote the mp3 against
    # it and the body embeds that name, so the note has to land on it too.
    # `previous` is what a rename carries across — the owner's tail, their
    # ticked boxes, the study link — because the destination stem is empty
    # until this write lands.
    note_path = writer.write_note(
        cfg,
        transcript,
        insight,
        audio_name=audio_name,
        path=prospective,
        previous=prev_note,
        study_stem_name=study.stem if study else None,
        has_study_pdf=bool(study and study.pdf_path),
        study_error=study_error,
        extract_error=extract_error,
        asr_repairs=study.notes.asr_repairs if study else None,
    )
    result["note_path"] = str(note_path)

    # STRICTLY after the write: the new note now provably carries what the old
    # one held, so removing the old one loses nothing. A failure between the
    # two leaves an orphan to clean up by hand, which this vault can recover
    # from — a delete before the write is not recoverable at all.
    if prev_note and not degraded and _is_stale_note(prev_note, note_path, transcript.id):
        try:
            os.remove(prev_note)
        except OSError:
            pass

    # Index the note we just wrote (parses it back -> sqlite).
    index_note(cfg, note_path)
    # Recording the transcript's own hash is what makes the next pass skip it,
    # so a discarded recording is stored under a hash that cannot match: the
    # note is complete, but the audio is still owed and has to be re-fetched.
    synced_hash = (
        transcript.hash + AUDIO_RETRY_HASH_SUFFIX if audio_still_owed else transcript.hash
    )
    with get_conn(cfg.db_path) as conn:
        record_sync(
            conn, transcript.source, transcript.native_id,
            synced_hash, canonical_note_path(note_path), _now(),
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
    synthesize_after: bool = True,
) -> dict:
    cfg = cfg or load_config()
    sources = sources or _default_sources(cfg)
    llm = LLM(cfg)

    health = llm.health()
    if not health["ok"]:
        # Don't burn a whole source pass into per-transcript failures.
        reason = (
            "kill switch on" if health["kill_switch"]
            else "no API key configured" if not health["key_configured"]
            else f"monthly budget reached (${health['month_spend_usd']:.2f} "
                 f"of ${health['monthly_budget_usd']:.2f})"
        )
        print(f"[sync] SKIPPED: Claude API unavailable ({reason}).", file=sys.stderr)
        return {"processed": 0, "skipped": 0, "junk": 0, "errors": 1,
                "items": [], "error_details": [{"error": reason}]}

    processed, skipped, junk, errors = [], 0, 0, []
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

                # Quality floor: junk never reaches the (billable) LLM or the
                # vault, and is recorded so it isn't reconsidered next cycle.
                reason = junk_reason(t, cfg)
                if reason is not None:
                    junk += 1
                    if verbose:
                        print(f"  - junk: {t.title} ({reason})")
                    if not dry_run:
                        with get_conn(cfg.db_path) as conn:
                            record_sync(conn, t.source, t.native_id, t.hash, "", _now())
                    continue

                try:
                    res = process_transcript(cfg, t, llm, dry_run=dry_run)
                    processed.append(res)
                    if verbose:
                        print(f"  + {res['title']}")
                except (LLMKillSwitchError, LLMBudgetError) as e:
                    # Hard stop: no point trying the remaining transcripts.
                    errors.append({"id": t.id, "title": t.title, "error": str(e)})
                    print(f"[sync] STOPPING: {e}", file=sys.stderr)
                    break
                except Exception as e:  # noqa: BLE001 - one bad transcript shouldn't stop sync
                    total = _count_failure(cfg)
                    errors.append({"id": t.id, "title": t.title, "error": str(e)})
                    print(f"  ! error on {t.title}: {e} "
                          f"(insight failures to date: {total})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - a whole source failing (e.g. Granola auth)
            errors.append({"source": source, "error": str(e)})
            print(f"[sync] source {source} failed: {e}", file=sys.stderr)

        # Advance the source's high-water mark after a successful, non-dry pass.
        if not dry_run and max_sort and max_sort != (created_after or ""):
            with get_conn(cfg.db_path) as conn:
                set_meta(conn, _high_water_key(source), max_sort)

    if not dry_run and processed:
        writer.rebuild_indexes(cfg)

    # Synthesis runs at most once per day (its own cadence guard), never per
    # 20-minute sync cycle.
    synthesis_summary = None
    if not dry_run and synthesize_after and cfg.synthesis.enabled:
        from .pipeline import synthesize

        try:
            synthesis_summary = synthesize.maybe_run(cfg, llm=llm, verbose=verbose)
        except (LLMKillSwitchError, LLMBudgetError) as e:
            print(f"[sync] synthesis stopped: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - synthesis must never break ingestion
            print(f"[sync] synthesis failed: {e}", file=sys.stderr)

    summary = {
        "processed": len(processed),
        "skipped": skipped,
        "junk": junk,
        "errors": len(errors),
        "items": processed,
        "error_details": errors,
        "synthesis": synthesis_summary,
    }
    if verbose:
        print(f"[sync] done: {summary['processed']} processed, "
              f"{summary['skipped']} skipped, {summary['junk']} junk, "
              f"{summary['errors']} errors")
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
    parser.add_argument("--no-synthesis", action="store_true",
                        help="Skip the post-sync synthesis pass.")
    args = parser.parse_args(argv)

    cfg = load_config()
    summary = sync(
        cfg,
        sources=args.source,
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
        synthesize_after=not args.no_synthesis,
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
