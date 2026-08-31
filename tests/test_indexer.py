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
    assert rec.title == "Chat with Angela, July 1st, 2026"
    assert rec.action_items == ["Review the deck", "Send the recap"]
    # Checkbox state comes from the body: the ticked item is closed.
    assert rec.open_action_items == ["Review the deck"]
    assert rec.attendees[0].email == "angela@example.com"
    assert rec.attendees[0].key == "angela@example.com"
    assert "I will review the deck." in rec.transcript_text


def test_parse_note_uses_headline_frontmatter(cfg):
    note = NOTE.replace(
        "transcript_id: abc123\n",
        'transcript_id: abc123\nheadline: "Pricing deck review with Angela"\n',
    ).replace("# Chat with Angela", "# Pricing deck review with Angela, July 1st, 2026")
    p = write(cfg.vault.insights_path / "2026-07-01 pricing.md", note)
    rec = indexer.parse_note(p)
    assert rec is not None
    assert rec.title == "Pricing deck review with Angela, July 1st, 2026"


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


def test_note_with_missing_date_still_indexes(cfg):
    """One note with a junk `date:` must not take the whole reindex down."""
    write(cfg.vault.insights_path / "2026-07-01 chat.md", NOTE)
    write(
        cfg.vault.insights_path / "undated.md",
        NOTE.replace("date: 2026-07-01\n", "").replace("abc123", "def456"),
    )
    assert indexer.reindex_all(cfg) == 2

    p = write(
        cfg.vault.insights_path / "bad-date.md",
        NOTE.replace("date: 2026-07-01", "date: whenever").replace("abc123", "ghi789"),
    )
    rec = indexer.parse_note(p)
    assert rec is not None
    # Falls back to the bare headline rather than raising on the bad date.
    assert rec.title == "Chat with Angela"


def test_writer_frontmatter_survives_backslashes_and_quotes(cfg):
    """An LLM headline with a backslash used to make the note unparseable —
    and parse_note swallows load errors, so the note vanished from the index."""
    from datetime import date as _date

    from transcript_analyzer.models import Attendee, Insight, Transcript
    from transcript_analyzer.obsidian import writer

    nasty = 'Migrating C:\\Users\\ops to the "new" share'
    transcript = Transcript(
        id="t9",
        source="granola",
        native_id="n9",
        title="raw",
        date=_date(2026, 7, 1),
        attendees=[Attendee(name=nasty, email="ops@example.com")],
        text="Ops: moving the share.",
    )
    insight = Insight(
        headline=nasty,
        summary="Moving the share.",
        topics=[nasty],
        action_items=[nasty],
        people=["Ops"],
    )
    p = write(
        cfg.vault.insights_path / "2026-07-01 migrating.md",
        writer.render_note(transcript, insight),
    )
    rec = indexer.parse_note(p)
    assert rec is not None, "note was dropped from the index"
    assert rec.title == f"{nasty}, July 1st, 2026"
    assert rec.topics == [nasty]
    assert rec.attendees[0].name == nasty


def test_writer_frontmatter_survives_newlines_and_control_chars(cfg):
    """An interior newline or control character in a hand-quoted scalar made the
    frontmatter unloadable (a '---' continuation line, or a C0 char PyYAML
    rejects outright), so the note vanished from the index — and values that did
    parse were silently folded onto one line."""
    from datetime import date as _date

    import frontmatter as _frontmatter

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    nasty = "Do X\n--- separator\twith \x0b \x0c \x00 and \x85"
    transcript = Transcript(
        id="t10",
        source="granola",
        native_id="n10",
        title="raw",
        date=_date(2026, 7, 1),
        text="Ops: doing X.",
    )
    insight = Insight(
        headline="Multi-line action items",
        summary="Doing X.",
        topics=[nasty],
        action_items=[nasty],
        people=["Ops"],
    )
    p = write(
        cfg.vault.insights_path / "2026-07-01 multiline.md",
        writer.render_note(transcript, insight),
    )

    rec = indexer.parse_note(p)
    assert rec is not None, "note was dropped from the index"
    # The frontmatter scalar round-trips exactly rather than folding.
    assert rec.topics == [nasty]
    assert _frontmatter.load(str(p)).metadata["action_items"] == [nasty]


def test_malformed_note_does_not_abort_the_whole_reindex(cfg):
    """A hand-edited note with a surprising field shape costs that note alone.

    `people: 42` makes parse_note iterate a non-iterable; reindex_all walks
    notes in a bare loop, so it used to take the entire vault index down.
    """
    write(
        cfg.vault.insights_path / "2026-06-01 broken.md",
        NOTE.replace('people:\n  - "[[Angela Jin]]"', "people: 42").replace(
            "abc123", "def456"
        ),
    )
    write(cfg.vault.insights_path / "2026-07-01 chat.md", NOTE)

    # The broken note sorts first, so the good one only indexes if the loop
    # survived it.
    assert indexer.reindex_all(cfg) == 1

    from transcript_analyzer.db import all_transcripts, get_conn

    with get_conn(cfg.db_path) as conn:
        recs = all_transcripts(conn)
    assert [r.transcript_id for r in recs] == ["abc123"]


def test_multiline_body_list_items_are_not_truncated(cfg):
    """The body list wins over the frontmatter in _parse_note, so an action
    item or key point carrying an interior newline used to reach the DB (and
    the commitments page, and every rollup) truncated at its first line — and a
    continuation line starting with '## ' faked a heading that cut the
    transcript out of the index entirely."""
    from datetime import date as _date

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    item = "Ship the migration:\nrun retitle_notes.py then backfill"
    point = "Decision recorded\n## Transcript\n> injected"
    transcript = Transcript(
        id="t11",
        source="granola",
        native_id="n11",
        title="raw",
        date=_date(2026, 7, 1),
        text="Ops: shipping the migration.",
    )
    insight = Insight(
        headline="Migration plan",
        summary="Shipping it.",
        key_points=[point],
        action_items=[item],
        people=["Ops"],
    )
    p = write(
        cfg.vault.insights_path / "2026-07-01 migration.md",
        writer.render_note(transcript, insight),
    )

    rec = indexer.parse_note(p)
    assert rec is not None
    assert rec.action_items == [
        "Ship the migration: run retitle_notes.py then backfill"
    ]
    assert rec.open_action_items == rec.action_items
    # The injected heading never becomes a real one, so the transcript survives.
    assert rec.transcript_text == "Ops: shipping the migration."
    headings = [
        ln
        for ln in p.read_text(encoding="utf-8").splitlines()
        if ln.strip().lower() == "## transcript"
    ]
    assert len(headings) == 1
