"""Study notes read the same in the vault and in the PDF, and say what is theirs.

The two-tier fidelity rule is only real if the rendering keeps it: grounded
material and background must never end up looking the same.
"""
from datetime import date

import pytest

from transcript_analyzer.models import (
    AsrRepair,
    BackgroundNote,
    StudyNotes,
    StudySection,
    Visual,
)
from transcript_analyzer.render import study


def notes(**over) -> StudyNotes:
    base = dict(
        overview="We row reduced a matrix.",
        sections=[
            StudySection(
                heading="Row reduction",
                body="Swap, then scale.",
                anchor="a",
                visuals=[
                    Visual(kind="mermaid", caption="Order of operations.",
                           source="flowchart TD\n  A-->B"),
                    Visual(kind="math", caption="The matrix.", source=r"\begin{bmatrix}1\end{bmatrix}"),
                    Visual(kind="code", caption="SML.", source="fun f x = x", language="sml"),
                ],
            )
        ],
        key_terms=["pivot — the leading nonzero entry"],
        assessment=["Homework 3 is due Friday."],
        background=[BackgroundNote(heading="RREF", body="Reduced row echelon form.")],
        asr_repairs=[AsrRepair(heard="age of AR", corrected="age of AI")],
    )
    base.update(over)
    return StudyNotes(**base)


def md(**over) -> str:
    return study.study_markdown(
        notes(**over), title="Row reduction", when=date(2026, 9, 1),
        course_code="21241", course_name="Linear Algebra",
        transcript_stem="2026-09-01 row-reduction", pdf_name="x.pdf",
    )


def html(**over) -> str:
    return study.study_html(
        notes(**over), title="Row reduction", when=date(2026, 9, 1),
        course_code="21241", course_name="Linear Algebra",
    )


def test_markdown_carries_every_visual_with_its_caption():
    text = md()
    assert "```mermaid\nflowchart TD" in text
    assert "$$\n\\begin{bmatrix}1\\end{bmatrix}\n$$" in text
    assert "```sml\nfun f x = x\n```" in text
    for caption in ("Order of operations.", "The matrix.", "SML."):
        assert f"*{caption}*" in text


def test_background_is_labelled_as_not_from_the_lecture():
    text = md()
    assert f"## {study.BACKGROUND_HEADING}" in text
    assert "Not said in class." in text
    # It comes AFTER the grounded material, never interleaved with it.
    assert text.index("## Row reduction") < text.index(study.BACKGROUND_HEADING)

    page = html()
    assert 'class="background"' in page
    assert "was not said in class" in page


def test_asr_repairs_are_shown_with_what_they_replaced():
    assert "“age of AR” → **age of AI**" in md()
    assert "age of AR" in html()


def test_drop_counts_are_reported_not_hidden():
    text = md()
    assert "dropped" not in text  # nothing was dropped in this fixture
    loud = study.study_markdown(
        notes(dropped_claims=2, dropped_visuals=1),
        title="t", when=date(2026, 9, 1),
    )
    assert "2 claim(s) dropped by the citation gate" in loud
    assert "1 diagram(s) dropped" in loud


def test_html_holds_diagram_sources_unrendered_for_the_browser_to_judge():
    page = html()
    assert page.count('data-viz=') == 3
    assert 'class="viz-mermaid"' in page and 'class="viz-math"' in page
    # The caption is a sibling of the rendered element, so dropping the figure
    # takes the caption with it.
    assert "<figcaption>Order of operations.</figcaption>" in page


def test_a_lecture_with_no_visuals_still_renders():
    page = html(sections=[StudySection(heading="Talk", body="Words.", anchor="a")])
    assert "data-viz" not in page
    assert "<h2>Talk</h2>" in page


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "</figcaption><script>x</script>",
    ],
)
def test_model_text_cannot_inject_markup_into_the_page(hostile):
    """Everything in these notes is model output; the page escapes first."""
    page = study.study_html(
        notes(overview=hostile, sections=[
            StudySection(heading=hostile, body=hostile, anchor="a",
                         visuals=[Visual(kind="code", caption=hostile, source=hostile)])
        ]),
        title=hostile, when=date(2026, 9, 1),
    )
    # The hostile text is present, but only ever as escaped text: no live
    # element is ever constructed from model output.
    assert "<script>" not in page and "<img" not in page
    assert "&lt;" in page


def test_markdown_subset_renders_the_shapes_the_model_emits():
    out = study.markdown_to_html(
        "## Heading\n\nA **bold** and *italic* and `code`.\n\n- one\n- two\n\n1. first\n2. second"
    )
    assert "<h4>Heading</h4>" in out  # nested under the section's own h2
    assert "<strong>bold</strong>" in out and "<em>italic</em>" in out
    assert "<code>code</code>" in out
    assert out.count("<li>") == 4 and "<ul>" in out and "<ol>" in out


def test_emphasis_markers_inside_code_stay_literal():
    out = study.markdown_to_html("Use `a ** b` for exponent.")
    assert "<code>a ** b</code>" in out
    assert "<strong>" not in out


def test_unclosed_emphasis_does_not_leave_an_open_tag():
    out = study.markdown_to_html("An *unclosed emphasis")
    assert out.count("<em>") == out.count("</em>")
