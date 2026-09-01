"""The lecture profile: study-grade notes and diagram specs for a class.

This is a PROFILE IN THE PIPELINE, not a Claude Code skill — sync runs
unattended under launchd with no agent present, so a SKILL.md would be inert
exactly when it is needed. It runs only for transcripts the extraction pass
classified `kind: lecture`, on its own model and effort (`stage="lecture"`).

Three constraints shape everything here:

1. **The transcript has no visual channel.** The professor's matrix was on a
   whiteboard and never entered the transcript. Every diagram is reconstructed
   from speech, so a diagram is a SPEC that is rendered deterministically
   (mermaid / KaTeX / a code listing) and DROPPED when it fails — never an
   image, never faked.
2. **Lecture ASR is noisy.** A single mic, no usable speaker labels; the real
   15150 transcript says "this new age of AR" where the professor said AI.
   Repairing that is allowed, but every repair is recorded in the note's
   frontmatter with the span it replaced, and the span has to be real.
3. **Fidelity is two-tier and visually separated.** Anything asserted as
   taught, assigned or examinable carries a verbatim anchor and goes through
   the citation gate. Gap-filling background is allowed but is never blended
   in: it is returned separately and rendered in its own marked block.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from ..config import Config
from ..models import (
    AsrRepair,
    BackgroundNote,
    Insight,
    StudyNotes,
    StudySection,
    Transcript,
    Visual,
)
from .citations import quote_matches
from .llm import LLM

_log = logging.getLogger(__name__)

# A span shorter than this proves nothing — "the" appears in every transcript.
MIN_ANCHOR_CHARS = 12
# Mermaid sources longer than this are never a lecture diagram; they are a
# runaway generation, and they are what makes a browser render hang.
MAX_VISUAL_CHARS = 4_000
# The diagram types the renderer knows how to validate and draw. A source
# whose first line is not one of these cannot render, so it is dropped before
# the browser is ever started.
MERMAID_DIAGRAMS = (
    "flowchart",
    "graph",
    "sequencediagram",
    "classdiagram",
    "statediagram",
    "erdiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
    "quadrantchart",
    "block-beta",
    "xychart-beta",
)

_VISUAL_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["mermaid", "math", "code"],
            "description": (
                "mermaid for a flow/timeline/concept map, math for a KaTeX "
                "expression, code for a source listing."
            ),
        },
        "caption": {
            "type": "string",
            "description": "One sentence stating what this illustrates and where it came from in the lecture.",
        },
        "source": {
            "type": "string",
            "description": (
                "The diagram source: valid Mermaid, a KaTeX/LaTeX math "
                "expression (no $ delimiters), or a code listing."
            ),
        },
        "language": {
            "type": "string",
            "description": "For kind=code, the language (e.g. 'sml'). Otherwise \"\".",
        },
    },
    "required": ["kind", "caption", "source", "language"],
}

LECTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": (
                "Markdown. The narrative summary of the whole class, readable "
                "on its own: what it was about, how the argument developed, "
                "where it ended up. 400-700 words."
            ),
        },
        "sections": {
            "type": "array",
            "description": "The lecture's material, in the order it was taught.",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {
                        "type": "string",
                        "description": (
                            "Markdown. Explain the material well enough to study "
                            "from: definitions, the reasoning, worked steps with "
                            "their numbers, and what the instructor emphasised."
                        ),
                    },
                    "anchor": {
                        "type": "string",
                        "description": (
                            "A VERBATIM span copied character-for-character from "
                            "the transcript, showing this section is about "
                            "something actually said in this lecture."
                        ),
                    },
                    "visuals": {"type": "array", "items": _VISUAL_SCHEMA},
                },
                "required": ["heading", "body", "anchor", "visuals"],
            },
        },
        "key_terms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Term — definition, one line each, as taught.",
        },
        "assessment": {
            "type": "array",
            "description": "Anything stated as assigned, due, or examinable.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "quote": {
                        "type": "string",
                        "description": "VERBATIM span from the transcript stating it.",
                    },
                },
                "required": ["text", "quote"],
            },
        },
        "background": {
            "type": "array",
            "description": (
                "Context the lecture assumed but did not state. NOT from the "
                "lecture — rendered in a separate, marked block."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["heading", "body"],
            },
        },
        "asr_repairs": {
            "type": "array",
            "description": "Obvious speech-recognition garble you corrected.",
            "items": {
                "type": "object",
                "properties": {
                    "heard": {
                        "type": "string",
                        "description": "The garbled text, VERBATIM from the transcript.",
                    },
                    "corrected": {"type": "string", "description": "What was actually said."},
                },
                "required": ["heard", "corrected"],
            },
        },
    },
    "required": [
        "overview",
        "sections",
        "key_terms",
        "assessment",
        "background",
        "asr_repairs",
    ],
}

SYSTEM = """You turn the transcript of a university lecture into study notes a
student can read straight through and understand the class from.

THE TRANSCRIPT IS ALL YOU HAVE. It comes from a single microphone with no
speaker labels, and it contains no image of anything the instructor wrote on
the board. Whatever was drawn, projected, or pointed at exists for you only as
the words spoken around it.

Two tiers of fidelity, and they are enforced in code, not trusted:

  GROUNDED — anything you state as taught, assigned, or examinable must come
  from the transcript. Each section carries `anchor`: a span copied
  character-for-character out of the transcript. A section whose anchor does
  not string-match the transcript is DISCARDED before anything is written, and
  so is an assessment item whose quote does not match. Copy the span; do not
  retype it, trim it mid-word, or fix its grammar.

  BACKGROUND — context the lecture assumed but never stated (a prerequisite
  definition, the standard name of a method) belongs in `background`, never in
  a section body. It is rendered under a heading that says it is not from the
  lecture. Putting it in a section instead is the one thing that makes these
  notes untrustworthy.

SPEECH RECOGNITION. Expect misheard technical terms: a functional-programming
lecture saying "this new age of AR" meant AI. Where the intended term is
unambiguous from context, use it and record the repair in `asr_repairs` with
the garbled text copied verbatim. Where it is genuinely ambiguous, keep what
was said and say in the body that the audio is unclear. Never invent a
correction to make a sentence tidier.

DIAGRAMS. Produce {min_visuals}-{max_visuals} visuals in total, spread across the sections
where they help. They are rendered mechanically, so they must be valid:

  * "mermaid" — a flowchart, timeline, state diagram, or concept map. Valid
    Mermaid, starting with the diagram keyword (`flowchart TD`, `timeline`,
    `stateDiagram-v2`, ...). Keep node labels short and quote any label with
    punctuation in it. This is the right choice for a process, a decision,
    a dependency, or how ideas relate.
  * "math" — a single KaTeX expression, no $ delimiters. Use it to show a
    matrix, an equation, or a step of a derivation that was spoken aloud.
    Reconstruct only what the words determine: if the numbers in a matrix
    were never said, do not invent them — draw the shape of the operation
    instead, or choose a different visual.
  * "code" — a source listing in the language being taught (`sml` for a
    functional programming lecture), from code the instructor dictated or
    described.

Every visual carries a caption saying what it illustrates. A visual that fails
to render is dropped, so prefer a simple diagram that will render over an
elaborate one that might not. Never describe a diagram you could not draw as
though the reader can see it."""

USER_TEMPLATE = """Lecture: {title}
{course}Date: {date}

Write the study notes for this lecture.

Transcript:
\"\"\"
{text}
\"\"\""""


def build_study_notes(
    transcript: Transcript,
    insight: Insight,
    cfg: Config,
    llm: Optional[LLM] = None,
    *,
    stage: str = "lecture",
) -> StudyNotes:
    """Run the study-notes pass and return only what survives the gates."""
    llm = llm or LLM(cfg)
    course = ""
    if insight.course_code or insight.course_name:
        course = f"Course: {insight.course_code} {insight.course_name}".strip() + "\n"
    user = USER_TEMPLATE.format(
        title=insight.headline or transcript.title,
        course=course,
        date=transcript.date.isoformat(),
        text=transcript.text,
    )
    system = SYSTEM.format(
        min_visuals=cfg.lecture.min_visuals, max_visuals=cfg.lecture.max_visuals
    )
    data = llm.chat_json(
        system,
        user,
        schema=LECTURE_SCHEMA,
        max_tokens=cfg.anthropic.lecture_max_tokens,
        stage=stage,
        # Study notes are the longest generation in the system and run with
        # thinking on; a non-streaming request would race the HTTP timeout.
        stream=True,
    )
    return study_notes_from_payload(data, transcript.text, cfg)


def study_notes_from_payload(
    data: dict, transcript_text: str, cfg: Config
) -> StudyNotes:
    """Apply every gate to one study-notes response.

    Separated from the call so the gates are testable without the API, and so
    a batched backfill runs the identical checks.
    """
    dropped_claims = 0
    sections: list[StudySection] = []
    budget = max(0, cfg.lecture.max_visuals)

    for raw in _as_dicts(data.get("sections")):
        heading = _text(raw.get("heading"))
        body = _text(raw.get("body"))
        anchor = _text(raw.get("anchor"))
        if not heading or not body:
            dropped_claims += 1
            continue
        if not quote_matches(anchor, transcript_text, min_chars=MIN_ANCHOR_CHARS):
            # The section claims to be about this lecture and cannot show it.
            _log.info("study notes: dropping ungrounded section %r", heading)
            dropped_claims += 1
            continue
        visuals, dropped_v = _clean_visuals(raw.get("visuals"), budget)
        budget -= len(visuals)
        sections.append(
            StudySection(heading=heading, body=body, anchor=anchor, visuals=visuals)
        )

    assessment: list[str] = []
    for raw in _as_dicts(data.get("assessment")):
        text = _text(raw.get("text"))
        quote = _text(raw.get("quote"))
        if not text or not quote_matches(
            quote, transcript_text, min_chars=MIN_ANCHOR_CHARS
        ):
            dropped_claims += 1
            continue
        assessment.append(text)

    repairs: list[AsrRepair] = []
    for raw in _as_dicts(data.get("asr_repairs")):
        heard = _text(raw.get("heard"))
        corrected = _text(raw.get("corrected"))
        # A repair is auditable only if the thing it replaced is really in the
        # transcript; an unmatched one is an unexplained rewrite.
        if not corrected or heard.casefold() == corrected.casefold():
            continue
        if not quote_matches(heard, transcript_text):
            dropped_claims += 1
            continue
        repairs.append(AsrRepair(heard=heard, corrected=corrected))

    background = [
        BackgroundNote(heading=_text(b.get("heading")), body=_text(b.get("body")))
        for b in _as_dicts(data.get("background"))
        if _text(b.get("heading")) and _text(b.get("body"))
    ]

    # Count every visual the model proposed that no section kept, including
    # the ones cut by the cap, so the note can say how many were dropped.
    proposed = sum(
        len(_as_dicts(s.get("visuals"))) for s in _as_dicts(data.get("sections"))
    )
    kept = sum(len(s.visuals) for s in sections)

    return StudyNotes(
        overview=_text(data.get("overview")),
        sections=sections,
        key_terms=[t for t in _as_strs(data.get("key_terms")) if t],
        assessment=assessment,
        background=background,
        asr_repairs=repairs,
        dropped_claims=dropped_claims,
        dropped_visuals=max(0, proposed - kept),
    )


def _clean_visuals(raw, budget: int) -> tuple[list[Visual], int]:
    """Structurally valid visuals, up to `budget`. Returns (kept, dropped).

    This is the cheap gate: a spec that cannot possibly render is dropped
    here, before a browser is started. The expensive gate is the renderer
    itself, which drops whatever mermaid or KaTeX refuses.
    """
    kept: list[Visual] = []
    dropped = 0
    for item in _as_dicts(raw):
        if len(kept) >= budget:
            dropped += 1
            continue
        v = _clean_visual(item)
        if v is None:
            dropped += 1
            continue
        kept.append(v)
    return kept, dropped


def _clean_visual(item: dict) -> Optional[Visual]:
    kind = _text(item.get("kind")).lower()
    caption = _text(item.get("caption"))
    source = str(item.get("source") or "").strip()
    if kind not in ("mermaid", "math", "code"):
        return None
    # A visual with no caption cannot state what it illustrates, which is the
    # whole contract for a diagram reconstructed from speech.
    if not caption or not source or len(source) > MAX_VISUAL_CHARS:
        return None
    if kind == "mermaid" and not _looks_like_mermaid(source):
        return None
    language = _text(item.get("language")).lower()
    if kind == "code" and not language:
        language = "text"
    return Visual(kind=kind, caption=caption, source=source, language=language)


def _looks_like_mermaid(source: str) -> bool:
    """Whether the source opens with a Mermaid diagram keyword.

    Mermaid decides the diagram type from the first meaningful line, so a
    source that does not start with one is guaranteed to fail in the browser.
    Catching it here keeps a broken spec out of the markdown study note too,
    where nothing else would ever have validated it.
    """
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith("%%"):
            continue
        head = line.lower()
        return any(head.startswith(d) for d in MERMAID_DIAGRAMS)
    return False


def _as_dicts(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _as_strs(v) -> list[str]:
    return [_text(x) for x in v] if isinstance(v, list) else []


def _text(v) -> str:
    return str(v or "").strip()


def visuals_of(notes: StudyNotes) -> Iterable[Visual]:
    return notes.visuals
