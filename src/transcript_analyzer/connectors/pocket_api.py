"""Pocket connector — official public API (https://public.heypocketai.com/api/v1).

Auth: `Authorization: Bearer pk_...` (create in heypocket.com → Settings →
Developer → API Keys). Key goes in config.toml `[pocket] api_key`.

Endpoints:
  GET /public/recordings              -> list recordings (page-based pagination)
  GET /public/recordings/{id}         -> recording detail incl. transcript + summarizations

Preferred over the vault-folder connector when an API key is configured.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

import httpx

from ..config import Config
from ..models import Transcript, stable_id
from ..obsidian import writer

_log = logging.getLogger(__name__)


class PocketAuthError(RuntimeError):
    pass


class AudioStemTaken(RuntimeError):
    """A finished download was discarded: its stem now belongs to another note.

    Distinct from an ordinary download failure because it is an expected
    outcome with a known remedy — fetch it again once this transcript holds a
    stem of its own — rather than a missing or unavailable recording.
    """


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

    def audio_url(self, rec_id: str) -> Optional[str]:
        """Return a short-lived signed URL to the recording's audio, or None."""
        try:
            data = self._get(f"/public/recordings/{rec_id}/audio-url")
        except Exception:  # noqa: BLE001
            return None
        d = data.get("data", data) if isinstance(data, dict) else {}
        return d.get("signed_url") if isinstance(d, dict) else None

    def download_audio(
        self, rec_id: str, dest: "Path", transcript_id: str
    ) -> Optional["Path"]:
        """Download the recording's audio to `dest`. Returns the path, or None.

        The stem is claimed for the whole stream — `writer.audio_partial` is
        what `claimable_stem` reads — and re-proven immediately before the
        replace. A check made at the start cannot carry a multi-minute
        download: a retitle pass or another sync can legitimately take that
        stem meanwhile, and replacing then destroys THEIR recording in a vault
        with no backup. Unproven means the finished file is discarded and
        `AudioStemTaken` is raised, which the sync records as work still owed
        so the next pass fetches it again under a stem this transcript owns.
        """
        if dest.exists() and dest.stat().st_size > 0:
            return dest  # already downloaded
        url = self.audio_url(rec_id)
        if not url:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = writer.audio_partial(dest)
        try:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 16):
                        f.write(chunk)
            if not writer.claimable_stem(
                writer.note_for_audio(dest), transcript_id, in_flight_download=True
            ):
                _log.warning(
                    "discarding the recording downloaded for %s: %s was taken "
                    "while it streamed and is not this transcript's to replace",
                    rec_id, dest,
                )
                tmp.unlink(missing_ok=True)
                raise AudioStemTaken(str(dest))
            tmp.replace(dest)
            return dest
        except AudioStemTaken:
            raise
        except Exception:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            return None

    # ---------- normalization ----------

    @staticmethod
    def _speaker_label(speaker) -> str:
        """Map Pocket's diarization labels to readable names; pass real names through."""
        if not speaker:
            return ""
        m = re.match(r"^SPEAKER_(\d+)$", str(speaker))
        return f"Speaker {int(m.group(1)) + 1}" if m else str(speaker).strip()

    @staticmethod
    def _iter_raw_segments(detail: dict):
        """Yield segment dicts from either public-API or webhook transcript shapes."""
        tr = detail.get("transcript")
        if isinstance(tr, dict):
            for seg in tr.get("segments") or []:
                if isinstance(seg, dict):
                    yield seg
            return
        if isinstance(tr, list):
            for seg in tr:
                if isinstance(seg, dict):
                    yield seg

    @classmethod
    def _transcript_segments(cls, detail: dict) -> list:
        from ..models import TranscriptSegment
        from ..transcript_fmt import coerce_seconds_series

        raw = list(cls._iter_raw_segments(detail))
        # Resolve the timing unit once across the whole recording, not per value.
        timings = coerce_seconds_series(
            [seg.get("start") for seg in raw] + [seg.get("end") for seg in raw]
        )
        starts, ends = timings[: len(raw)], timings[len(raw):]

        segments: list[TranscriptSegment] = []
        for i, seg in enumerate(raw):
            t = (seg.get("text") or "").strip()
            if not t:
                continue
            segments.append(
                TranscriptSegment(
                    text=t,
                    speaker=cls._speaker_label(seg.get("speaker")),
                    start_sec=starts[i],
                    end_sec=ends[i],
                )
            )
        return segments

    @classmethod
    def _transcript_text(cls, detail: dict) -> tuple[str, list]:
        from ..transcript_fmt import format_segments

        segments = cls._transcript_segments(detail)
        if segments:
            return format_segments(segments), segments

        tr = detail.get("transcript") or {}
        if isinstance(tr, dict):
            flat = (tr.get("text") or "").strip()
            if flat:
                return flat, []
        # Last resort: a Pocket summary.
        summ = detail.get("summarizations")
        if isinstance(summ, dict):
            for v in summ.values():
                if isinstance(v, str) and v.strip():
                    return v.strip(), []
                if isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, str) and vv.strip():
                            return vv.strip(), []
        return "", []

    def to_transcript(self, detail: dict) -> Optional[Transcript]:
        rec_id = detail.get("id")
        if not rec_id:
            return None
        text, segments = self._transcript_text(detail)
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
            segments=segments,
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
