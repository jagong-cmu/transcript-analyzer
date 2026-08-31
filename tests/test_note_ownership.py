"""Nothing that is not PROVABLY ours is written or renamed over.

The vault has no backup and a note's filename is now derived from an LLM
headline that can be re-worded on any re-sync, so both the sync path
(writer.note_path_for) and the retitle migration can land on a name the vault
owner already used. Ownership therefore has to be read back off the target,
never inferred from the absence of a transcript_id.
"""
import importlib.util
from datetime import date
from pathlib import Path

from transcript_analyzer.models import Insight, Transcript
from transcript_analyzer.obsidian import writer
from transcript_analyzer.pipeline import indexer

_SPEC = importlib.util.spec_from_file_location(
    "retitle_notes",
    Path(__file__).resolve().parents[1] / "scripts" / "retitle_notes.py",
)
retitle_notes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retitle_notes)

HAND_WRITTEN = """# Pricing deck review

My own notes from the meeting. Nothing generated ever wrote this file.
"""

BASE_NAME = "2026-07-01 pricing-deck-review.md"
SUFFIXED_NAME = "2026-07-01 pricing-deck-review (t1abcd).md"


def _transcript(tid: str = "t1abcdef") -> Transcript:
    return Transcript(
        id=tid,
        source="granola",
        native_id=f"n-{tid}",
        title="raw source title",
        date=date(2026, 7, 1),
        text="Angela: I will review the deck.",
    )


def _insight() -> Insight:
    return Insight(headline="Pricing deck review", summary="Angela reviews the deck.")


def test_write_note_never_overwrites_a_hand_written_note(cfg):
    victim = cfg.vault.insights_path / BASE_NAME
    victim.write_text(HAND_WRITTEN, encoding="utf-8")
    before = victim.read_bytes()

    transcript = _transcript()
    path = writer.write_note(cfg, transcript, _insight())

    assert path.name == SUFFIXED_NAME
    assert victim.read_bytes() == before, "the vault owner's note was overwritten"
    rec = indexer.parse_note(path)
    assert rec is not None and rec.transcript_id == transcript.id

    # And the next sync keeps landing on the note it already owns.
    assert writer.write_note(cfg, transcript, _insight()) == path
    assert victim.read_bytes() == before


def test_write_note_never_overwrites_an_unreadable_target(cfg):
    victim = cfg.vault.insights_path / BASE_NAME
    victim.write_bytes(b"\xff\xfe\x00 not decodable as utf-8")
    before = victim.read_bytes()

    path = writer.write_note(cfg, _transcript(), _insight())

    assert path.name == SUFFIXED_NAME
    assert victim.read_bytes() == before


def test_write_note_never_overwrites_a_target_that_errors_on_read(cfg, monkeypatch):
    victim = cfg.vault.insights_path / BASE_NAME
    victim.write_text(HAND_WRITTEN, encoding="utf-8")
    before = victim.read_bytes()
    real_read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if Path(self) == victim:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)

    path = writer.write_note(cfg, _transcript(), _insight())

    assert path.name == SUFFIXED_NAME
    assert victim.read_bytes() == before


def test_disambiguation_keeps_going_past_an_occupied_suffix(cfg):
    first = cfg.vault.insights_path / BASE_NAME
    second = cfg.vault.insights_path / SUFFIXED_NAME
    first.write_text(HAND_WRITTEN, encoding="utf-8")
    second.write_text(HAND_WRITTEN + "\nAnd a second one.\n", encoding="utf-8")
    before = (first.read_bytes(), second.read_bytes())

    path = writer.write_note(cfg, _transcript(), _insight())

    assert path.name == "2026-07-01 pricing-deck-review (t1abcd-2).md"
    assert (first.read_bytes(), second.read_bytes()) == before


def test_two_transcripts_that_slugify_alike_still_get_the_short_id(cfg):
    first = writer.write_note(cfg, _transcript("t1abcdef"), _insight())
    second = writer.write_note(cfg, _transcript("t2fedcba"), _insight())

    assert first.name == BASE_NAME
    assert second.name == "2026-07-01 pricing-deck-review (t2fedc).md"
    assert indexer.parse_note(first).transcript_id == "t1abcdef"
    assert indexer.parse_note(second).transcript_id == "t2fedcba"


NOTE_TO_RETITLE = """---
source: granola
date: 2026-07-01
transcript_id: t1abcdef
headline: "Pricing deck review"
---

# raw source title

**Source:** granola  ·  **Date:** July 1st, 2026

## Summary
Angela agreed to review the pricing deck.

## Transcript
> [!note]- Full transcript
> Angela: I will review the deck.
"""


def test_retitle_never_renames_over_a_hand_written_note(cfg, monkeypatch):
    monkeypatch.setattr(retitle_notes, "load_config", lambda: cfg)
    old = cfg.vault.insights_path / "2026-07-01 raw-source-title.md"
    old.write_text(NOTE_TO_RETITLE, encoding="utf-8")
    victim = cfg.vault.insights_path / BASE_NAME
    victim.write_text(HAND_WRITTEN, encoding="utf-8")
    before = victim.read_bytes()

    result = retitle_notes.retitle(cheap=True)

    assert result["updated"] == 1 and result["errors"] == 0
    assert victim.read_bytes() == before, "the vault owner's note was renamed over"
    moved = cfg.vault.insights_path / SUFFIXED_NAME
    assert moved.exists() and not old.exists()
    rec = indexer.parse_note(moved)
    assert rec is not None and rec.transcript_id == "t1abcdef"
    assert rec.title == "Pricing deck review, July 1st, 2026"
