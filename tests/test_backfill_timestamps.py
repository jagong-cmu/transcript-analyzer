"""The timestamp backfill rewrites the ## Transcript section in place."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backfill_timestamps",
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_timestamps.py",
)
backfill_timestamps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill_timestamps)

NOTE = """# A note

## Summary
Something happened.

## Transcript
> [!note]- Full transcript
> old untimed text
"""


def test_replaces_the_transcript_section():
    out = backfill_timestamps._replace_transcript_section(NOTE, "[0:00] Hi")
    assert "old untimed text" not in out
    assert "> [0:00] Hi" in out
    assert "Something happened." in out


def test_transcript_text_is_never_a_regex_template():
    """Speech-to-text can emit backslashes; as a re.sub template they raised
    'bad escape' (and \\1 would have expanded a capture group)."""
    timed = "[0:00] Path is C:\\Users\\bob and group \\1"
    out = backfill_timestamps._replace_transcript_section(NOTE, timed)
    assert "C:\\Users\\bob" in out
    assert "\\1" in out


def test_appends_when_there_is_no_transcript_section():
    out = backfill_timestamps._replace_transcript_section("# A note\n", "[0:00] Hi")
    assert out.startswith("# A note")
    assert "## Transcript" in out
    assert "> [0:00] Hi" in out


def test_already_timed_detection():
    assert backfill_timestamps._already_timed("[0:00] Hi\n[1:23] There")
    assert backfill_timestamps._already_timed("plain\n[12:05] later")
    assert not backfill_timestamps._already_timed("plain text only")
    assert not backfill_timestamps._already_timed("")


NOTE_WITH_TRAILING_CONTENT = """# A note

## Summary
Something happened.

## Transcript
> [!note]- Full transcript
> old untimed text

## My follow-up
Notes I added by hand in Obsidian after the fact.
"""


def test_content_after_the_transcript_callout_survives():
    """The vault is the source of truth: matching to end-of-file discarded
    everything the owner had appended below the transcript."""
    out = backfill_timestamps._replace_transcript_section(
        NOTE_WITH_TRAILING_CONTENT, "[0:00] Hi"
    )
    assert "> [0:00] Hi" in out
    assert "old untimed text" not in out
    assert "## My follow-up" in out
    assert "Notes I added by hand in Obsidian after the fact." in out
    # And the appended section stays below the rewritten callout.
    assert out.index("> [0:00] Hi") < out.index("## My follow-up")


NOTE_WITH_TRAILING_CALLOUT = """# A note

## Summary
Something happened.

## Transcript
> [!note]- Full transcript
> old untimed text

> [!tip] My own note
> remember to follow up
"""


def test_a_blockquote_appended_below_the_transcript_survives():
    """Scanning across the blank line swallowed the owner's own callout: the
    generated callout never contains a truly blank line (_quote_block writes
    every transcript line as '> '), so the first one ends the region."""
    out = backfill_timestamps._replace_transcript_section(
        NOTE_WITH_TRAILING_CALLOUT, "[0:00] Hi"
    )
    assert "> [0:00] Hi" in out
    assert "old untimed text" not in out
    assert "> [!tip] My own note" in out
    assert "> remember to follow up" in out
    assert out.index("> [0:00] Hi") < out.index("> [!tip] My own note")


def test_a_blank_line_inside_the_transcript_is_preserved():
    """An empty transcript line is emitted as '> ', so it does not terminate
    the callout and the whole transcript is still replaced."""
    out = backfill_timestamps._replace_transcript_section(
        NOTE_WITH_TRAILING_CALLOUT, "[0:00] Hi\n\n[0:30] Still here"
    )
    assert "> [0:00] Hi" in out
    assert "> [0:30] Still here" in out
    assert "> [!tip] My own note" in out


def test_lowercase_or_indented_heading_is_replaced_not_duplicated():
    """_already_timed reads the transcript through the indexer's lenient
    heading rule, so a stricter rule here appended a SECOND transcript."""
    for heading in ("## transcript", "  ## Transcript", "## Transcript  "):
        note = NOTE.replace("## Transcript", heading)
        out = backfill_timestamps._replace_transcript_section(note, "[0:00] Hi")
        headings = [
            ln for ln in out.splitlines() if ln.strip().lower() == "## transcript"
        ]
        assert len(headings) == 1, f"{heading!r} produced {len(headings)} sections"
        assert "old untimed text" not in out
        assert "> [0:00] Hi" in out


def test_replaced_transcript_is_what_the_indexer_reads_back():
    """The rewritten section has to survive the round trip the dashboard uses."""
    from transcript_analyzer.pipeline.indexer import _extract_transcript

    out = backfill_timestamps._replace_transcript_section(
        NOTE, "[0:00] Hi\n\n[0:30] Bye"
    )
    assert _extract_transcript(out) == "[0:00] Hi\n\n[0:30] Bye"
