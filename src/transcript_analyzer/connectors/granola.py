"""Granola connector.

Granola encrypts its local cache + auth token, so we pull from Granola's cloud
API using a bearer token the user pastes into config.toml ([granola] token).

Endpoints (Granola's private API, as used by community tools):
  POST {api_base}/v2/get-documents           -> list documents (paginated)
  POST {api_base}/v1/get-document-transcript -> transcript segments for a document

If Granola changes these, adjust here; everything else is source-agnostic.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterator, Optional

import httpx

from ..config import Config
from ..models import Transcript, stable_id


class GranolaAuthError(RuntimeError):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "transcript-analyzer/0.1 (personal)",
        "X-Client-Type": "electron",
    }


def _parse_dt(val) -> date:
    if not val:
        return date.today()
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val / 1000 if val > 1e12 else val).date()
    s = str(val).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return date.today()


def _prosemirror_text(node) -> str:
    """Flatten a ProseMirror/Granola notes document into plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    parts: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text" and "text" in node:
            parts.append(node["text"])
        for child in node.get("content", []) or []:
            parts.append(_prosemirror_text(child))
        if node.get("type") in ("paragraph", "heading", "listItem", "bulletList"):
            parts.append("\n")
    elif isinstance(node, list):
        for child in node:
            parts.append(_prosemirror_text(child))
    return "".join(parts)


class GranolaClient:
    def __init__(self, cfg: Config) -> None:
        if not cfg.granola.enabled:
            raise GranolaAuthError("No Granola token configured ([granola] token in config.toml).")
        self.cfg = cfg
        self.base = cfg.granola.api_base.rstrip("/")
        self._client = httpx.Client(headers=_headers(cfg.granola.token), timeout=60)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GranolaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _post(self, path: str, body: dict) -> dict:
        r = self._client.post(f"{self.base}{path}", json=body)
        if r.status_code in (401, 403):
            raise GranolaAuthError(
                f"Granola API returned {r.status_code} — token missing/expired. "
                "Re-paste your token into config.toml."
            )
        r.raise_for_status()
        return r.json()

    def list_documents(self, limit: int = 100, offset: int = 0) -> list[dict]:
        data = self._post("/v2/get-documents", {"limit": limit, "offset": offset})
        # Response shape varies; try common keys.
        if isinstance(data, list):
            return data
        for key in ("docs", "documents", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    def transcript_text(self, doc_id: str) -> str:
        try:
            data = self._post("/v1/get-document-transcript", {"document_id": doc_id})
        except httpx.HTTPError:
            return ""
        segments = data if isinstance(data, list) else data.get("segments", [])
        lines: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            speaker = seg.get("speaker") or seg.get("source") or ""
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"{speaker}: {text}" if speaker else text)
        return "\n".join(lines)

    def _people(self, doc: dict) -> list[str]:
        out: list[str] = []
        people = doc.get("people") or doc.get("attendees") or []
        for p in people:
            if isinstance(p, dict):
                name = p.get("name") or p.get("display_name") or p.get("email")
                if name:
                    out.append(str(name))
            elif isinstance(p, str):
                out.append(p)
        return out

    def to_transcript(self, doc: dict) -> Optional[Transcript]:
        doc_id = doc.get("id") or doc.get("document_id")
        if not doc_id:
            return None
        title = doc.get("title") or doc.get("name") or "Untitled Granola note"
        when = _parse_dt(doc.get("created_at") or doc.get("created") or doc.get("date"))

        # Prefer the spoken transcript; fall back to the note body (notes / last_viewed_panel).
        text = self.transcript_text(str(doc_id))
        if not text:
            notes = doc.get("notes") or doc.get("notes_markdown") or doc.get("content")
            text = _prosemirror_text(notes) if isinstance(notes, (dict, list)) else (notes or "")
        text = (text or "").strip()
        if not text:
            return None

        return Transcript(
            id=stable_id("granola", str(doc_id)),
            source="granola",
            native_id=str(doc_id),
            title=str(title).strip(),
            date=when,
            participants=self._people(doc),
            text=text,
            source_ref=str(doc_id),
        )


def iter_transcripts(cfg: Config, limit: Optional[int] = None) -> Iterator[Transcript]:
    with GranolaClient(cfg) as client:
        fetched = 0
        offset = 0
        page_size = 100
        while True:
            docs = client.list_documents(limit=page_size, offset=offset)
            if not docs:
                break
            for doc in docs:
                t = client.to_transcript(doc)
                if t is not None:
                    yield t
                    fetched += 1
                    if limit is not None and fetched >= limit:
                        return
            if len(docs) < page_size:
                break
            offset += page_size
