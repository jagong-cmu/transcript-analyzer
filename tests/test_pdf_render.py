"""The browser is the gate: a diagram renders, or it is dropped.

This is the only test that starts a browser. It is skipped where the
Playwright Chromium or the cached render assets are not available (CI installs
the package but not the browser), so the deterministic gates in
test_lecture_profile.py stay the ones that always run.
"""
import shutil
import subprocess
import time
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


def test_an_unavailable_asset_cache_is_a_render_error_not_an_escape(monkeypatch, tmp_path):
    """Staging the page is part of producing it.

    An AssetError leaving `render_pdf` would sail past the only exception the
    lecture pass catches, and the whole (paid) study-notes result would be
    discarded instead of the PDF alone.
    """
    pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(pdf, "playwright_available", lambda: True)

    def no_assets(data_dir, dest):
        raise assets.AssetError("cdn unreachable and nothing cached")

    monkeypatch.setattr(assets, "stage_assets", no_assets)
    with pytest.raises(pdf.PdfRenderError, match="cdn unreachable"):
        pdf.render_pdf("<html></html>", tmp_path)


def test_a_visual_that_will_not_settle_loses_to_the_deadline(render_dir, monkeypatch):
    """The diagram loop is bounded INSIDE the page, because it cannot be
    bounded from Python.

    `Page.evaluate` takes no timeout, so `page.set_default_timeout` never
    covered the mermaid/KaTeX loop — the exact step a pathological source
    spins in. Unbounded, an unattended sync wedges on one lecture forever with
    no error and no log line. The deadline is shortened here so the bound is
    what ends the test, not a real spin.
    """
    # ONLY the render deadline is shortened. `page.goto` keeps its own generous
    # bound, so loading the half-megabyte mermaid bundle cannot be what trips —
    # Playwright's own message also contains "exceeded", and a test that can
    # pass for the wrong reason is worse than no test.
    monkeypatch.setattr(pdf, "RENDER_TIMEOUT_MS", 250)
    notes = notes_with(
        Visual(kind="mermaid", caption="Fine.", source="flowchart TD\n  A-->B")
    )
    html = study.study_html(notes, title="Lecture", when=date(2026, 9, 1))
    # A script that never yields: the render loop cannot finish, so the
    # deadline must be what resolves the page.
    stuck = html.replace(
        "</body>",
        "<script>window.mermaid.render = () => new Promise(() => {});</script></body>",
    )
    assert stuck != html, "the probe was not injected"

    started = time.monotonic()
    with pytest.raises(pdf.PdfRenderError) as caught:
        pdf.render_pdf(stuck, render_dir)

    # The IN-PAGE deadline's own message, not Playwright's generic timeout.
    assert "study-notes render exceeded" in str(caught.value)
    assert time.monotonic() - started < 60, "the deadline did not end the render"


def test_a_tripped_deadline_reaches_the_caller_as_a_render_error(monkeypatch, tmp_path):
    """Browser-free half of the same contract.

    However the page-level failure arrives, `lecture.produce` only catches
    PdfRenderError — anything else discards the whole paid study-notes result
    instead of the PDF alone.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(pdf, "playwright_available", lambda: True)
    monkeypatch.setattr(assets, "stage_assets", lambda data_dir, dest: None)

    def deadline_tripped(*a, **k):
        raise playwright.Error("study-notes render exceeded 250ms")

    monkeypatch.setattr(pdf, "sync_playwright", deadline_tripped, raising=False)
    import playwright.sync_api as pw

    monkeypatch.setattr(pw, "sync_playwright", deadline_tripped)
    with pytest.raises(pdf.PdfRenderError, match="exceeded"):
        pdf.render_pdf("<html></html>", tmp_path)


NODE_HARNESS = """
const figure = { id: 'viz1', remove() {} };
const el = {
  dataset: { src: 'flowchart TD\\n A-->B' },
  closest: () => figure,
  querySelector: () => ({}),
  set innerHTML(_v) {},
};
global.document = {
  querySelectorAll: (sel) => (sel === '.viz-mermaid' ? [el] : []),
};
global.window = {
  katex: { render() {} },
  mermaid: {
    initialize() {},
    parse: async () => true,
    // Never settles: exactly the pathological layout the deadline exists for.
    render: () => new Promise(() => {}),
  },
};
const started = Date.now();
render(%(deadline)d).then(
  () => console.log('RESOLVED'),
  (e) => console.log('REJECTED ' + (Date.now() - started) + ' ' + e.message),
);
"""


def test_the_render_loop_rejects_when_it_cannot_settle(tmp_path):
    """The bound itself, executed — no browser and no real spin.

    `_RENDER_JS` is the generated program handed to the page, so running it is
    running the interface. With `mermaid.render` never settling, the deadline
    has to be what resolves it; unbounded, `page.evaluate` would hang the
    unattended sync forever with no error and no log line.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available to execute the page script")

    script = tmp_path / "probe.js"
    script.write_text(
        f"const render = {pdf._RENDER_JS};\n" + NODE_HARNESS % {"deadline": 200},
        encoding="utf-8",
    )
    out = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr

    verdict = out.stdout.strip()
    assert verdict.startswith("REJECTED"), f"the loop was never bounded: {verdict!r}"
    _, elapsed_ms, *rest = verdict.split(" ", 2)
    assert int(elapsed_ms) < 5_000, "the deadline did not fire promptly"
    assert "exceeded 200ms" in " ".join(rest)


def test_the_render_loop_returns_normally_when_it_settles(tmp_path):
    """The guard: the deadline must not pre-empt a render that works."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available to execute the page script")

    script = tmp_path / "probe_ok.js"
    harness = NODE_HARNESS.replace(
        "render: () => new Promise(() => {}),",
        "render: async () => ({ svg: '<svg/>' }),",
    )
    script.write_text(
        f"const render = {pdf._RENDER_JS};\n" + harness % {"deadline": 5_000},
        encoding="utf-8",
    )
    out = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "RESOLVED"
