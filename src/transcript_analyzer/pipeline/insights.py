"""Extract structured insights from a transcript using the Claude API.

Note: this does NOT assign a category. Notes are organized by date; categories
are created on demand via the `categorize` command (see pipeline/organize.py).

Failures propagate (LLMError and subclasses) — under a paid API we never
write an empty note we were billed for; the sync loop counts the failure and
retries the transcript on a later cycle.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config
from ..models import Insight, Transcript
from .llm import LLM

# Cost sanity cap, not a context-window limit (the 1M window fits any
# transcript this system will ever see). ~25k tokens of transcript.
_MAX_CHARS = 100_000

INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "mixed"]},
    },
    "required": ["summary", "key_points", "action_items", "people", "topics", "sentiment"],
}

SYSTEM = """You are an assistant that reads a meeting or conversation transcript and
extracts a concise, structured summary. Be faithful to the transcript; do not invent facts."""

USER_TEMPLATE = """Analyze the following transcript and return a JSON object with EXACTLY these keys:

- "summary": string. 2-4 sentence overview of what the conversation was about.
- "key_points": array of strings. The most important points, decisions, or takeaways (3-8 items).
- "action_items": array of strings. Concrete follow-ups or todos mentioned (may be empty).
- "people": array of strings. Names of people involved or referenced (may be empty).
- "topics": array of strings. Short topic tags, lowercase (2-6 items).
- "sentiment": string. One of "positive", "neutral", "negative", or "mixed".

Transcript title: {title}
Known participants: {participants}

Transcript:
\"\"\"
{text}
\"\"\""""


def extract_insight(
    transcript: Transcript,
    cfg: Config,
    llm: Optional[LLM] = None,
) -> Insight:
    llm = llm or LLM(cfg)

    text = transcript.text
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n...[truncated]"

    user = USER_TEMPLATE.format(
        title=transcript.title,
        participants=", ".join(transcript.participants) or "(unknown)",
        text=text,
    )

    data = llm.chat_json(SYSTEM, user, schema=INSIGHT_SCHEMA)

    return Insight(
        summary=_as_str(data.get("summary")),
        key_points=_as_list(data.get("key_points")),
        action_items=_as_list(data.get("action_items")),
        people=_as_list(data.get("people")) or list(transcript.participants),
        topics=[t.lower() for t in _as_list(data.get("topics"))],
        category="",  # categories are assigned on demand, not here
        sentiment=_as_str(data.get("sentiment")) or None,
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
