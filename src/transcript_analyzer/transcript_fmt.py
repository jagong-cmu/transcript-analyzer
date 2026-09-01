"""Timestamped transcript lines for notes and clickable audio seek in the UI.

Canonical line form when timing is known:

    [1:23] Speaker: hello there

Indexer and the web UI parse that prefix — ``[M:SS]``, or ``[H:MM:SS]`` past an
hour; a leading-zero minute is accepted on the way in. Clicking it seeks the
HTML5 audio player (Pocket recordings that have an mp3 in Attachments/).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

from .models import TranscriptSegment

# [1:02:03] or [01:23] at start of a line
TS_PREFIX_RE = re.compile(
    r"^\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]\s*(.*)$"
)


def format_timestamp(seconds: float) -> str:
    """Seconds → ``[M:SS]`` or ``[H:MM:SS]`` (no leading zero on hours)."""
    if seconds < 0 or seconds != seconds:  # NaN
        seconds = 0.0
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"[{h}:{m:02d}:{s:02d}]"
    return f"[{m}:{s:02d}]"


def parse_timestamp_prefix(line: str) -> tuple[Optional[float], str]:
    """Return (seconds_or_None, remainder_of_line)."""
    m = TS_PREFIX_RE.match(line.strip())
    if not m:
        return None, line
    h = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    rest = m.group(4)
    return float(h * 3600 + mm * 60 + ss), rest


# Neither Pocket nor Granola declares the unit of its timing fields, so it is
# inferred: anything at or past this mark is milliseconds. The inference is made
# once per transcript (see coerce_seconds_series) rather than per value —
# per-value it would read the first 2h46m of a long recording as seconds and
# everything after it as milliseconds, collapsing the tail to a few seconds and
# breaking the ordering mid-transcript. A recording genuinely longer than
# MS_THRESHOLD seconds (~2h46m) reported in seconds is still misread; the source
# APIs give nothing better to disambiguate on.
MS_THRESHOLD = 10_000


def _as_number(value) -> Optional[float]:
    """Parse a raw timing field to a non-negative number, unit unresolved."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            v = value.strip()
            # ISO-8601 wall clock — caller should convert relatively; skip here.
            if "T" in v or v.endswith("Z"):
                return None
            num = float(v)
        else:
            num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0 or num != num:  # negative or NaN
        return None
    return num


def coerce_seconds(value) -> Optional[float]:
    """Normalize one API timing field to seconds. Values ≥ 10_000 treated as ms.

    Prefer coerce_seconds_series when normalizing a whole transcript, so every
    segment in it resolves against the same unit.
    """
    num = _as_number(value)
    if num is None:
        return None
    return num / 1000.0 if num >= MS_THRESHOLD else num


def coerce_seconds_series(values: Iterable) -> list[Optional[float]]:
    """Normalize a transcript's timing fields, picking the unit once for all.

    If any value in the series looks like milliseconds, the whole series is
    milliseconds — that keeps a single transcript internally consistent and
    monotonic even when it runs past the ambiguity threshold.
    """
    nums = [_as_number(v) for v in values]
    div = 1000.0 if any(n is not None and n >= MS_THRESHOLD for n in nums) else 1.0
    return [None if n is None else n / div for n in nums]


def iso_to_epoch(value) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def relative_seconds_from_iso(timestamps: list[Optional[str]]) -> list[Optional[float]]:
    """Convert a list of ISO wall-clock times to seconds from the first stamp."""
    epochs = [iso_to_epoch(t) for t in timestamps]
    base = next((e for e in epochs if e is not None), None)
    if base is None:
        return [None] * len(timestamps)
    out: list[Optional[float]] = []
    for e in epochs:
        out.append(None if e is None else max(0.0, e - base))
    return out


def format_segments(segments: Iterable[TranscriptSegment]) -> str:
    """Render segments as timestamped, speaker-attributed lines."""
    lines: list[str] = []
    prev_speaker: Optional[str] = None
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        speaker = (seg.speaker or "").strip()
        prefix = ""
        if seg.start_sec is not None:
            prefix = format_timestamp(seg.start_sec) + " "
        if speaker and speaker != prev_speaker:
            lines.append(f"{prefix}{speaker}: {text}")
            prev_speaker = speaker
        elif speaker:
            lines.append(f"{prefix}{text}")
        else:
            lines.append(f"{prefix}{text}" if prefix else text)
    return "\n".join(lines)


def parse_transcript_lines(text: str) -> list[dict]:
    """Split transcript text into {start_sec, ts_label, text} dicts for the web UI."""
    rows: list[dict] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            rows.append({"start_sec": None, "ts_label": "", "text": ""})
            continue
        sec, rest = parse_timestamp_prefix(raw)
        if sec is None:
            rows.append({"start_sec": None, "ts_label": "", "text": raw})
        else:
            # Strip brackets from format_timestamp for the button label.
            label = format_timestamp(sec).strip("[]")
            rows.append({"start_sec": sec, "ts_label": label, "text": rest})
    return rows
