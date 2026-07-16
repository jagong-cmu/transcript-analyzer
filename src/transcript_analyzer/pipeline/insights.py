"""Extract structured insights from a transcript using the local LLM."""
from __future__ import annotations

from typing import Optional

from ..config import Config
from ..models import Insight, Transcript
from .categorize import Taxonomy
from .llm import LLM

_MAX_CHARS = 24000  # ~6k tokens of transcript; fits comfortably in num_ctx below
_NUM_CTX = 16384    # room for instructions + transcript + JSON output

# JSON Schema passed to Ollama structured outputs to force the exact shape.
INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "category": {"type": "string"},
        "sentiment": {"type": "string"},
    },
    "required": ["summary", "key_points", "action_items", "people", "topics",
                 "category", "sentiment"],
}

SYSTEM = """You are an assistant that reads a meeting or conversation transcript and
extracts a concise, structured summary. You ALWAYS respond with a single JSON object
and nothing else. Be faithful to the transcript; do not invent facts."""

USER_TEMPLATE = """Analyze the following transcript and return a JSON object with EXACTLY these keys:

- "summary": string. 2-4 sentence overview of what the conversation was about.
- "key_points": array of strings. The most important points, decisions, or takeaways (3-8 items).
- "action_items": array of strings. Concrete follow-ups or todos mentioned (may be empty).
- "people": array of strings. Names of people involved or referenced (may be empty).
- "topics": array of strings. Short topic tags, lowercase (2-6 items).
- "category": string. ONE best-fit category for this conversation. Prefer one of these existing
  categories if it fits: {categories}. Otherwise propose a short new category (1-3 words).
- "sentiment": string. One of "positive", "neutral", "negative", or "mixed".

Transcript title: {title}
Known participants: {participants}

Transcript:
\"\"\"
{text}
\"\"\"

Respond with ONLY the JSON object."""


def extract_insight(
    transcript: Transcript,
    cfg: Config,
    llm: Optional[LLM] = None,
    taxonomy: Optional[Taxonomy] = None,
) -> Insight:
    llm = llm or LLM(cfg)
    taxonomy = taxonomy or Taxonomy(cfg, llm)

    text = transcript.text
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n...[truncated]"

    user = USER_TEMPLATE.format(
        categories=", ".join(taxonomy.categories),
        title=transcript.title,
        participants=", ".join(transcript.participants) or "(unknown)",
        text=text,
    )

    try:
        data = llm.chat_json(
            SYSTEM, user, schema=INSIGHT_SCHEMA,
            options={"temperature": 0.2, "num_ctx": _NUM_CTX},
        )
    except Exception:  # noqa: BLE001 - fall back to a minimal insight on LLM/JSON failure
        data = {}

    insight = Insight(
        summary=_as_str(data.get("summary")),
        key_points=_as_list(data.get("key_points")),
        action_items=_as_list(data.get("action_items")),
        people=_as_list(data.get("people")) or list(transcript.participants),
        topics=[t.lower() for t in _as_list(data.get("topics"))],
        category=_as_str(data.get("category")) or "Uncategorized",
        sentiment=_as_str(data.get("sentiment")) or None,
    )
    # Snap the proposed category onto the controlled, growing taxonomy.
    insight.category = taxonomy.resolve(insight.category)
    return insight


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
