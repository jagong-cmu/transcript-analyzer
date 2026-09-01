"""Study notes and their PDF obey the same ownership invariant as everything else.

The vault has no backup. A study stem names two files — the markdown and the
PDF — and only the markdown can carry `transcript_id`, so the PDF is claimed
through the note beside it, exactly the way an mp3 in Attachments/ is claimed
through the note at its stem. Nothing here may write, move, or replace a file
it cannot prove is this transcript's.
"""
import pytest

from transcript_analyzer.obsidian import writer
from transcript_analyzer.pipeline import indexer

OURS = "t-ours"
THEIRS = "t-theirs"


def study_base(cfg, stem="2026-09-01 lecture"):
    return writer.study_note_for(cfg.vault.insights_path, stem)


def foreign_note(path, tid=THEIRS):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nsynth: true\ntranscript_id: {tid}\n---\n\n# Someone else's\n",
        encoding="utf-8",
    )
    return path


def test_the_namespace_is_excluded_from_the_index_and_allowed_for_writes():
    """A namespace in one list but not the other breaks the feedback-loop guard."""
    assert writer.STUDY_SUBDIR in writer.SYNTH_SUBDIRS
    assert writer.STUDY_SUBDIR in indexer.EXCLUDED_SUBDIRS


def test_study_notes_never_share_a_stem_with_the_transcript_note():
    """Two vault files with one name make every wikilink to it ambiguous."""
    assert writer.study_stem("2026-09-01 lecture") != "2026-09-01 lecture"


def test_a_free_stem_is_claimable(cfg):
    base = study_base(cfg)
    assert writer.claimable_study_stem(base, OURS)
    assert writer.claim_study_path(base, OURS) == base


def test_a_stem_holding_someone_elses_note_is_not_ours(cfg):
    base = study_base(cfg)
    foreign_note(base)
    assert not writer.claimable_study_stem(base, OURS)
    claimed = writer.claim_study_path(base, OURS)
    assert claimed != base
    assert claimed.name.startswith("2026-09-01 lecture (study notes) (")
    # And the stranger's file is untouched.
    assert "Someone else's" in base.read_text()


def test_a_stem_holding_an_unclaimed_pdf_is_not_ours(cfg):
    """A PDF with no study note beside it is somebody's file, not ours."""
    base = study_base(cfg)
    pdf = writer.study_pdf_for(base)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 not ours")
    assert not writer.claimable_study_stem(base, OURS)
    assert writer.claim_study_path(base, OURS) != base


def test_our_own_stem_is_reclaimed_not_suffixed(cfg):
    """Regeneration is idempotent: the same lecture keeps the same filename."""
    base = study_base(cfg)
    foreign_note(base, tid=OURS)
    assert writer.claimable_study_stem(base, OURS)
    assert writer.claim_study_path(base, OURS) == base


def test_write_study_note_stamps_the_id_and_is_idempotent(cfg):
    note = cfg.vault.insights_path / "2026-09-01 lecture.md"
    first = writer.write_study_note(cfg, note, OURS, "v1", title="Lecture")
    assert f"transcript_id: {OURS}" in first.read_text()
    second = writer.write_study_note(cfg, note, OURS, "v2", title="Lecture")
    assert second == first
    text = first.read_text()
    assert "v2" in text and "v1" not in text
    assert text.count(writer.SYNTH_BEGIN) == 1


def test_hand_annotations_on_a_study_note_survive_regeneration(cfg):
    note = cfg.vault.insights_path / "2026-09-01 lecture.md"
    path = writer.write_study_note(cfg, note, OURS, "generated v1")
    path.write_text(
        path.read_text().replace(
            writer.SYNTH_END, writer.SYNTH_END + "\n\n## My own working\nI got 3.\n"
        ),
        encoding="utf-8",
    )
    writer.write_study_note(cfg, note, OURS, "generated v2")
    text = path.read_text()
    assert "generated v2" in text and "I got 3." in text


def test_write_managed_refuses_a_file_that_is_not_the_claimed_transcripts(cfg):
    """The per-transcript namespace proves ownership per FILE, not per folder."""
    path = cfg.vault.insights_path / writer.STUDY_SUBDIR / "x.md"
    foreign_note(path)
    with pytest.raises(ValueError, match="does not prove it belongs"):
        writer.write_managed(cfg, path, "mine", transcript_id=OURS)
    assert "Someone else's" in path.read_text()


def test_corpus_wide_synthesis_notes_are_unaffected_by_the_id_check(cfg):
    """A digest or dossier carries no transcript_id and must still regenerate."""
    path = cfg.vault.insights_path / "Digests" / "2026-09-01.md"
    writer.write_managed(cfg, path, "v1", title="Digest")
    writer.write_managed(cfg, path, "v2")
    assert "v2" in path.read_text()


def test_the_pdf_is_only_written_at_a_stem_whose_note_proves_it_is_ours(cfg):
    note = cfg.vault.insights_path / "2026-09-01 lecture.md"
    study = writer.write_study_note(cfg, note, OURS, "notes")
    written = writer.write_study_pdf(study, OURS, b"%PDF-1.4 ours")
    assert written is not None and written.read_bytes() == b"%PDF-1.4 ours"

    # A study note belonging to another transcript claims that PDF, not us.
    assert writer.write_study_pdf(study, THEIRS, b"%PDF-1.4 stolen") is None
    assert written.read_bytes() == b"%PDF-1.4 ours"


def test_a_pdf_with_no_study_note_beside_it_is_never_replaced(cfg):
    base = study_base(cfg)
    pdf = writer.study_pdf_for(base)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF someone else")
    assert writer.write_study_pdf(base, OURS, b"%PDF ours") is None
    assert pdf.read_bytes() == b"%PDF someone else"


def test_study_notes_follow_the_note_when_it_is_renamed(cfg):
    old_note = cfg.vault.insights_path / "2026-09-01 old name.md"
    new_note = cfg.vault.insights_path / "2026-09-01 new name.md"
    study = writer.write_study_note(cfg, old_note, OURS, "notes")
    writer.write_study_pdf(study, OURS, b"%PDF ours")

    moved = writer.move_study_with_note(cfg, old_note, new_note, OURS)
    assert moved == writer.study_note_path_for(cfg, new_note)
    assert moved.exists() and not study.exists()
    assert writer.study_pdf_for(moved).read_bytes() == b"%PDF ours"
    assert not writer.study_pdf_for(study).exists()


def test_a_move_is_refused_when_the_source_is_not_ours(cfg):
    old_note = cfg.vault.insights_path / "2026-09-01 old name.md"
    new_note = cfg.vault.insights_path / "2026-09-01 new name.md"
    foreign = foreign_note(writer.study_note_path_for(cfg, old_note))

    assert writer.move_study_with_note(cfg, old_note, new_note, OURS) is None
    assert foreign.exists()  # left where it is, as an orphan to clean up by hand
    assert not writer.study_note_path_for(cfg, new_note).exists()


def test_a_move_is_refused_when_the_destination_belongs_to_someone_else(cfg):
    old_note = cfg.vault.insights_path / "2026-09-01 old name.md"
    new_note = cfg.vault.insights_path / "2026-09-01 new name.md"
    ours = writer.write_study_note(cfg, old_note, OURS, "notes")
    theirs = foreign_note(writer.study_note_path_for(cfg, new_note))

    assert writer.move_study_with_note(cfg, old_note, new_note, OURS) is None
    assert ours.exists() and "Someone else's" in theirs.read_text()


def test_moving_onto_our_own_destination_replaces_it(cfg):
    """A retitle that lands where an earlier run left our own notes is fine."""
    old_note = cfg.vault.insights_path / "2026-09-01 old name.md"
    new_note = cfg.vault.insights_path / "2026-09-01 new name.md"
    writer.write_study_note(cfg, old_note, OURS, "current")
    writer.write_study_note(cfg, new_note, OURS, "stale")

    moved = writer.move_study_with_note(cfg, old_note, new_note, OURS)
    assert moved is not None and "current" in moved.read_text()
