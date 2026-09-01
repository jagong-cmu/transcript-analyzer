"""The browser is the gate: a diagram renders, or it is dropped.

This is the only test that starts a browser. It is skipped where the
Playwright Chromium or the cached render assets are not available (CI installs
the package but not the browser), so the deterministic gates in
test_lecture_profile.py stay the ones that always run.
"""
from datetime import date
from pathlib import Path

import pytest

from transcript_analyzer.models import StudyNotes, StudySection, Visual
from transcript_analyzer.render import assets, pdf, study


def _renderable(tmp_path: Path) -> bool:
    """Whether this machine can actually render — browser AND libraries."""
    if not pdf.playwright_available():
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception:  # noqa: BLE001 - any failure means "cannot render here"
        return False
    try:
        assets.ensure_assets(tmp_path)
    except assets.AssetError:
        return False
    return True


@pytest.fixture(scope="module")
def render_dir(tmp_path_factory):
    """A data dir with the render assets cached, or a skip."""
    d = tmp_path_factory.mktemp("render-assets")
    if not _renderable(d):
        pytest.skip("no Playwright Chromium or render assets on this machine")
    return d


def notes_with(*visuals) -> StudyNotes:
    return StudyNotes(
        overview="An overview.",
        sections=[StudySection(heading="S", body="Body.", anchor="a", visuals=list(visuals))],
    )


def render(render_dir, notes) -> pdf.RenderResult:
    html = study.study_html(notes, title="Lecture", when=date(2026, 9, 1))
    return pdf.render_pdf(html, render_dir)


def test_valid_visuals_render_into_a_real_pdf(render_dir):
    result = render(
        render_dir,
        notes_with(
            Visual(kind="mermaid", caption="Flow.", source="flowchart TD\n  A-->B"),
            Visual(kind="math", caption="Matrix.", source=r"\begin{bmatrix}1 & 2\end{bmatrix}"),
            Visual(kind="code", caption="SML.", source="fun f x = x", language="sml"),
        ),
    )
    assert result.pdf.startswith(b"%PDF")
    assert result.dropped == []
    assert result.kept == 3


def test_a_diagram_that_will_not_render_is_dropped_never_faked(render_dir):
    """Mermaid and KaTeX decide, in the page that is about to be printed."""
    result = render(
        render_dir,
        notes_with(
            Visual(kind="mermaid", caption="Good.", source="flowchart TD\n  A-->B"),
            Visual(kind="mermaid", caption="Broken.", source="flowchart TD\n  A[[[--> ???"),
            Visual(kind="math", caption="Broken math.", source=r"\frac{1}{"),
        ),
    )
    assert result.pdf.startswith(b"%PDF")
    assert result.dropped_ids == {"viz2", "viz3"}
    assert result.kept == 1
    reasons = " ".join(d.reason for d in result.dropped)
    assert "mermaid" in reasons and "katex" in reasons


def test_a_lecture_with_no_diagrams_still_produces_a_pdf(render_dir):
    result = render(render_dir, notes_with())
    assert result.pdf.startswith(b"%PDF") and result.kept == 0


def test_render_assets_are_cached_not_refetched(render_dir):
    """The second render works offline — that is the point of the mirror."""
    assert assets.have_assets(render_dir)
    root = assets.assets_dir(render_dir)
    assert (root / "mermaid.min.js").stat().st_size > 500_000
    assert list((root / "fonts").glob("*.woff2"))


def test_a_missing_browser_is_a_clear_error_not_a_silent_empty_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr(pdf, "playwright_available", lambda: False)
    with pytest.raises(pdf.PdfRenderError, match="playwright is not installed"):
        pdf.render_pdf("<html></html>", tmp_path)
