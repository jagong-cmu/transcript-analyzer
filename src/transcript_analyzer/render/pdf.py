"""Render a study-notes page to PDF with the cached Playwright Chromium.

Chosen over pandoc/XeLaTeX because one renderer covers all three visual kinds
at once — Mermaid draws natively in the browser, KaTeX does the math, and code
listings are just markup — and because a LaTeX failure inside an unattended
daemon is a silent, unfixable-at-3am kind of failure.

The browser is also the GATE. A diagram spec reconstructed from speech is a
guess about something the transcript never showed, so the rule is: it renders
or it is dropped. `mermaid.parse` and KaTeX with `throwOnError` decide, in the
same page that is about to be printed, and a figure that fails is removed
whole — caption included, so no caption is ever left describing a picture that
is not there. Nothing is drawn to stand in for it.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import assets

_log = logging.getLogger(__name__)

# A pathological mermaid source can spin in layout; the page as a whole is
# bounded so an unattended sync can never wedge on one lecture.
RENDER_TIMEOUT_MS = 60_000
PDF_MARGIN = "16mm"


class PdfRenderError(RuntimeError):
    """The page could not be rendered at all (no browser, assets, or libraries)."""


@dataclass
class DroppedVisual:
    """One figure the browser refused to draw, and why."""

    id: str
    reason: str


@dataclass
class RenderResult:
    pdf: bytes
    kept: int = 0
    dropped: list[DroppedVisual] = field(default_factory=list)

    @property
    def dropped_ids(self) -> set[str]:
        """Figure ids the page removed — `study_html` numbers them viz1, viz2, …

        The caller prunes the same visuals out of the markdown study note with
        these, so the two renderings of one lecture cannot disagree about
        which diagrams exist.
        """
        return {d.id for d in self.dropped}


# Runs inside the page, after both libraries have loaded. Returns the ids of
# the figures it removed, so the caller can report what was dropped rather
# than shipping a PDF that quietly lost half its diagrams.
_RENDER_JS = r"""
async () => {
  if (typeof window.mermaid === 'undefined') throw new Error('mermaid did not load');
  if (typeof window.katex === 'undefined') throw new Error('katex did not load');
  window.mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'neutral',
    fontFamily: 'system-ui, -apple-system, Helvetica, Arial, sans-serif',
  });
  const dropped = [];
  const drop = (fig, why) => { dropped.push({ id: fig.id, reason: why }); fig.remove(); };

  let n = 0;
  for (const el of Array.from(document.querySelectorAll('.viz-mermaid'))) {
    const fig = el.closest('figure');
    const src = el.dataset.src || '';
    try {
      await window.mermaid.parse(src);
      const { svg } = await window.mermaid.render('m' + (n++), src);
      el.innerHTML = svg;
      if (!el.querySelector('svg')) { drop(fig, 'mermaid produced no svg'); }
    } catch (e) {
      drop(fig, 'mermaid: ' + (e && e.message ? e.message : String(e)));
    }
  }

  for (const el of Array.from(document.querySelectorAll('.viz-math'))) {
    const fig = el.closest('figure');
    try {
      window.katex.render(el.dataset.src || '', el, {
        throwOnError: true, displayMode: true,
      });
      if (!el.querySelector('.katex')) { drop(fig, 'katex produced no output'); }
    } catch (e) {
      drop(fig, 'katex: ' + (e && e.message ? e.message : String(e)));
    }
  }
  return { dropped, kept: document.querySelectorAll('figure[data-viz]').length };
}
"""


def playwright_available() -> bool:
    """Whether a PDF can be rendered at all on this machine.

    Checked before the lecture pass decides to promise a PDF, so a machine
    without the browser still gets its markdown study notes instead of an
    exception in the middle of an unattended sync.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def render_pdf(html: str, data_dir: Path) -> RenderResult:
    """Render one study-notes page, dropping every visual that fails.

    Raises PdfRenderError when the PAGE could not be produced — no browser, no
    libraries, a timeout. A single bad diagram is never that: it is dropped and
    named in `RenderResult.dropped`.
    """
    if not playwright_available():
        raise PdfRenderError(
            "playwright is not installed; install the 'pdf' extra "
            "(pip install -e '.[pdf]') to render study-note PDFs"
        )
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="ta-study-") as tmp:
        work = Path(tmp)
        try:
            # Local copies, loaded over file:// with relative paths — that is
            # what lets KaTeX's stylesheet find its own fonts, and what keeps
            # an unattended render working when the network is not there.
            assets.stage_assets(data_dir, work)
            page_path = work / "index.html"
            page_path.write_text(html, encoding="utf-8")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page()
                    page.set_default_timeout(RENDER_TIMEOUT_MS)
                    page.goto(page_path.as_uri(), wait_until="load")
                    outcome = page.evaluate(_RENDER_JS)
                    pdf = page.pdf(
                        format="A4",
                        print_background=True,
                        margin={
                            "top": PDF_MARGIN,
                            "bottom": PDF_MARGIN,
                            "left": PDF_MARGIN,
                            "right": PDF_MARGIN,
                        },
                    )
                finally:
                    browser.close()
        except PlaywrightTimeout as e:
            raise PdfRenderError(f"study-notes render timed out: {e}") from e
        except PlaywrightError as e:
            raise PdfRenderError(f"study-notes render failed: {e}") from e
        except (assets.AssetError, OSError) as e:
            # Staging the page is part of producing it, so a missing asset
            # cache is the same kind of failure as a browser that will not
            # start: it costs the PDF, and the caller still writes the
            # markdown study notes.
            raise PdfRenderError(f"study-notes page could not be staged: {e}") from e

    dropped = [
        DroppedVisual(id=str(d.get("id", "")), reason=str(d.get("reason", "")))
        for d in (outcome or {}).get("dropped", [])
        if isinstance(d, dict)
    ]
    for d in dropped:
        _log.warning(
            "dropped study-notes visual %s that would not render (%s)", d.id, d.reason
        )
    return RenderResult(pdf=pdf, kept=int((outcome or {}).get("kept", 0)), dropped=dropped)
