"""The timestamp backfill rewrites the ## Transcript section in place."""
import importlib.util
from pathlib import Path

import pytest

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


HEAD = "# A note\n\n## Summary\nSomething happened.\n\n"
CALLOUT = "> [!note]- Full transcript\n> old untimed text\n"
USER_CALLOUT = "> [!tip] My own note\n> remember to follow up"
USER_SECTION = "## My follow-up\nNotes I added by hand in Obsidian after the fact."

# (case, note, text the owner must keep byte-identical)
SHAPES = [
    ("heading then callout", HEAD + "## Transcript\n" + CALLOUT, ""),
    ("heading, blank, callout", HEAD + "## Transcript\n\n" + CALLOUT, ""),
    ("heading, two blanks, callout", HEAD + "## Transcript\n\n\n" + CALLOUT, ""),
    (
        "user callout below",
        HEAD + "## Transcript\n" + CALLOUT + "\n" + USER_CALLOUT + "\n",
        USER_CALLOUT,
    ),
    (
        "user section below",
        HEAD + "## Transcript\n" + CALLOUT + "\n" + USER_SECTION + "\n",
        USER_SECTION,
    ),
    (
        "blank-separated callout, user callout below",
        HEAD + "## Transcript\n\n" + CALLOUT + "\n" + USER_CALLOUT + "\n",
        USER_CALLOUT,
    ),
    ("lowercase heading", HEAD + "## transcript\n" + CALLOUT, ""),
    ("indented heading", HEAD + "  ## Transcript\n" + CALLOUT, ""),
    ("heading with trailing space", HEAD + "## Transcript  \n" + CALLOUT, ""),
    ("no transcript section", HEAD.rstrip("\n") + "\n", ""),
]


@pytest.mark.parametrize(
    "note,keep", [(n, k) for _name, n, k in SHAPES], ids=[s[0] for s in SHAPES]
)
def test_every_note_shape_ends_with_exactly_one_transcript(note, keep):
    """One grammar, one outcome: the heading + its callout are replaced, and
    nothing the owner wrote is touched — whatever shape the note is in."""
    out = backfill_timestamps._replace_transcript_section(note, "[0:00] Hi")

    headings = [ln for ln in out.splitlines() if ln.strip().lower() == "## transcript"]
    callouts = [ln for ln in out.splitlines() if ln.strip() == "> [!note]- Full transcript"]
    assert len(headings) == 1
    assert len(callouts) == 1
    assert "> [0:00] Hi" in out
    assert "old untimed text" not in out
    assert "Something happened." in out
    if keep:
        assert keep in out
        assert out.index("> [0:00] Hi") < out.index(keep)


@pytest.mark.parametrize(
    "note", [n for _name, n, _k in SHAPES], ids=[s[0] for s in SHAPES]
)
def test_replacing_twice_is_idempotent(note):
    """A second migration pass (--force) must not stack up more callouts."""
    once = backfill_timestamps._replace_transcript_section(note, "[0:00] Hi")
    twice = backfill_timestamps._replace_transcript_section(once, "[0:00] Hi")
    assert twice == once


def test_owner_callout_below_the_transcript_is_not_indexed_as_transcript(cfg):
    """End-to-end shape the migration now produces: the owner's own callout
    survives below the transcript, so the indexer must stop where the backfill
    stopped — otherwise their private follow-up is published in the /transcript
    view and the RAG corpus as transcript content."""
    from datetime import date

    from transcript_analyzer.db import get_conn, get_transcript
    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer
    from transcript_analyzer.pipeline.indexer import index_note

    transcript = Transcript(
        id="bf1",
        source="granola",
        native_id="bn1",
        title="raw",
        date=date(2026, 7, 1),
        text="Angela: untimed original.",
    )
    note = writer.write_note(cfg, transcript, Insight(headline="Pricing chat"))
    note.write_text(
        note.read_text(encoding="utf-8").rstrip("\n")
        + "\n\n> [!tip] My own note\n> remember to follow up\n",
        encoding="utf-8",
    )

    import frontmatter

    post = frontmatter.load(str(note))
    post.content = backfill_timestamps._replace_transcript_section(
        post.content, "[0:00] Angela: timed line.\n\n[0:30] Angela: after a pause."
    )
    dumped = frontmatter.dumps(post)
    note.write_text(dumped if dumped.endswith("\n") else dumped + "\n", encoding="utf-8")

    assert index_note(cfg, note) is not None
    with get_conn(cfg.db_path) as conn:
        rec = get_transcript(conn, transcript.id)

    assert rec is not None
    assert rec.transcript_text == (
        "[0:00] Angela: timed line.\n\n[0:30] Angela: after a pause."
    )
    assert "remember to follow up" not in rec.transcript_text
    # And the owner's callout is still in the note itself.
    assert "> [!tip] My own note" in note.read_text(encoding="utf-8")


def test_already_timed_is_not_faked_by_an_owner_callout(cfg):
    """_already_timed reads through the indexer, so a '[0:00]'-shaped line in
    the owner's own callout must not make an untimed note look migrated."""
    from transcript_analyzer.pipeline.indexer import _extract_transcript

    note = (
        "## Transcript\n"
        "> [!note]- Full transcript\n"
        "> Angela: untimed original.\n"
        "\n"
        "> [!tip] My own note\n"
        "> [0:00] looks like a timestamp\n"
    )
    assert _extract_transcript(note) == "Angela: untimed original."
    assert not backfill_timestamps._already_timed(_extract_transcript(note))
