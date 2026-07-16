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


class Transcript(BaseModel):
    """A normalized transcript from any source."""

    id: str  # stable_id(source, native_id)
    source: Source
    native_id: str  # granola doc id, or vault file path for pocket
    title: str
    date: _date
    participants: list[str] = Field(default_factory=list)
    text: str
    source_ref: str = ""  # granola doc id, or absolute vault file path
    remote_sort_key: str = ""  # e.g. Granola created_at ISO, for incremental high-water marks

    @property
    def hash(self) -> str:
        return content_hash(self.text)


class Insight(BaseModel):
    """LLM-extracted structured insight for a transcript."""

    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    category: str = ""  # unused during ingestion; categories are assigned on demand
    sentiment: Optional[str] = None


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
    summary: str = ""
    note_path: str = ""  # absolute path to the .md note
    transcript_text: str = ""
