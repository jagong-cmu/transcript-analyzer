"""Agentic Q&A over the indexed transcripts.

No embeddings, no top-k: at personal-corpus scale (~12k tokens of summaries)
Claude reads the summary of EVERY conversation in context, then pulls whole
notes on demand via the fetch_transcript tool. Whole notes mean speaker
labels stay intact, dates come from frontmatter, and proper nouns are exact
text — the three defects vector retrieval had.

The corpus block carries a cache_control breakpoint, so repeated questions
within a session pay ~0.1x for the corpus prefix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterator, Optional

from .config import Config, load_config
from .db import all_transcripts, get_conn
from .models import NoteRecord
from .pipeline.llm import LLM

_MAX_ROUNDS = 6
_MAX_NOTE_CHARS = 60_000
_CITATION_RE = re.compile(r"\[(\d+)\]")


@dataclass
class Retrieved:
    transcript_id: str
    title: str
    note_path: str


SYSTEM = """You answer questions about the user's own meeting and conversation
transcripts. You are given an index of EVERY conversation they have recorded:
one numbered entry per conversation with its id, date, title, people, and
summary. Read the whole index — do not skim.

When a summary is not enough (exact wording, who said what, numbers,
specifics), call fetch_transcript with the conversation's id to read the full
note. Fetch as many as you need, in parallel when independent.

Cite conversations inline as [n], where n is the entry's number in the index.
If the index doesn't contain the answer, say so plainly. Be concise and
specific."""

FETCH_TOOL = {
    "name": "fetch_transcript",
    "description": (
        "Read the full note for one conversation from the index: metadata, "
        "summary, and the complete transcript with speaker labels. Use this "
        "whenever the index summary is not enough to answer precisely."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transcript_id": {
                "type": "string",
                "description": "The `id` field of an index entry.",
            }
        },
        "required": ["transcript_id"],
        "additionalProperties": False,
    },
}


def _corpus(records: list[NoteRecord]) -> tuple[str, dict[int, NoteRecord]]:
    lines = ["CONVERSATION INDEX (newest first):", ""]
    by_num: dict[int, NoteRecord] = {}
    for n, rec in enumerate(records, 1):
        by_num[n] = rec
        people = ", ".join(rec.people) or "(unknown)"
        summary = " ".join(rec.summary.split()) or "(no summary)"
        lines.append(
            f"[{n}] id={rec.transcript_id} | {rec.date} | {rec.title} | people: {people}"
        )
        lines.append(f"    {summary}")
    return "\n".join(lines), by_num


def _render_full(rec: NoteRecord) -> str:
    text = rec.transcript_text or "(no transcript text)"
    if len(text) > _MAX_NOTE_CHARS:
        text = text[:_MAX_NOTE_CHARS] + "\n...[truncated]"
    return (
        f"Title: {rec.title}\nDate: {rec.date}\nPeople: {', '.join(rec.people)}\n\n"
        f"Summary:\n{rec.summary}\n\nTranscript:\n{text}"
    )


def stream_events(
    question: str,
    cfg: Optional[Config] = None,
    llm: Optional[LLM] = None,
) -> Iterator[tuple[str, object]]:
    """Yield ("token", str) as the answer streams, then ("sources", [dict])
    mapping the [n] citations in the answer to transcripts."""
    cfg = cfg or load_config()
    llm = llm or LLM(cfg)

    with get_conn(cfg.db_path) as conn:
        records = all_transcripts(conn)
    corpus, by_num = _corpus(records)
    by_id = {r.transcript_id: r for r in records}

    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": corpus,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"Today's date: {date.today().isoformat()}\n\nQuestion: {question}",
                },
            ],
        }
    ]

    text_parts: list[str] = []
    for _round in range(_MAX_ROUNDS):
        with llm.stream(
            system=SYSTEM,
            messages=messages,
            tools=[FETCH_TOOL],
            thinking={"type": "adaptive"},
        ) as s:
            for tok in s.text_stream:
                text_parts.append(tok)
                yield ("token", tok)
            final = s.get_final_message()
        if final.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": final.content})
        results = []
        for block in final.content:
            if block.type != "tool_use":
                continue
            tid = str(block.input.get("transcript_id", "")).strip()
            rec = by_id.get(tid)
            if rec is not None:
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id,
                     "content": _render_full(rec)}
                )
            else:
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id,
                     "content": f"No transcript with id {tid!r} in the index.",
                     "is_error": True}
                )
        messages.append({"role": "user", "content": results})
    else:
        yield ("token", "\n\n[Stopped after too many retrieval rounds.]")

    answer = "".join(text_parts)
    cited = sorted(
        {int(m) for m in _CITATION_RE.findall(answer) if int(m) in by_num}
    )
    yield (
        "sources",
        [
            {
                "n": n,
                "id": by_num[n].transcript_id,
                "title": by_num[n].title,
                "note_path": by_num[n].note_path,
            }
            for n in cited
        ],
    )


def answer(
    question: str,
    cfg: Optional[Config] = None,
    llm: Optional[LLM] = None,
) -> dict:
    """Non-streaming convenience wrapper around stream_events()."""
    tokens: list[str] = []
    sources: list[dict] = []
    for kind, payload in stream_events(question, cfg=cfg, llm=llm):
        if kind == "token":
            tokens.append(payload)  # type: ignore[arg-type]
        elif kind == "sources":
            sources = payload  # type: ignore[assignment]
    return {"answer": "".join(tokens), "sources": sources}
