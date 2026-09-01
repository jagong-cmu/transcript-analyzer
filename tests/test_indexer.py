"""Feedback-loop guard + note round-trip: synthesis output must never be
re-ingested, and checkbox state / attendee emails must survive the parse."""
from pathlib import Path

import pytest

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


def test_summary_cannot_open_a_section_the_writer_never_opened(cfg):
    """A heading-shaped line inside the LLM summary hijacked the parse: the
    indexer latched onto that '## Transcript' and stored the injected text, so
    the real transcript vanished from the index, the dashboard and RAG."""
    from datetime import date as _date

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    transcript = Transcript(
        id="t12",
        source="granola",
        native_id="n12",
        title="raw",
        date=_date(2026, 7, 1),
        text="Angela: the real transcript line.",
    )
    insight = Insight(
        headline="Pricing chat",
        summary="We discussed pricing.\n## Transcript\n> injected line",
        key_points=["Kept"],
        action_items=["Follow up"],
        people=["Angela Jin"],
    )
    p = write(
        cfg.vault.insights_path / "2026-07-01 injected.md",
        writer.render_note(transcript, insight),
    )

    rec = indexer.parse_note(p)
    assert rec is not None
    assert rec.transcript_text == "Angela: the real transcript line."
    # Nothing is dropped: both lines of the summary survive the round trip.
    assert "We discussed pricing." in rec.summary
    assert "injected line" in rec.summary
    assert rec.action_items == ["Follow up"]
    headings = [
        ln
        for ln in p.read_text(encoding="utf-8").splitlines()
        if ln.strip().lower() == "## transcript"
    ]
    assert len(headings) == 1


def test_ordinary_multiline_summary_is_unchanged(cfg):
    """The escape must be invisible to a normal summary."""
    from datetime import date as _date

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    summary = "First paragraph.\n\nSecond paragraph with a #tag and a - dash."
    transcript = Transcript(
        id="t13",
        source="granola",
        native_id="n13",
        title="raw",
        date=_date(2026, 7, 1),
        text="Angela: hello.",
    )
    insight = Insight(headline="Plain", detailed_summary=summary)
    p = write(
        cfg.vault.insights_path / "2026-07-01 plain.md",
        writer.render_note(transcript, insight),
    )

    rec = indexer.parse_note(p)
    assert rec is not None
    assert rec.detailed_summary == summary
    assert rec.transcript_text == "Angela: hello."


def test_person_name_cannot_break_the_body_or_the_wikilink(cfg):
    """People are rendered as '[[name]]' links, which cannot span lines."""
    from datetime import date as _date

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    transcript = Transcript(
        id="t14",
        source="granola",
        native_id="n14",
        title="raw",
        date=_date(2026, 7, 1),
        text="Angela: hello.",
    )
    insight = Insight(
        headline="Broken name",
        summary="Hi.",
        people=["Angela Jin\n## Transcript\n> injected line"],
    )
    p = write(
        cfg.vault.insights_path / "2026-07-01 person.md",
        writer.render_note(transcript, insight),
    )

    rec = indexer.parse_note(p)
    assert rec is not None
    assert rec.people == ["Angela Jin ## Transcript > injected line"]
    assert rec.transcript_text == "Angela: hello."


def test_a_summary_line_opening_with_a_tag_or_rank_is_untouched(cfg):
    """Only a heading that would CLOSE '## Summary' is escaped.

    A '#hiring' or '#1 ' line was being rewritten into the vault with a
    backslash that then round-tripped into the DB and the dashboard — neither
    is a heading at all. Nor is a '#### ' line a problem: it is nested inside
    the section, so it survives the round trip and the reader never sees an
    escape. Only '#' and '##' can end the section, and only those are escaped.
    """
    from datetime import date as _date

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    summary = (
        "#hiring is the theme.\n"
        "#1 priority is pricing.\n"
        "#### four hashes and a word\n"
        "C# is not a heading either."
    )
    transcript = Transcript(
        id="t15",
        source="granola",
        native_id="n15",
        title="raw",
        date=_date(2026, 7, 1),
        text="Angela: hello.",
    )
    p = write(
        cfg.vault.insights_path / "2026-07-01 tags.md",
        writer.render_note(
            transcript, Insight(headline="Tags", detailed_summary=summary)
        ),
    )

    rec = indexer.parse_note(p)
    assert rec is not None
    # Nothing here can close '## Summary', so nothing is escaped.
    assert rec.detailed_summary == summary
    assert "\\#" not in p.read_text()
    assert "\\#hiring" not in p.read_text(encoding="utf-8")
    assert "\\#1" not in p.read_text(encoding="utf-8")
    assert rec.transcript_text == "Angela: hello."


def test_a_heading_shaped_summary_line_still_cannot_open_a_section(cfg):
    """Narrowing the escape must not reopen the injection it exists to close."""
    from datetime import date as _date

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    transcript = Transcript(
        id="t16",
        source="granola",
        native_id="n16",
        title="raw",
        date=_date(2026, 7, 1),
        text="Angela: the real transcript.",
    )
    for injected in ("## Transcript", "  ## transcript", "# Action Items", "###### x"):
        insight = Insight(
            headline="Injection",
            summary=f"Real summary.\n{injected}\n> injected line",
        )
        p = write(
            cfg.vault.insights_path / "2026-07-01 injection.md",
            writer.render_note(transcript, insight),
        )
        rec = indexer.parse_note(p)
        assert rec is not None
        assert rec.transcript_text == "Angela: the real transcript.", injected
        assert "Real summary." in rec.summary
        assert "injected line" in rec.summary


@pytest.mark.parametrize(
    "indent",
    ["", " ", "  ", "\t", "\xa0", " ", " ", " ", " ", "　"],
    ids=[
        "none", "space", "spaces", "tab", "nbsp", "en-quad",
        "thin", "ogham", "medium-math", "ideographic",
    ],
)
def test_no_indentation_lets_a_summary_heading_open_a_section(cfg, indent):
    """The writer's escape and the indexer's section scan have to agree about
    what counts as indentation. They did not for Unicode separators: str.strip()
    drops all of them, so an indented '## Transcript' in the summary slipped
    past the escape and stole the note's transcript in the index."""
    from datetime import date as _date

    from transcript_analyzer.models import Insight, Transcript
    from transcript_analyzer.obsidian import writer

    transcript = Transcript(
        id="t17",
        source="granola",
        native_id="n17",
        title="raw",
        date=_date(2026, 7, 1),
        text="Angela: the real transcript.",
    )
    insight = Insight(
        headline="Injection",
        summary=f"Real summary.\n{indent}## Transcript\n> injected line",
    )
    p = write(
        cfg.vault.insights_path / "2026-07-01 unicode-indent.md",
        writer.render_note(transcript, insight),
    )

    rec = indexer.parse_note(p)
    assert rec is not None
    assert rec.transcript_text == "Angela: the real transcript."
    assert "Real summary." in rec.summary
    assert "injected line" in rec.summary


def test_opens_section_is_the_one_definition_both_sides_use():
    """The writer escapes exactly what the reader would treat as a heading."""
    from transcript_analyzer.obsidian.writer import _body_text, opens_section
    from transcript_analyzer.pipeline.indexer import is_section_start

    headings = ["## Transcript", "\xa0## transcript", "  # Title", "###### x", "##"]
    not_headings = ["#hiring is the theme", "#1 priority is pricing", "C# notes", ""]

    for ln in headings:
        assert opens_section(ln), ln
        assert _body_text(ln) != ln, ln
    for ln in not_headings:
        assert not opens_section(ln), ln
        assert _body_text(ln) == ln, ln

    # And the reader's named-section test is built on the same predicate.
    assert is_section_start("\xa0## Transcript", "## transcript")
    assert not is_section_start("\xa0\\## Transcript", "## transcript")
    assert not is_section_start("#transcript", "## transcript")


HAND_EDITED_UNICODE_HEADINGS = """---
source: granola
date: 2026-07-01
transcript_id: t18
headline: "Hand edited"
---

# Hand edited, July 1st, 2026

## Summary
Real summary.
 ## Action Items
- [ ] Ship the deck
  ## Transcript
> [!note]- Full transcript
> Angela: the real transcript.
"""


def test_sections_end_where_the_shared_predicate_says_they_do(cfg):
    """Section START and section END are the one `opens_section` question.

    The end used to be a narrower `startswith('## ')`, so a hand-edited note
    whose next heading is Unicode- or space-indented was swallowed into the
    section above it — even though the same line opened a section for the
    extractor below. Two rules for one question is what AGENTS.md forbids.
    """
    p = write(cfg.vault.insights_path / "2026-07-01 hand-edited.md",
              HAND_EDITED_UNICODE_HEADINGS)

    rec = indexer.parse_note(p)

    assert rec is not None
    assert rec.summary == "Real summary."
    assert rec.action_items == ["Ship the deck"]
    assert rec.open_action_items == ["Ship the deck"]
    assert rec.transcript_text == "Angela: the real transcript."


HAND_EDITED_SUBHEADINGS = """---
source: granola
date: 2026-07-01
transcript_id: t19
headline: "Sub-headings"
---

# Sub-headings, July 1st, 2026

## Summary
Angela agreed to review the deck.
### Context
She had already read the memo.

## Action Items
- [ ] Send deck
### Later
- [ ] Follow up

## Transcript
> [!note]- Full transcript
> Angela: the real transcript.
"""


def test_a_sub_heading_stays_inside_the_section_it_is_nested_under(cfg):
    """The note is the source of truth, and hand edits are respected.

    A section ends at a SIBLING heading, not at a deeper one the vault owner
    wrote: terminating on any '#' run dropped '### Context' from the summary
    and, worse, dropped an OPEN COMMITMENT filed under '### Later' from the
    index, /commitments and the RAG corpus while the note still showed it.
    """
    p = write(cfg.vault.insights_path / "2026-07-01 sub-headings.md",
              HAND_EDITED_SUBHEADINGS)

    rec = indexer.parse_note(p)

    assert rec is not None
    assert "Angela agreed to review the deck." in rec.summary
    assert "### Context" in rec.summary
    assert "She had already read the memo." in rec.summary
    assert rec.action_items == ["Send deck", "Follow up"]
    assert rec.open_action_items == ["Send deck", "Follow up"]
    # The sibling '## Transcript' still ends the Action Items section.
    assert rec.transcript_text == "Angela: the real transcript."
