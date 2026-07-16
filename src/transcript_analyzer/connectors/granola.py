"""Granola connector — official public API (https://public-api.granola.ai/v1).

Auth: `Authorization: Bearer grn_...` (an API key created in the Granola desktop
app, Business plan). The key goes in config.toml `[granola] token`.

Endpoints:
  GET /notes                     -> list notes (cursor pagination; created_after filter)
  GET /notes/{id}?include=transcript -> note detail incl. transcript + summary_markdown

Only notes that have a generated summary + transcript are returned by the API.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterator, Optional

import httpx

from ..config import Config
from ..models import Transcript, stable_id


class GranolaAuthError(RuntimeError):
    pass


def _parse_date(val) -> date:
    if not val:
        return date.today()
    s = str(val).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return date.today()


class GranolaClient:
    def __init__(self, cfg: Config) -> None:
        if not cfg.granola.enabled:
            raise GranolaAuthError("No Granola API key configured ([granola] token in config.toml).")
        self.cfg = cfg
        self.base = cfg.granola.api_base.rstrip("/")
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {cfg.granola.token}",
                "Accept": "application/json",
                "User-Agent": "transcript-analyzer/0.1",
            },
            timeout=60,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GranolaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self._client.get(f"{self.base}{path}", params=params or {})
        if r.status_code in (401, 403):
            raise GranolaAuthError(
                f"Granola API returned {r.status_code} — API key missing/invalid/expired. "
                "Check [granola] token in config.toml."
            )
        if r.status_code == 429:
            raise RuntimeError("Granola API rate limit (429). Try again later or sync less often.")
        r.raise_for_status()
        return r.json()

    def list_notes(self, created_after: Optional[str] = None) -> Iterator[dict]:
        """Yield note metadata objects across all pages (newest-first per API)."""
        cursor: Optional[str] = None
        while True:
            params: dict = {}
            if created_after:
                params["created_after"] = created_after
            if cursor:
                params["cursor"] = cursor
            data = self._get("/notes", params)
            for note in data.get("notes", []) or []:
                yield note
            if not data.get("hasMore"):
                break
            cursor = data.get("cursor")
            if not cursor:
                break

    def get_note(self, note_id: str) -> dict:
        return self._get(f"/notes/{note_id}", {"include": "transcript"})

    # ---------- normalization ----------

    @staticmethod
    def _participants(detail: dict) -> list[str]:
        names: list[str] = []
        seen = set()

        def add(n: Optional[str]) -> None:
            n = (n or "").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                names.append(n)

        for att in detail.get("attendees") or []:
            if isinstance(att, dict):
                add(att.get("name") or att.get("email"))
            elif isinstance(att, str):
                add(att)
        # Fall back to / augment with speaker names from the transcript.
        for seg in detail.get("transcript") or []:
            sp = seg.get("speaker") if isinstance(seg, dict) else None
            if isinstance(sp, dict):
                add(sp.get("name"))
        return names

    @staticmethod
    def _channel_labels(detail: dict) -> tuple[str, str]:
        """(owner label, other-party label) for segments that carry no name.

        Granola tags each segment's audio channel: 'microphone' = the recorder
        (owner), 'speaker' = the other party. When there's exactly one other
        attendee we can name them; otherwise fall back to a generic label.
        """
        owner = ((detail.get("owner") or {}).get("name") or "").strip() or "Me"
        others = []
        for a in detail.get("attendees") or []:
            if isinstance(a, dict):
                n = (a.get("name") or "").strip()
                if n and n.lower() != owner.lower():
                    others.append(n)
        other = others[0] if len(others) == 1 else "Speaker"
        return owner, other

    @staticmethod
    def _seg_label(seg: dict, owner: str, other: str) -> str:
        sp = seg.get("speaker") if isinstance(seg.get("speaker"), dict) else {}
        name = (sp.get("name") or "").strip()
        if name:
            return name
        src = sp.get("source")
        if src == "microphone":
            return owner
        if src == "speaker":
            return other
        return ""

    @staticmethod
    def _transcript_text(detail: dict) -> str:
        segments = detail.get("transcript") or []
        owner, other = GranolaClient._channel_labels(detail)
        lines: list[str] = []
        prev = None
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            label = GranolaClient._seg_label(seg, owner, other)
            if label and label != prev:
                lines.append(f"{label}: {text}")
                prev = label
            else:
                lines.append(text)
        text = "\n".join(lines).strip()
        if not text:
            # No spoken transcript available — fall back to Granola's own summary.
            text = (detail.get("summary_markdown") or detail.get("summary_text") or "").strip()
        return text

    def to_transcript(self, detail: dict) -> Optional[Transcript]:
        note_id = detail.get("id")
        if not note_id:
            return None
        text = self._transcript_text(detail)
        if not text:
            return None
        created = detail.get("created_at") or detail.get("updated_at")
        return Transcript(
            id=stable_id("granola", str(note_id)),
            source="granola",
            native_id=str(note_id),
            title=(detail.get("title") or "Untitled Granola note").strip(),
            date=_parse_date(created),
            participants=self._participants(detail),
            text=text,
            source_ref=detail.get("web_url") or str(note_id),
            remote_sort_key=str(created or ""),
        )


def iter_transcripts(
    cfg: Config,
    limit: Optional[int] = None,
    created_after: Optional[str] = None,
) -> Iterator[Transcript]:
    with GranolaClient(cfg) as client:
        fetched = 0
        for note in client.list_notes(created_after=created_after):
            note_id = note.get("id")
            if not note_id:
                continue
            detail = client.get_note(str(note_id))
            t = client.to_transcript(detail)
            if t is None:
                continue
            yield t
            fetched += 1
            if limit is not None and fetched >= limit:
                return
