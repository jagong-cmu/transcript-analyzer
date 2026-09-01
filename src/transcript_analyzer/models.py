"""Core data models shared across connectors, pipeline, and web."""
from __future__ import annotations

import hashlib
from datetime import date as _date
from typing import Literal, Optional

from pydantic import BaseModel, Field

Source = Literal["granola", "pocket"]


def stable_id(source: str, native_id: str) -> str:
    """Deterministic short id for a transcript from (source, native id)."""
    h = hashlib.sha1(f"{source}:{native_id}".encode("utf-8")).hexdigest()
    return h[:12]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Attendee(BaseModel):
    """A meeting participant. `email` is the stable identity key (display
    names vary: "Angela Jin" vs "Angela_jin"); it may be empty when the
    source has no email (Pocket, transcript-only speakers)."""

    name: str = ""
    email: str = ""

    @property
    def key(self) -> str:
        """Identity key: lowercased email when present, else normalized name."""
        if self.email.strip():
            return self.email.strip().lower()
        return " ".join(self.name.lower().replace("_", " ").split())


class TranscriptSegment(BaseModel):
    """One timed utterance (or turn) from a source transcript."""

    text: str
    speaker: str = ""
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


class Transcript(BaseModel):
    """A normalized transcript from any source."""

    id: str  # stable_id(source, native_id)
    source: Source
    native_id: str  # granola doc id, or vault file path for pocket
    title: str
    date: _date
    participants: list[str] = Field(default_factory=list)
    attendees: list[Attendee] = Field(default_factory=list)
    text: str
    # Optional timed segments; when present, `text` is usually
    # format_segments(segments). Kept for callers that only need the string.
    segments: list[TranscriptSegment] = Field(default_factory=list)
    source_ref: str = ""  # granola doc id, or absolute vault file path
    remote_sort_key: str = ""  # e.g. Granola created_at ISO, for incremental high-water marks

    @property
    def hash(self) -> str:
        return content_hash(self.text)


# What kind of recording this is. `lecture` is the one that changes the
# pipeline (study notes + a PDF); the rest only tune how long the detailed
# summary should be. Detection is LLM classification inside the existing
# extraction call — there is deliberately no course registry in config.
Kind = Literal["lecture", "meeting", "interview", "personal"]
KINDS: tuple[str, ...] = ("lecture", "meeting", "interview", "personal")
DEFAULT_KIND = "meeting"


def coerce_kind(value: str) -> str:
    """A kind we recognize, or the safe default.

    Anything unrecognized falls back to `meeting`, which is the branch that
    spends nothing extra: an unknown string must never be treated as a lecture
    and buy an Opus study-notes pass.
    """
    v = str(value or "").strip().lower()
    return v if v in KINDS else DEFAULT_KIND


class Insight(BaseModel):
    """LLM-extracted structured insight for a transcript."""

    # Short one-liner describing the conversation (no date). Display title is
    # composed as "{headline}, July 26th, 2026" at write/index time.
    headline: str = ""
    # One paragraph. NOT what the note shows — this is the RETRIEVAL field:
    # digests, dossiers, study rollups, category rollups and the dashboard Ask
    # all read it, and Ask sends every summary on every question. Keeping it
    # short is what keeps that corpus affordable (~58k tokens, against ~165k
    # if the detailed summaries went in instead).
    summary: str = ""
    # What the reader actually gets in the note: length proportional to the
    # conversation (~150 words for a short chat, 600-900 for an hour meeting).
    detailed_summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    category: str = ""  # unused during ingestion; categories are assigned on demand
    sentiment: Optional[str] = None
    kind: str = DEFAULT_KIND
    # Course identity for lectures, model-emitted then normalized against the
    # courses already in the index (see courses.py) so week 2 of 21241 binds
    # to week 1. Empty for everything that is not a lecture.
    course_code: str = ""
    course_name: str = ""

    @property
    def is_lecture(self) -> bool:
        return self.kind == "lecture"


class Visual(BaseModel):
    """One diagram spec for a lecture's study notes.

    A SPEC, never an image: mermaid source, a KaTeX expression, or a code
    listing, all rendered deterministically at PDF time. A spec that fails to
    render is dropped — the transcript has no visual channel, so a diagram is
    already a reconstruction from speech and a *faked* one would be a
    reconstruction of nothing.
    """

    kind: Literal["mermaid", "math", "code"]
    caption: str = ""  # what this illustrates; required at render time
    source: str = ""
    language: str = ""  # for kind="code" (e.g. "sml")


class AsrRepair(BaseModel):
    """A garbled term the study-notes pass corrected.

    `heard` must appear verbatim in the transcript — that is what makes the
    repair auditable rather than a silent rewrite of what the professor said.
    """

    heard: str
    corrected: str


class StudySection(BaseModel):
    """One section of transcript-grounded study notes.

    `anchor` is a verbatim span from the transcript; the citation gate drops
    the whole section when it does not string-match, which is what keeps
    "taught in this lecture" from drifting into "generally true".
    """

    heading: str
    body: str
    anchor: str = ""
    visuals: list[Visual] = Field(default_factory=list)


class BackgroundNote(BaseModel):
    """Gap-filling context that is NOT from the lecture.

    Rendered in its own visually separated block, never blended into the
    grounded sections.
    """

    heading: str
    body: str


class StudyNotes(BaseModel):
    """The lecture profile's output: study-grade notes plus diagram specs."""

    overview: str = ""
    sections: list[StudySection] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    # Anything asserted as assigned, due, or examinable. Transcript-grounded
    # and citation-gated: these are the claims a student would act on.
    assessment: list[str] = Field(default_factory=list)
    background: list[BackgroundNote] = Field(default_factory=list)
    asr_repairs: list[AsrRepair] = Field(default_factory=list)
    dropped_claims: int = 0
    dropped_visuals: int = 0

    @property
    def visuals(self) -> list[Visual]:
        return [v for s in self.sections for v in s.visuals]


class NoteRecord(BaseModel):
    """A row in the derived SQLite index, parsed from an Obsidian insight note."""

    transcript_id: str
    source: str
    title: str
    date: str  # ISO date string
    category: str
    people: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    # Unchecked "- [ ]" items parsed from the note body — the note is the
    # source of truth, so ticking a box in Obsidian closes the commitment.
    open_action_items: list[str] = Field(default_factory=list)
    attendees: list[Attendee] = Field(default_factory=list)
    # The one-paragraph retrieval abstract (frontmatter `abstract:`), NOT the
    # long summary the note shows — every corpus-wide reader uses this field.
    summary: str = ""
    # The long-form summary from the note body's `## Summary` section.
    detailed_summary: str = ""
    kind: str = DEFAULT_KIND
    course_code: str = ""
    course_name: str = ""
    note_path: str = ""  # absolute path to the .md note
    transcript_text: str = ""

    @property
    def is_lecture(self) -> bool:
        return self.kind == "lecture"
