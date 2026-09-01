"""Render study notes to markdown (for the vault) and HTML (for the PDF).

Both renderers are pure functions of a `StudyNotes` — no browser, no network —
so what the notes SAY is testable apart from whether a diagram draws. The
browser's job in `render.pdf` is only to decide which diagram specs actually
render, and to drop the ones that do not.

The two-tier fidelity rule is a layout rule here: grounded sections come
first, and background is a single, clearly labelled block at the end. Nothing
merges them.
"""
from __future__ import annotations

import html
from datetime import date as _date
from typing import Optional

from ..models import StudyNotes, Visual

BACKGROUND_HEADING = "Background (not from lecture)"
_PROVENANCE = (
    "Generated from the recording's transcript. Diagrams are reconstructed "
    "from what was said — the transcript carries no image of the board."
)


def course_line(course_code: str, course_name: str) -> str:
    parts = [p for p in (course_code.strip(), course_name.strip()) if p]
    return " · ".join(parts)


# ---------------------------------------------------------------- markdown


def study_markdown(
    notes: StudyNotes,
    *,
    title: str,
    when: _date,
    course_code: str = "",
    course_name: str = "",
    transcript_stem: str = "",
    pdf_name: str = "",
) -> str:
    """The vault-readable study notes. Obsidian renders mermaid and $$…$$."""
    out: list[str] = []
    course = course_line(course_code, course_name)
    if course:
        out.append(f"**{course}**  ")
    out.append(f"**{when.isoformat()}**")
    links = []
    if transcript_stem:
        links.append(f"[[{transcript_stem}|Recording and transcript]]")
    if pdf_name:
        links.append(f"[[{pdf_name}|Printable PDF]]")
    if links:
        out.append("")
        out.append(" · ".join(links))
    out.append("")

    if notes.overview:
        out.append("## Overview")
        out.append(notes.overview)
        out.append("")

    for section in notes.sections:
        out.append(f"## {section.heading}")
        out.append(section.body)
        out.append("")
        for v in section.visuals:
            out.append(_visual_markdown(v))
            out.append("")

    if notes.key_terms:
        out.append("## Key terms")
        out.extend(f"- {t}" for t in notes.key_terms)
        out.append("")

    if notes.assessment:
        out.append("## Assigned and examinable")
        out.extend(f"- {a}" for a in notes.assessment)
        out.append("")

    if notes.background:
        # Its own block, never blended into a section: everything above is
        # grounded in the transcript and everything here is not.
        out.append(f"## {BACKGROUND_HEADING}")
        out.append(
            "_Context the lecture assumed but did not state. Not said in class._"
        )
        out.append("")
        for b in notes.background:
            out.append(f"### {b.heading}")
            out.append(b.body)
            out.append("")

    if notes.asr_repairs:
        out.append("## Speech-recognition repairs")
        out.append(
            "_Garbled audio corrected in these notes, with what the transcript says._"
        )
        out.extend(f"- “{r.heard}” → **{r.corrected}**" for r in notes.asr_repairs)
        out.append("")

    out.append("---")
    out.append(f"_{_PROVENANCE}_")
    if notes.dropped_claims or notes.dropped_visuals:
        out.append(
            f"_{notes.dropped_claims} claim(s) dropped by the citation gate · "
            f"{notes.dropped_visuals} diagram(s) dropped._"
        )
    return "\n".join(out)


def _visual_markdown(v: Visual) -> str:
    if v.kind == "mermaid":
        body = f"```mermaid\n{v.source.strip()}\n```"
    elif v.kind == "math":
        body = f"$$\n{v.source.strip()}\n$$"
    else:
        body = f"```{v.language or 'text'}\n{v.source.rstrip()}\n```"
    return f"{body}\n\n*{v.caption}*"


# -------------------------------------------------------------------- HTML

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 11.5pt/1.55 "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  color: #16181d;
  background: #fff;
}
.page { padding: 0; }
h1 { font-size: 22pt; line-height: 1.2; margin: 0 0 6px; }
h2 { font-size: 14pt; margin: 22px 0 6px; padding-bottom: 3px;
     border-bottom: 1px solid #d9dce3; break-after: avoid; }
h3 { font-size: 12pt; margin: 14px 0 4px; break-after: avoid; }
p { margin: 0 0 9px; }
ul, ol { margin: 0 0 9px; padding-left: 20px; }
li { margin: 0 0 3px; }
code { font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 0.88em;
       background: #f2f3f6; padding: 1px 4px; border-radius: 3px; }
pre { background: #f7f8fa; border: 1px solid #e3e6ec; border-radius: 6px;
      padding: 10px 12px; overflow-x: auto; }
pre code { background: none; padding: 0; }
.masthead { border-bottom: 2px solid #16181d; padding-bottom: 10px; margin-bottom: 18px; }
.course { font: 600 10pt/1.3 -apple-system, "Helvetica Neue", Arial, sans-serif;
          letter-spacing: .07em; text-transform: uppercase; color: #5a6072; }
.dateline { font: 10pt/1.4 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #5a6072; }
figure { margin: 14px 0; padding: 12px; border: 1px solid #e3e6ec; border-radius: 8px;
         background: #fbfcfe; break-inside: avoid; }
figure svg { max-width: 100%; height: auto; display: block; margin: 0 auto; }
figure .katex-display { margin: 4px 0; }
figcaption { margin-top: 8px; font: italic 9.5pt/1.4 -apple-system, "Helvetica Neue", Arial, sans-serif;
             color: #5a6072; text-align: center; }
.background { margin-top: 26px; padding: 14px 16px; border: 1px dashed #b9861f;
              border-radius: 8px; background: #fffaf0; break-inside: auto; }
.background h2 { border-bottom: none; margin-top: 0; color: #8a6510; }
.background .disclaimer { font: 9.5pt/1.4 -apple-system, "Helvetica Neue", Arial, sans-serif;
                          color: #8a6510; margin-bottom: 10px; }
.repairs { font: 10pt/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #3c4150; }
.repairs .heard { color: #8a2b2b; }
footer { margin-top: 26px; padding-top: 10px; border-top: 1px solid #d9dce3;
         font: 9pt/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #6b7183; }
"""

_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="katex.min.css">
<style>{css}</style>
</head><body><div class="page">"""


def study_html(
    notes: StudyNotes,
    *,
    title: str,
    when: _date,
    course_code: str = "",
    course_name: str = "",
    source_label: str = "",
) -> str:
    """A self-contained page whose diagram specs are still unrendered.

    Every visual is emitted as a `<figure data-viz>` holding its SOURCE. The
    browser turns those into SVG and removes the ones that fail — which is why
    the caption lives outside the rendered element: a dropped diagram takes its
    caption with it and never leaves a caption describing nothing.
    """
    e = html.escape
    parts: list[str] = [_HEAD.format(title=e(title), css=_CSS)]
    course = course_line(course_code, course_name)
    parts.append('<header class="masthead">')
    if course:
        parts.append(f'<div class="course">{e(course)}</div>')
    parts.append(f"<h1>{e(title)}</h1>")
    dateline = when.strftime("%B %d, %Y")
    if source_label:
        dateline = f"{dateline} · {source_label}"
    parts.append(f'<div class="dateline">{e(dateline)}</div>')
    parts.append("</header>")

    if notes.overview:
        parts.append("<h2>Overview</h2>")
        parts.append(markdown_to_html(notes.overview))

    n = 0
    for section in notes.sections:
        parts.append(f"<h2>{e(section.heading)}</h2>")
        parts.append(markdown_to_html(section.body))
        for v in section.visuals:
            n += 1
            parts.append(_visual_html(v, n))

    if notes.key_terms:
        parts.append("<h2>Key terms</h2><ul>")
        parts.extend(f"<li>{_inline(t)}</li>" for t in notes.key_terms)
        parts.append("</ul>")

    if notes.assessment:
        parts.append("<h2>Assigned and examinable</h2><ul>")
        parts.extend(f"<li>{_inline(a)}</li>" for a in notes.assessment)
        parts.append("</ul>")

    if notes.background:
        parts.append('<section class="background">')
        parts.append(f"<h2>{e(BACKGROUND_HEADING)}</h2>")
        parts.append(
            '<div class="disclaimer">Context the lecture assumed but did not '
            "state. This was not said in class.</div>"
        )
        for b in notes.background:
            parts.append(f"<h3>{e(b.heading)}</h3>")
            parts.append(markdown_to_html(b.body))
        parts.append("</section>")

    if notes.asr_repairs:
        parts.append("<h2>Speech-recognition repairs</h2>")
        parts.append(
            '<div class="repairs">Garbled audio corrected in these notes, with '
            "what the transcript says.<ul>"
        )
        parts.extend(
            f'<li><span class="heard">“{e(r.heard)}”</span> → <strong>{e(r.corrected)}</strong></li>'
            for r in notes.asr_repairs
        )
        parts.append("</ul></div>")

    parts.append(f"<footer>{e(_PROVENANCE)}</footer>")
    parts.append("</div>")
    parts.append('<script src="katex.min.js"></script>')
    parts.append('<script src="mermaid.min.js"></script>')
    parts.append("</body></html>")
    return "\n".join(parts)


def _visual_html(v: Visual, n: int) -> str:
    e = html.escape
    vid = f"viz{n}"
    if v.kind == "mermaid":
        inner = f'<div class="viz-mermaid" data-src="{e(v.source)}"></div>'
    elif v.kind == "math":
        inner = f'<div class="viz-math" data-src="{e(v.source)}"></div>'
    else:
        lang = e(v.language or "text")
        inner = f'<pre><code class="language-{lang}">{e(v.source.rstrip())}</code></pre>'
    return (
        f'<figure id="{vid}" data-viz="{e(v.kind)}">{inner}'
        f"<figcaption>{e(v.caption)}</figcaption></figure>"
    )


_INLINE_CODE = "`"


def markdown_to_html(text: str) -> str:
    """The small markdown subset the model actually emits, as HTML.

    Deliberately not a markdown library: the study-note body is headings,
    paragraphs, lists, and inline emphasis, and a dependency that renders raw
    HTML from model output would hand the page an injection surface it does
    not need. Everything is escaped first; only these shapes are re-admitted.
    """
    out: list[str] = []
    list_open: Optional[str] = None

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        level = 0
        while level < len(stripped) and stripped[level] == "#":
            level += 1
        if 0 < level <= 6 and stripped[level: level + 1] == " ":
            close_list()
            # This text always sits under a section's own h2, so a heading may
            # never outrank h3: a '#' or '##' the model wrote is demoted to
            # h3, and anything already deeper keeps the level it asked for.
            tag = f"h{max(3, min(6, level))}"
            out.append(f"<{tag}>{_inline(stripped[level + 1:])}</{tag}>")
            continue
        if stripped.startswith(("- ", "* ")):
            if list_open != "ul":
                close_list()
                out.append("<ul>")
                list_open = "ul"
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        num = stripped.split(". ", 1)
        if len(num) == 2 and num[0].isdigit():
            if list_open != "ol":
                close_list()
                out.append("<ol>")
                list_open = "ol"
            out.append(f"<li>{_inline(num[1])}</li>")
            continue
        close_list()
        out.append(f"<p>{_inline(stripped)}</p>")
    close_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape, then re-admit `code`, **bold** and *italic* — in that order.

    Code first, so emphasis markers inside a code span stay literal.
    """
    escaped = html.escape(str(text or ""))
    parts = escaped.split(_INLINE_CODE)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(f"<code>{part}</code>")
        else:
            out.append(_emphasis(part))
    return "".join(out)


def _emphasis(text: str) -> str:
    for marker, tag in (("**", "strong"), ("*", "em")):
        pieces = text.split(marker)
        if len(pieces) < 3:
            continue
        rebuilt = [pieces[0]]
        for i in range(1, len(pieces)):
            # Pair markers up: odd pieces open, even pieces close.
            rebuilt.append(f"<{tag}>" if i % 2 == 1 else f"</{tag}>")
            rebuilt.append(pieces[i])
        # An unmatched trailing marker would leave an open tag — close it.
        if len(pieces) % 2 == 0:
            rebuilt.append(f"</{tag}>")
        text = "".join(rebuilt)
    return text
