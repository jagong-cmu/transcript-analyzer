"""The lecture profile's gates: nothing ungrounded, and no diagram it invented.

The transcript has no visual channel and lecture ASR is noisy, so everything
the study-notes pass asserts about the class has to be provable against the
transcript, and every diagram has to be something a renderer will actually
draw.
"""
from dataclasses import replace

import pytest

from transcript_analyzer.pipeline import lecture

TRANSCRIPT = (
    "[0:12] Okay so today we are row reducing a three by three matrix.\n"
    "[1:30] The first pivot is the leading entry in row one.\n"
    "[2:05] Homework three is due Friday at midnight, no extensions.\n"
    "[3:40] This matters for the skills we need in this new age of AR.\n"
)


def payload(**over):
    base = {
        "overview": "We row reduced a matrix.",
        "sections": [
            {
                "heading": "Row reduction",
                "body": "Swap, scale, eliminate.",
                "anchor": "row reducing a three by three matrix",
                "visuals": [],
            }
        ],
        "key_terms": ["pivot — the leading nonzero entry"],
        "assessment": [
            {"text": "Homework 3 is due Friday.", "quote": "Homework three is due Friday"}
        ],
        "background": [{"heading": "RREF", "body": "Reduced row echelon form."}],
        "asr_repairs": [{"heard": "new age of AR", "corrected": "new age of AI"}],
    }
    base.update(over)
    return base


def build(cfg, **over):
    return lecture.study_notes_from_payload(payload(**over), TRANSCRIPT, cfg)


def test_grounded_section_survives(cfg):
    notes = build(cfg)
    assert [s.heading for s in notes.sections] == ["Row reduction"]
    assert notes.assessment == ["Homework 3 is due Friday."]
    assert notes.dropped_claims == 0


def test_section_whose_anchor_is_not_in_the_transcript_is_dropped(cfg):
    """A section is an assertion about what was taught. No span, no section."""
    notes = build(
        cfg,
        sections=[
            {
                "heading": "Eigenvalues",
                "body": "The professor covered eigenvalues at length.",
                "anchor": "today we will cover eigenvalues in depth",
                "visuals": [],
            }
        ],
    )
    assert notes.sections == []
    assert notes.dropped_claims == 1


def test_anchor_must_be_long_enough_to_mean_something(cfg):
    """'the' appears in every transcript; a two-word span proves nothing."""
    notes = build(
        cfg,
        sections=[
            {"heading": "X", "body": "y", "anchor": "the", "visuals": []}
        ],
    )
    assert notes.sections == [] and notes.dropped_claims == 1


def test_assessment_claim_needs_a_verbatim_quote(cfg):
    """The examinable claims are the ones a student acts on — gate them hardest."""
    notes = build(
        cfg,
        assessment=[
            {"text": "The midterm is next week.", "quote": "the midterm is next week"}
        ],
    )
    assert notes.assessment == []
    assert notes.dropped_claims == 1


def test_asr_repair_must_quote_what_it_replaced(cfg):
    notes = build(cfg)
    assert [(r.heard, r.corrected) for r in notes.asr_repairs] == [
        ("new age of AR", "new age of AI")
    ]

    invented = build(
        cfg, asr_repairs=[{"heard": "the professor said quicksort", "corrected": "mergesort"}]
    )
    assert invented.asr_repairs == []
    assert invented.dropped_claims == 1


def test_a_repair_that_changes_nothing_is_not_recorded(cfg):
    notes = build(cfg, asr_repairs=[{"heard": "the first pivot", "corrected": "The First Pivot"}])
    assert notes.asr_repairs == []
    # Not a dropped claim either — nothing was asserted, so nothing was lost.
    assert notes.dropped_claims == 0


def test_background_is_kept_separate_not_merged(cfg):
    notes = build(cfg)
    assert [b.heading for b in notes.background] == ["RREF"]
    # It is NOT a section: sections are the grounded tier.
    assert "RREF" not in [s.heading for s in notes.sections]


@pytest.mark.parametrize(
    "visual, why",
    [
        ({"kind": "mermaid", "caption": "c", "source": "A --> B", "language": ""},
         "mermaid without a diagram keyword cannot render"),
        ({"kind": "mermaid", "caption": "", "source": "flowchart TD\n A-->B", "language": ""},
         "a diagram with no caption cannot say what it illustrates"),
        ({"kind": "png", "caption": "c", "source": "data:...", "language": ""},
         "only the three deterministic kinds exist"),
        ({"kind": "math", "caption": "c", "source": "", "language": ""},
         "empty source"),
        ({"kind": "mermaid", "caption": "c", "source": "flowchart TD\n" + "x" * 5000,
          "language": ""},
         "runaway source"),
    ],
)
def test_unrenderable_visual_specs_are_dropped(cfg, visual, why):
    notes = build(
        cfg,
        sections=[
            {
                "heading": "Row reduction",
                "body": "b",
                "anchor": "row reducing a three by three matrix",
                "visuals": [visual],
            }
        ],
    )
    assert notes.visuals == [], why
    assert notes.dropped_visuals == 1


def test_valid_visuals_survive_and_code_gets_a_language(cfg):
    notes = build(
        cfg,
        sections=[
            {
                "heading": "Row reduction",
                "body": "b",
                "anchor": "row reducing a three by three matrix",
                "visuals": [
                    {"kind": "mermaid", "caption": "Order of operations.",
                     "source": "flowchart TD\n  A-->B", "language": ""},
                    {"kind": "math", "caption": "The matrix.",
                     "source": r"\begin{bmatrix}1\end{bmatrix}", "language": ""},
                    {"kind": "code", "caption": "SML.", "source": "fun f x = x", "language": ""},
                ],
            }
        ],
    )
    assert [v.kind for v in notes.visuals] == ["mermaid", "math", "code"]
    assert notes.visuals[2].language == "text"
    assert notes.dropped_visuals == 0


def test_visual_cap_is_enforced_across_sections(cfg):
    cfg = replace(cfg, lecture=replace(cfg.lecture, max_visuals=2))
    one = {"kind": "mermaid", "caption": "c", "source": "flowchart TD\n A-->B", "language": ""}
    notes = build(
        cfg,
        sections=[
            {"heading": "A", "body": "b", "anchor": "row reducing a three by three matrix",
             "visuals": [one, one]},
            {"heading": "B", "body": "b", "anchor": "the leading entry in row one",
             "visuals": [one, one]},
        ],
    )
    assert len(notes.visuals) == 2
    assert notes.dropped_visuals == 2


def test_prune_visuals_removes_exactly_what_the_renderer_refused(cfg):
    one = {"kind": "mermaid", "caption": "c", "source": "flowchart TD\n A-->B", "language": ""}
    two = {"kind": "math", "caption": "c", "source": "x^2", "language": ""}
    notes = build(
        cfg,
        sections=[
            {"heading": "A", "body": "b", "anchor": "row reducing a three by three matrix",
             "visuals": [one, two]},
            {"heading": "B", "body": "b", "anchor": "the leading entry in row one",
             "visuals": [one]},
        ],
    )
    assert len(notes.visuals) == 3
    # study_html numbers figures viz1..viz3 across sections in order.
    pruned = lecture.prune_visuals(notes, {"viz2"})
    assert [v.kind for v in pruned.visuals] == ["mermaid", "mermaid"]
    assert pruned.dropped_visuals == notes.dropped_visuals + 1


def test_prune_with_nothing_dropped_is_the_same_notes(cfg):
    notes = build(cfg)
    assert lecture.prune_visuals(notes, set()) is notes
