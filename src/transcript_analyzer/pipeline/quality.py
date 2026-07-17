"""Ingest-time quality floor.

~27% of the Pocket corpus is junk (background-noise recordings, "hello
testing", Pocket onboarding demos). Under the Claude API every transcript is
billable, so junk is filtered *before* any LLM call and never written into
the vault — otherwise the daily digest would faithfully synthesize
"background-noise-recording".
"""
from __future__ import annotations

from typing import Optional

from ..config import Config
from ..models import Transcript


def junk_reason(transcript: Transcript, cfg: Config) -> Optional[str]:
    """Return a human-readable reason this transcript is junk, or None."""
    title = " ".join(transcript.title.lower().replace("-", " ").split())
    for pat in cfg.quality.junk_title_patterns:
        norm = " ".join(pat.lower().replace("-", " ").split())
        if norm and norm in title:
            return f"title matches junk pattern {pat!r}"
    text = transcript.text.strip()
    if len(text) < cfg.quality.min_transcript_chars:
        return (
            f"transcript too short ({len(text)} chars < "
            f"{cfg.quality.min_transcript_chars} minimum)"
        )
    return None
