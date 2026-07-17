"""Feedback-loop guard + note round-trip: synthesis output must never be
re-ingested, and checkbox state / attendee emails must survive the parse."""
from pathlib import Path

from transcript_analyzer.pipeline import indexer

NOTE = """---
source: granola
date: 2026-07-01
transcript_id: abc123
people:
  - "[[Angela Jin]]"
topics:
  - "pricing"
action_items:
  - "Review the deck"
  - "Send the recap"
attendees:
  - name: "Angela Jin"
    email: "angela@example.com"
---

# Chat with Angela

## Summary
Angela agreed to review the pricing deck.

## Action Items
- [ ] Review the deck
- [x] Send the recap

## Transcript
> [!note]- Full transcript
> Angela: I will review the deck.
"""

SYNTH_NOTE = """---
synth: true
---

# Digest

<!-- synth:begin -->
stuff
<!-- synth:end -->
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_note_round_trip(cfg):
    p = write(cfg.vault.insights_path / "2026-07-01 chat-with-angela.md", NOTE)
    rec = indexer.parse_note(p)
    assert rec is not None
    assert rec.transcript_id == "abc123"
    assert rec.action_items == ["Review the deck", "Send the recap"]
    # Checkbox state comes from the body: the ticked item is closed.
    assert rec.open_action_items == ["Review the deck"]
    assert rec.attendees[0].email == "angela@example.com"
    assert rec.attendees[0].key == "angela@example.com"
    assert "I will review the deck." in rec.transcript_text


def test_synth_notes_never_parsed(cfg):
    p = write(cfg.vault.insights_path / "Digests" / "2026-07-01.md", SYNTH_NOTE)
    assert indexer.parse_note(p) is None
    assert indexer.index_note(cfg, p) is None


def test_note_without_transcript_id_skipped(cfg):
    p = write(cfg.vault.insights_path / "random.md", "# Just a note\n")
    assert indexer.parse_note(p) is None


def test_iter_excludes_subdirs_and_hub(cfg):
    write(cfg.vault.insights_path / "2026-07-01 real.md", NOTE)
    write(cfg.vault.insights_path / "Transcript Insights.md", "# hub\n")
    write(cfg.vault.insights_path / "Digests" / "x.md", SYNTH_NOTE)
    write(cfg.vault.insights_path / "People" / "Angela.md", SYNTH_NOTE)
    paths = [p.name for p in indexer._iter_note_paths(cfg)]
    assert paths == ["2026-07-01 real.md"]


def test_reindex_all_populates_db(cfg):
    write(cfg.vault.insights_path / "2026-07-01 chat.md", NOTE)
    assert indexer.reindex_all(cfg) == 1
    from transcript_analyzer.db import all_transcripts, get_conn

    with get_conn(cfg.db_path) as conn:
        recs = all_transcripts(conn)
    assert len(recs) == 1
    assert recs[0].open_action_items == ["Review the deck"]
    assert recs[0].attendees[0].email == "angela@example.com"
