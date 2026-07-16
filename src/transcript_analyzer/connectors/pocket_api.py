"""Pocket connector — official public API (https://public.heypocketai.com/api/v1).

Auth: `Authorization: Bearer pk_...` (create in heypocket.com → Settings →
Developer → API Keys). Key goes in config.toml `[pocket] api_key`.

Endpoints:
  GET /public/recordings              -> list recordings (page-based pagination)
  GET /public/recordings/{id}         -> recording detail incl. transcript + summarizations

Preferred over the vault-folder connector when an API key is configured.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterator, Optional

import httpx

from ..config import Config
from ..models import Transcript, stable_id


class PocketAuthError(RuntimeError):
    pass


def _parse_date(val) -> date:
    if not val:
        return date.today()
    s = str(val).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return date.today()


class PocketClient:
    def __init__(self, cfg: Config) -> None:
        if not cfg.pocket.api_enabled:
            raise PocketAuthError("No Pocket API key configured ([pocket] api_key in config.toml).")
        self.cfg = cfg
        self.base = cfg.pocket.api_base.rstrip("/")
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {cfg.pocket.api_key}",
                "Accept": "application/json",
                "User-Agent": "transcript-analyzer/0.1",
            },
            timeout=60,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PocketClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self._client.get(f"{self.base}{path}", params=params or {})
        if r.status_code in (401, 403):
            raise PocketAuthError(
                f"Pocket API returned {r.status_code} — API key missing/invalid. "
                "Check [pocket] api_key in config.toml."
            )
        if r.status_code == 429:
            raise RuntimeError("Pocket API rate limit (429). Try again later.")
        r.raise_for_status()
        return r.json()

    def list_recordings(self) -> Iterator[dict]:
        page = 1
        while True:
            data = self._get("/public/recordings", {"page": page, "limit": 50})
            for rec in data.get("data", []) or []:
                yield rec
            pag = data.get("pagination") or {}
            if not pag.get("has_more"):
                break
            page += 1

    def get_recording(self, rec_id: str) -> dict:
        data = self._get(f"/public/recordings/{rec_id}")
        return data.get("data", data)

    # ---------- normalization ----------

    @staticmethod
    def _transcript_text(detail: dict) -> str:
        tr = detail.get("transcript") or {}
        if isinstance(tr, dict):
            # Prefer the flat text; fall back to joining diarized segments.
            text = (tr.get("text") or "").strip()
            if text:
                return text
            lines = []
            for seg in tr.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                t = (seg.get("text") or "").strip()
                if not t:
                    continue
                sp = seg.get("speaker") or ""
                lines.append(f"{sp}: {t}" if sp else t)
            if lines:
                return "\n".join(lines)
        # Last resort: a Pocket summary.
        summ = detail.get("summarizations")
        if isinstance(summ, dict):
            for v in summ.values():
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, str) and vv.strip():
                            return vv.strip()
        return ""

    def to_transcript(self, detail: dict) -> Optional[Transcript]:
        rec_id = detail.get("id")
        if not rec_id:
            return None
        text = self._transcript_text(detail)
        if not text:
            return None
        created = detail.get("recording_at") or detail.get("created_at")
        title = (detail.get("title") or "Untitled Pocket recording").strip()
        # Pocket diarizes speakers anonymously (SPEAKER_01…), so no real names.
        return Transcript(
            id=stable_id("pocket", str(rec_id)),
            source="pocket",
            native_id=str(rec_id),
            title=title,
            date=_parse_date(created),
            participants=[],
            text=text,
            source_ref=str(rec_id),
            remote_sort_key=str(detail.get("created_at") or created or ""),
        )


def iter_transcripts(
    cfg: Config,
    limit: Optional[int] = None,
    created_after: Optional[str] = None,
) -> Iterator[Transcript]:
    with PocketClient(cfg) as client:
        fetched = 0
        for rec in client.list_recordings():
            rec_id = rec.get("id")
            if not rec_id:
                continue
            # Only completed recordings have transcripts.
            if rec.get("state") not in (None, "completed"):
                continue
            # Incremental: skip anything at/before the high-water mark without fetching detail.
            if created_after and str(rec.get("created_at") or "") <= created_after:
                continue
            detail = client.get_recording(str(rec_id))
            t = client.to_transcript(detail)
            if t is None:
                continue
            yield t
            fetched += 1
            if limit is not None and fetched >= limit:
                return
