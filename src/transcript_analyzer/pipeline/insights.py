"""Extract structured insights from a transcript using the Claude API.

Note: this does NOT assign a category. Notes are organized by date; categories
are created on demand via the `categorize` command (see pipeline/organize.py).

TWO summaries come out of this one call, and the distinction matters:

  `detailed_summary` is what the NOTE shows — the thing the captain reads
  straight through. Its length is proportional to the recording.
  `summary` is a one-paragraph RETRIEVAL abstract that never reaches the note
  body. Every corpus-wide reader (digests, dossiers, study rollups, category
  rollups, the dashboard Ask) reads it, and Ask sends every summary on every
  question — so this is the field that has to stay small.

`kind` classification rides along in the same call rather than costing a
second one; a `lecture` is what makes sync buy the study-notes pass.

Failures propagate (LLMError and subclasses) — under a paid API we never
write an empty note we were billed for; the sync loop counts the failure and
retries the transcript on a later cycle.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config
from ..courses import Course, bind
from ..models import Insight, Transcript, coerce_kind
from ..titles import clean_headline, headline_from_summary, retrieval_abstract
from .llm import LLM

# Cost sanity cap, not a context-window limit (the 1M window fits any
# transcript this system will ever see). ~25k tokens of transcript.
_MAX_CHARS = 100_000

# The detailed summary is the long output here; 16k tokens of note is far more
# than even a 45k-character lecture warrants, and leaves room for the rest.
MAX_TOKENS = 16_000

INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "A short, specific one-line title for this conversation "
                "(5–12 words). No date. Prefer concrete topics and people "
                "over generic labels like 'Meeting' or 'Sync'."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["lecture", "meeting", "interview", "personal"],
            "description": (
                "What kind of recording this is. 'lecture' means an instructor "
                "teaching a class."
            ),
        },
        "course_code": {
            "type": "string",
            "description": (
                "For a lecture, the course number as spoken or implied "
                "(e.g. '21241', '15150'). Empty string when unknown or not a lecture."
            ),
        },
        "course_name": {
            "type": "string",
            "description": (
                "For a lecture, the course's subject name. Empty string when "
                "unknown or not a lecture."
            ),
        },
        "abstract": {
            "type": "string",
            "description": (
                "ONE paragraph (2-4 sentences) summarizing the conversation. "
                "This is a retrieval index entry, not the reader's summary."
            ),
        },
        "detailed_summary": {
            "type": "string",
            "description": (
                "The full narrative summary a reader can read straight through, "
                "in markdown. Length proportional to the recording."
            ),
        },
        "key_points": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "mixed"]},
    },
    "required": [
        "title",
        "kind",
        "course_code",
        "course_name",
        "abstract",
        "detailed_summary",
        "key_points",
        "action_items",
        "people",
        "topics",
        "sentiment",
    ],
}

SYSTEM = """You are an assistant that reads a recording transcript — a class
lecture, a meeting, an interview, or a personal note to self — and extracts a
faithful structured summary. Be faithful to the transcript; do not invent facts,
names, numbers, or conclusions that are not in it.

These transcripts come from automatic speech recognition on a single
microphone. Expect misheard technical terms and missing speaker labels. When a
word is obviously garbled but its intended meaning is clear from context, use
the intended term; when it is not clear, say what was said and note the
uncertainty rather than guessing."""

USER_TEMPLATE = """Analyze the following transcript and return a JSON object with EXACTLY these keys:

- "title": string. A short, specific one-line title for the conversation (5–12 words).
  No date in the title. Name the topic and, when clear, the main person or purpose.
  Bad: "Meeting", "Sync", "Untitled". Good: "Pricing deck review with Angela".
- "kind": one of "lecture", "meeting", "interview", "personal".
  * "lecture": an instructor teaching a class — one dominant voice explaining
    material, worked examples, references to homework/exams/office hours,
    students asking questions. A talk or a seminar counts.
  * "meeting": two or more people coordinating work.
  * "interview": a research or user interview, where one side is mostly asking.
  * "personal": a note to self, a voice memo, a phone call about personal life.
- "course_code": for a lecture, the course number if it is stated or clearly
  implied (e.g. "21241"). Otherwise "".
- "course_name": for a lecture, the course subject (e.g. "Matrices and Linear
  Transformations"). Otherwise "".
- "abstract": string. ONE paragraph, 2-4 sentences, covering what this recording
  was and what came out of it. This is an index entry used to decide whether to
  open the note — keep it tight.
- "detailed_summary": string, markdown. The summary a reader can read on its own
  to understand what happened, in order, without listening to the recording.
  Cover what was discussed or taught, how it developed, the reasoning, the
  examples and numbers used, what was decided or concluded, and what was left
  open. Use short `###` subheadings when the recording had distinct parts
  (`###`, not `##` — this text is placed inside the note's own `## Summary`
  section, and a `##` line would end it).
  Length, proportional to the recording:
    * a short conversation (a few minutes): about 150 words;
    * an hour-long meeting or interview: 600-900 words;
    * a lecture: 500-800 words covering the arc of the class.
  Do not repeat the abstract verbatim, and do not restate the key points and
  action items as a list — those are separate fields below.
- "key_points": array of strings. The most important points, decisions, or takeaways (3-8 items).
- "action_items": array of strings. Concrete follow-ups or todos mentioned (may be empty).
  For a lecture, this is homework, readings, and deadlines the instructor assigned.
- "people": array of strings. Names of people involved or referenced (may be empty).
- "topics": array of strings. Short topic tags, lowercase (2-6 items).
- "sentiment": string. One of "positive", "neutral", "negative", or "mixed".

Transcript title: {title}
Known participants: {participants}
{course_hint}
Transcript:
\"\"\"
{text}
\"\"\""""


def _course_hint(known: dict[str, Course]) -> str:
    """Show the courses already in the vault so a lecture rejoins its own.

    Only the codes seen before — this is not a registry the user maintains,
    it is the index describing itself back to the model. `bind` still
    normalizes whatever comes back, so a course the model invents anyway
    costs nothing worse than a new entity.
    """
    if not known:
        return ""
    listed = ", ".join(
        f"{c.code}" + (f" ({c.name})" if c.name else "")
        for c in sorted(known.values(), key=lambda c: c.key)
    )
    return (
        "Courses already recorded in this vault (reuse the same course_code "
        f"when this lecture belongs to one of them): {listed}\n"
    )


def extraction_prompt(
    transcript: Transcript, known_courses: Optional[dict[str, Course]] = None
) -> tuple[str, str]:
    """The (system, user) pair for one extraction, whichever way it is sent.

    One definition, two transports: the live per-transcript call and the
    batched backfill. A second copy of this prompt would let the backfill
    quietly summarize the vault to a different specification than sync does.
    """
    text = transcript.text
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n...[truncated]"
    user = USER_TEMPLATE.format(
        title=transcript.title,
        participants=", ".join(transcript.participants) or "(unknown)",
        course_hint=_course_hint(known_courses or {}),
        text=text,
    )
    return SYSTEM, user


def extract_insight(
    transcript: Transcript,
    cfg: Config,
    llm: Optional[LLM] = None,
    *,
    known_courses: Optional[dict[str, Course]] = None,
    stage: str = "extract",
) -> Insight:
    llm = llm or LLM(cfg)
    known = known_courses or {}
    system, user = extraction_prompt(transcript, known)
    data = llm.chat_json(
        system, user, schema=INSIGHT_SCHEMA, max_tokens=MAX_TOKENS, stage=stage
    )
    return insight_from_payload(data, transcript, known_courses=known)


def insight_from_payload(
    data: dict,
    transcript: Transcript,
    *,
    known_courses: Optional[dict[str, Course]] = None,
) -> Insight:
    """Turn one extraction response into an Insight.

    Split out from the call so the batch backfill — which gets the same JSON
    back hours later, out of order, from the Batch API — builds its insights
    through exactly this code rather than a parallel copy of it.
    """
    abstract = _as_str(data.get("abstract"))
    detailed = _as_str(data.get("detailed_summary"))
    headline = clean_headline(_as_str(data.get("title")))
    if not headline:
        headline = headline_from_summary(abstract or detailed, fallback=transcript.title)
    kind = coerce_kind(_as_str(data.get("kind")))
    code, name = ("", "")
    if kind == "lecture":
        code, name = bind(
            _as_str(data.get("course_code")),
            _as_str(data.get("course_name")),
            known_courses or {},
        )

    return Insight(
        headline=headline,
        # A model that answered only one of the two still gets a usable note:
        # the abstract falls back to the detailed summary's opening, and the
        # detailed summary falls back to the abstract rather than being blank.
        # Both go through the SAME bound — the model's own field is not
        # trusted to have obeyed "ONE paragraph, 2-4 sentences".
        summary=retrieval_abstract(abstract or detailed),
        detailed_summary=detailed or abstract,
        key_points=_as_list(data.get("key_points")),
        action_items=_as_list(data.get("action_items")),
        people=_as_list(data.get("people")) or list(transcript.participants),
        topics=[t.lower() for t in _as_list(data.get("topics"))],
        category="",  # categories are assigned on demand, not here
        sentiment=_as_str(data.get("sentiment")) or None,
        kind=kind,
        course_code=code,
        course_name=name,
    )


def _as_str(v) -> str:
    if isinstance(v, str):
        return v.strip()
    if v is None:
        return ""
    return str(v).strip()


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []
