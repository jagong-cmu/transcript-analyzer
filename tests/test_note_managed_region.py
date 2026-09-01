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


def test_a_transcript_that_quotes_the_end_marker_does_not_split_the_note(cfg):
    """The marker is a LINE, not a substring anywhere in the file.

    The callout writes every transcript line as '> …', so a recording that
    mentions the marker text used to be found before the real marker: the
    splice started inside the transcript and duplicated the whole managed
    region on every regeneration.
    """
    spoken = f"[0:01] Angela: the note ends at {writer.NOTE_END} apparently."
    path = writer.write_note(cfg, transcript(spoken), insight())
    path.write_text(path.read_text() + "\n## My own notes\nKept.\n", encoding="utf-8")

    writer.write_note(cfg, transcript(spoken), insight())
    once = path.read_text()
    writer.write_note(cfg, transcript(spoken), insight())
    twice = path.read_text()

    assert once == twice, "the note is not byte-stable across regenerations"
    assert once.count("apparently.") == 1, "the transcript was spliced into the tail"
    assert once.count(writer.NOTE_BEGIN) == 1
    assert once.count("Kept.") == 1
    assert indexer.parse_note(path).transcript_text == spoken


# ---------- study notes an earlier run left are carried across too ----------


def with_study(cfg, path, *, pdf=True, repairs=True):
    """Regenerate `path` as a lecture that produced study notes and a PDF."""
    study = writer.write_study_note(cfg, path, TID, "study notes body")
    if pdf:
        writer.write_study_pdf(study, TID, b"%PDF-1.4 rendered")
    from transcript_analyzer.models import AsrRepair

    return writer.write_note(
        cfg, transcript(), insight(), path=path,
        study_stem_name=study.stem,
        has_study_pdf=pdf,
        asr_repairs=[AsrRepair(heard="new age of AR", corrected="new age of AI")]
        if repairs else [],
    )


def test_a_run_with_no_lecture_pass_keeps_the_study_notes_already_on_disk(cfg):
    """The dashboard still serves them; the note must not claim they are gone.

    `[lecture] enabled = false`, a contained failure, or simply a re-sync all
    reach `write_note` with no study notes — but the markdown and the PDF are
    still in the vault, still provably this transcript's.
    """
    path = cfg.vault.insights_path / "2026-09-01 lecture.md"
    writer.write_note(cfg, transcript(), insight(), path=path)
    with_study(cfg, path)

    # A later regeneration where the lecture pass produced nothing at all.
    writer.write_note(cfg, transcript("[0:01] Angela: new words."), insight(), path=path)

    text = path.read_text()
    stem = writer.study_note_path_for(cfg, path).stem
    assert f"[[{stem}|Full study notes]]" in text
    assert f"[[{stem}.pdf|Printable PDF]]" in text
    assert f'study_notes: "{stem}"' in text
    # The audit trail for those notes survives with them.
    assert "new age of AR" in text and "new age of AI" in text


def test_the_carried_pdf_link_still_tracks_whether_the_pdf_exists(cfg):
    """Carrying the link must not resurrect a download the vault cannot serve."""
    path = cfg.vault.insights_path / "2026-09-01 lecture.md"
    writer.write_note(cfg, transcript(), insight(), path=path)
    with_study(cfg, path, pdf=False)

    writer.write_note(cfg, transcript("[0:01] Angela: new words."), insight(), path=path)

    text = path.read_text()
    assert "|Full study notes]]" in text
    assert "Printable PDF" not in text


def test_no_study_notes_on_disk_invents_no_link(cfg):
    path = cfg.vault.insights_path / "2026-09-01 plain.md"
    writer.write_note(cfg, transcript(), insight(), path=path)
    writer.write_note(cfg, transcript("[0:01] Angela: new words."), insight(), path=path)

    text = path.read_text()
    assert "## Study Notes" not in text and "study_notes:" not in text


def test_study_notes_that_are_not_ours_are_never_linked(cfg):
    """Ownership gates the carry-across exactly as it gates the tail."""
    path = cfg.vault.insights_path / "2026-09-01 lecture.md"
    writer.write_note(cfg, transcript(), insight(), path=path)
    stranger = writer.study_note_path_for(cfg, path)
    stranger.parent.mkdir(parents=True, exist_ok=True)
    stranger.write_text(
        "---\nsynth: true\ntranscript_id: someone-else\n---\n\n# Theirs\n",
        encoding="utf-8",
    )
    writer.study_pdf_for(stranger).write_bytes(b"%PDF theirs")

    writer.write_note(cfg, transcript("[0:01] Angela: new words."), insight(), path=path)

    text = path.read_text()
    assert "## Study Notes" not in text and "study_notes:" not in text
    assert "Theirs" in stranger.read_text()


def test_a_fresh_lecture_pass_replaces_the_carried_repairs(cfg):
    """Carrying across is a fallback, never an override of what this run made."""
    path = cfg.vault.insights_path / "2026-09-01 lecture.md"
    writer.write_note(cfg, transcript(), insight(), path=path)
    with_study(cfg, path)

    study = writer.study_note_path_for(cfg, path)
    writer.write_note(
        cfg, transcript(), insight(), path=path,
        study_stem_name=study.stem, has_study_pdf=True, asr_repairs=[],
    )

    text = path.read_text()
    assert "asr_repairs:" not in text
    assert f"[[{study.stem}|Full study notes]]" in text
