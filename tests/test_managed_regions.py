"""R9: regeneration must never clobber hand annotations, and synthesis must
be unable to write outside its namespaces."""
import pytest

from transcript_analyzer.obsidian.writer import SYNTH_BEGIN, SYNTH_END, write_managed


def test_create_and_regenerate(cfg):
    path = cfg.vault.insights_path / "Digests" / "2026-07-01.md"
    write_managed(cfg, path, "first version", title="Digest")
    text = path.read_text()
    assert "first version" in text and SYNTH_BEGIN in text and "synth: true" in text

    write_managed(cfg, path, "second version")
    text = path.read_text()
    assert "second version" in text and "first version" not in text
    assert text.count(SYNTH_BEGIN) == 1  # idempotent, no duplicate regions


def test_user_edits_outside_region_survive(cfg):
    path = cfg.vault.insights_path / "People" / "Angela.md"
    write_managed(cfg, path, "generated v1", title="Angela")
    original = path.read_text()
    annotated = original.replace(
        SYNTH_END, SYNTH_END + "\n\n## My own notes\nShe prefers async.\n"
    )
    path.write_text(annotated)

    write_managed(cfg, path, "generated v2")
    text = path.read_text()
    assert "generated v2" in text
    assert "She prefers async." in text  # the hand annotation survived


def test_missing_markers_appends_not_clobbers(cfg):
    path = cfg.vault.insights_path / "Studies" / "Airport.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Handwritten study page\nimportant\n")
    write_managed(cfg, path, "generated")
    text = path.read_text()
    assert "important" in text and "generated" in text


def test_namespace_isolation(cfg):
    with pytest.raises(ValueError, match="synthesis may only write"):
        write_managed(cfg, cfg.vault.insights_path / "2026-07-01 note.md", "x")
    with pytest.raises(ValueError):
        write_managed(cfg, cfg.vault.path / "Elsewhere" / "x.md", "x")
