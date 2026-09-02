"""Two summaries, and only one of them may reach the corpus.

The note shows the long summary; the short abstract stays a frontmatter
retrieval field, because the dashboard Ask sends every abstract on every
question. Swapping them silently triples that corpus.
"""
from datetime import date

from transcript_analyzer.models import Insight, Transcript
from transcript_analyzer.obsidian import writer
from transcript_analyzer.pipeline import indexer, insights
from transcript_analyzer.titles import ABSTRACT_CHARS

LONG = (
    "### Opening\n\nThe class opened with a recap of last week.\n\n"
    "### The method\n\nThen the professor worked through the elimination steps."
)


def transcript(tid="t1") -> Transcript:
    return Transcript(
        id=tid, source="pocket", native_id="n1", title="raw",
        date=date(2026, 9, 1), text="[0:01] Professor: today we row reduce.",
    )


def write(cfg, insight, name="2026-09-01 note.md"):
    path = cfg.vault.insights_path / name
    path.write_text(writer.render_note(transcript(), insight), encoding="utf-8")
    return path


def test_the_note_body_shows_the_detailed_summary(cfg):
    path = write(
        cfg,
        Insight(headline="Row reduction", summary="One paragraph.", detailed_summary=LONG),
    )
    text = path.read_text()
    # A '###' nested under '## Summary' is inside the section, so it is NOT
    # escaped: the reader sees real structure, not backslashes.
    assert "## Summary\n### Opening" in text
    assert "\\###" not in text
    assert "Then the professor worked through the elimination steps." in text


def test_a_summary_heading_that_would_close_the_section_is_escaped(cfg):
    """A '##' in the summary would end '## Summary' and fake a new section."""
    path = write(
        cfg,
        Insight(headline="H", summary="a", detailed_summary="## Fake\ntext"),
        name="2026-09-01 escaped.md",
    )
    assert "\\## Fake" in path.read_text()
    rec = indexer.parse_note(path)
    assert "Fake" in rec.detailed_summary
    assert rec.transcript_text.startswith("[0:01]")


def test_the_abstract_lives_in_frontmatter_not_the_body(cfg):
    path = write(
        cfg,
        Insight(headline="Row reduction", summary="One paragraph.", detailed_summary=LONG),
    )
    head = path.read_text().split("---")[1]
    assert 'abstract: "One paragraph."' in head

    rec = indexer.parse_note(path)
    assert rec.summary == "One paragraph."          # the retrieval field
    assert "elimination steps" in rec.detailed_summary  # what the reader gets


def test_a_legacy_note_without_an_abstract_still_indexes(cfg):
    """Notes written before the split have a short body summary and no
    `abstract:`. That summary becomes both, so the corpus does not grow."""
    path = cfg.vault.insights_path / "2026-01-01 legacy.md"
    path.write_text(
        "---\nsource: pocket\ndate: 2026-01-01\ntranscript_id: old1\n"
        "headline: \"Old note\"\n---\n\n"
        "# Old note, January 1st, 2026\n\n## Summary\nAngela agreed to review the deck.\n",
        encoding="utf-8",
    )
    rec = indexer.parse_note(path)
    assert rec.summary == "Angela agreed to review the deck."
    assert rec.detailed_summary == "Angela agreed to review the deck."
    assert rec.kind == "meeting"


def test_a_legacy_fallback_abstract_is_bounded(cfg):
    """A long body summary contributes its opening paragraph, not itself:
    this field is sent for every conversation on every Ask question."""
    body = "First paragraph. " * 200
    path = cfg.vault.insights_path / "2026-01-02 long.md"
    path.write_text(
        "---\nsource: pocket\ndate: 2026-01-02\ntranscript_id: old2\n---\n\n"
        f"## Summary\n{body}\n\nSecond paragraph.\n",
        encoding="utf-8",
    )
    rec = indexer.parse_note(path)
    assert len(rec.summary) <= ABSTRACT_CHARS
    assert len(rec.detailed_summary) > len(rec.summary)


def test_kind_and_course_round_trip_through_the_note(cfg):
    insight = Insight(
        headline="Row reduction", summary="a", detailed_summary=LONG,
        kind="lecture", course_code="21-241", course_name="Linear Algebra",
    )
    rec = indexer.parse_note(write(cfg, insight))
    assert rec.kind == "lecture" and rec.is_lecture
    assert (rec.course_code, rec.course_name) == ("21-241", "Linear Algebra")


def test_an_unknown_kind_never_becomes_a_lecture(cfg):
    """A lecture buys an expensive study-notes pass; only the real word does."""
    path = cfg.vault.insights_path / "2026-01-03 odd.md"
    path.write_text(
        "---\nsource: pocket\ndate: 2026-01-03\ntranscript_id: odd1\n"
        "kind: \"seminar-ish\"\n---\n\n## Summary\nx\n",
        encoding="utf-8",
    )
    assert indexer.parse_note(path).kind == "meeting"


def test_extraction_payload_splits_the_two_summaries():
    insight = insights.insight_from_payload(
        {
            "title": "Row reducing a 3x3 matrix",
            "kind": "lecture",
            "course_code": "21-241",
            "course_name": "Linear Algebra",
            "abstract": "A linear algebra lecture on Gaussian elimination.",
            "detailed_summary": LONG,
            "key_points": ["Pivots come first"],
            "action_items": ["Homework 3 by Friday"],
            "people": [],
            "topics": ["Linear Algebra"],
            "sentiment": "neutral",
        },
        transcript(),
    )
    assert insight.summary == "A linear algebra lecture on Gaussian elimination."
    assert insight.detailed_summary == LONG
    assert insight.is_lecture and insight.course_code == "21-241"
    # key_points and action_items stay separate structured fields — the
    # commitment tracker is pure checkbox extraction off action_items.
    assert insight.action_items == ["Homework 3 by Friday"]
    assert insight.topics == ["linear algebra"]


def test_a_missing_abstract_falls_back_without_flooding_the_corpus():
    insight = insights.insight_from_payload(
        {"title": "t", "kind": "meeting", "course_code": "", "course_name": "",
         "abstract": "", "detailed_summary": LONG, "key_points": [],
         "action_items": [], "people": [], "topics": [], "sentiment": "neutral"},
        transcript(),
    )
    assert insight.summary == "The class opened with a recap of last week."
    assert insight.detailed_summary == LONG


def test_a_missing_detailed_summary_falls_back_to_the_abstract():
    insight = insights.insight_from_payload(
        {"title": "t", "kind": "meeting", "course_code": "", "course_name": "",
         "abstract": "Short.", "detailed_summary": "", "key_points": [],
         "action_items": [], "people": [], "topics": [], "sentiment": "neutral"},
        transcript(),
    )
    assert insight.detailed_summary == "Short."


def test_course_identity_is_not_recorded_for_a_non_lecture():
    insight = insights.insight_from_payload(
        {"title": "t", "kind": "meeting", "course_code": "21241",
         "course_name": "Linear Algebra", "abstract": "a", "detailed_summary": "b",
         "key_points": [], "action_items": [], "people": [], "topics": [],
         "sentiment": "neutral"},
        transcript(),
    )
    assert (insight.course_code, insight.course_name) == ("", "")


def test_a_model_abstract_that_ignores_the_length_rule_is_bounded(cfg):
    """The PRIMARY path is not trusted either, only the fallbacks were.

    Ask sends every abstract on every question, so a model that answers the
    "ONE paragraph, 2-4 sentences" field with three thousand words must not be
    able to inflate the corpus. The bound is enforced in code, not asked for
    in the prompt.
    """
    import frontmatter

    runaway = "The class covered a great many things. " * 200
    insight = insights.insight_from_payload(
        {
            "title": "Row reduction",
            "abstract": runaway,
            "detailed_summary": LONG,
            "key_points": ["A point"],
            "action_items": [],
            "people": [],
            "topics": [],
            "kind": "lecture",
        },
        transcript(),
    )

    assert len(runaway) > ABSTRACT_CHARS
    assert len(insight.summary) <= ABSTRACT_CHARS

    path = write(cfg, insight)
    # The note's frontmatter is the retrieval field's on-disk contract.
    assert len(str(frontmatter.load(str(path))["abstract"])) <= ABSTRACT_CHARS

    rec = indexer.parse_note(path)
    assert len(rec.summary) <= ABSTRACT_CHARS
    # And the long summary is untouched: only the retrieval field is bounded.
    assert "elimination steps" in rec.detailed_summary


def test_the_abstract_and_the_legacy_fallback_share_one_bound(cfg):
    """Two definitions drifting apart is how the corpus grew back before."""
    from transcript_analyzer.titles import retrieval_abstract

    long_detailed = "First paragraph. " * 200
    insight = insights.insight_from_payload(
        {"title": "T", "abstract": "", "detailed_summary": long_detailed},
        transcript(),
    )
    legacy = cfg.vault.insights_path / "2026-01-03 legacy.md"
    legacy.write_text(
        "---\nsource: pocket\ndate: 2026-01-03\ntranscript_id: old3\n---\n\n"
        f"## Summary\n{long_detailed}\n",
        encoding="utf-8",
    )

    assert insight.summary == retrieval_abstract(long_detailed)
    assert indexer.parse_note(legacy).summary == insight.summary
