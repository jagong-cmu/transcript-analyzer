"""A transcript note is regenerated, not overwritten.

Before the markers, re-running a transcript rewrote the whole file: anything
the vault owner had typed into the note in Obsidian was gone, and every box
they had ticked reopened. Both are hand edits the system asks for — ticking a
checkbox is how a commitment is closed.
"""
from datetime import date

from transcript_analyzer.models import Insight, Transcript
from transcript_analyzer.obsidian import writer
from transcript_analyzer.pipeline import indexer

TID = "t-managed"


def transcript(text="[0:01] Angela: I will send the deck.") -> Transcript:
    return Transcript(
        id=TID, source="pocket", native_id="n1", title="raw",
        date=date(2026, 9, 1), text=text,
    )


def insight(**over) -> Insight:
    base = dict(
        headline="Pricing deck review",
        summary="Angela will send the deck.",
        detailed_summary="Angela agreed to send the deck this week.",
        action_items=["Send the pricing deck", "Book the follow-up"],
    )
    base.update(over)
    return Insight(**base)


def test_the_generated_region_is_marked(cfg):
    path = writer.write_note(cfg, transcript(), insight())
    text = path.read_text()
    assert writer.NOTE_BEGIN in text and writer.NOTE_END in text
    assert text.index(writer.NOTE_BEGIN) < text.index("## Summary")
    assert text.rstrip().endswith(writer.NOTE_END)


def test_text_written_below_the_end_marker_survives_regeneration(cfg):
    path = writer.write_note(cfg, transcript(), insight())
    path.write_text(
        path.read_text() + "\n## My own notes\nAngela sounded unconvinced.\n",
        encoding="utf-8",
    )

    writer.write_note(cfg, transcript("[0:01] Angela: changed my mind."), insight())
    text = path.read_text()
    assert "Angela sounded unconvinced." in text
    assert "changed my mind." in text
    assert text.count(writer.NOTE_BEGIN) == 1


def test_a_ticked_box_stays_ticked_across_a_regeneration(cfg):
    """The note is the source of truth; ticking closes the commitment."""
    path = writer.write_note(cfg, transcript(), insight())
    path.write_text(
        path.read_text().replace("- [ ] Send the pricing deck", "- [x] Send the pricing deck"),
        encoding="utf-8",
    )

    writer.write_note(cfg, transcript("[0:01] Angela: new words."), insight())
    text = path.read_text()
    assert "- [x] Send the pricing deck" in text
    assert "- [ ] Book the follow-up" in text

    rec = indexer.parse_note(path)
    assert rec.open_action_items == ["Book the follow-up"]


def test_an_action_item_that_no_longer_exists_does_not_come_back(cfg):
    path = writer.write_note(cfg, transcript(), insight())
    path.write_text(
        path.read_text().replace("- [ ] Send the pricing deck", "- [x] Send the pricing deck"),
        encoding="utf-8",
    )
    writer.write_note(cfg, transcript(), insight(action_items=["Book the follow-up"]))
    text = path.read_text()
    assert "Send the pricing deck" not in text


def test_a_note_written_before_the_markers_keeps_its_appended_text(cfg):
    """Legacy notes have no end marker; the transcript callout's end is the
    boundary, read through the one definition the indexer uses."""
    path = cfg.vault.insights_path / "2026-09-01 legacy.md"
    path.write_text(
        f"---\nsource: pocket\ndate: 2026-09-01\ntranscript_id: {TID}\n---\n\n"
        "# Legacy note\n\n## Summary\nOld and short.\n\n"
        "## Transcript\n> [!note]- Full transcript\n> [0:01] Angela: old words.\n\n"
        "## My own notes\nI added this by hand.\n",
        encoding="utf-8",
    )

    writer.write_note(cfg, transcript(), insight(), path=path)
    text = path.read_text()
    assert "I added this by hand." in text
    assert "Angela agreed to send the deck this week." in text
    assert "old words." not in text  # the generated region really was replaced


def test_a_stranger_s_note_is_never_read_for_its_content(cfg):
    """Ownership gates the read too: we do not merge a file that is not ours."""
    path = cfg.vault.insights_path / "2026-09-01 taken.md"
    path.write_text(
        "---\nsource: pocket\ndate: 2026-09-01\ntranscript_id: someone-else\n---\n\n"
        "# Theirs\n\n## Action Items\n- [x] Their closed item\n",
        encoding="utf-8",
    )

    written = writer.write_note(cfg, transcript(), insight(), path=path)
    assert written != path
    assert "Their closed item" in path.read_text()
    assert "Their closed item" not in written.read_text()


def test_the_transcript_still_round_trips_through_the_markers(cfg):
    text = "[0:01] Angela: first line.\n\n[0:09] Angela: after a blank line."
    path = writer.write_note(cfg, transcript(text), insight())
    assert indexer.parse_note(path).transcript_text == text


def test_repeated_regeneration_does_not_grow_the_gap(cfg):
    """The tail's leading blank line is a separator, not content: returning it
    and adding one back grew the note by a line on every single sync."""
    path = writer.write_note(cfg, transcript(), insight())
    path.write_text(path.read_text() + "\n## Mine\nKept.\n", encoding="utf-8")

    shapes = set()
    for i in range(4):
        writer.write_note(cfg, transcript(f"[0:0{i}] words {i}."), insight())
        text = path.read_text()
        shapes.add(text[text.index(writer.NOTE_END):])
    assert len(shapes) == 1, "the note is not byte-stable across regenerations"
    assert path.read_text().count("Kept.") == 1
