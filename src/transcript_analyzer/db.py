"""SQLite index: transcripts (from vault notes), chunks + embeddings, sync state.

This DB is a *derived* index. The Obsidian markdown notes are the source of truth;
the indexer rebuilds these tables by parsing the vault notes.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

import numpy as np

from .models import NoteRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id   TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    title           TEXT NOT NULL,
    date            TEXT NOT NULL,
    category        TEXT NOT NULL,
    people          TEXT NOT NULL DEFAULT '[]',
    topics          TEXT NOT NULL DEFAULT '[]',
    action_items    TEXT NOT NULL DEFAULT '[]',
    summary         TEXT NOT NULL DEFAULT '',
    note_path       TEXT NOT NULL DEFAULT '',
    transcript_text TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id TEXT NOT NULL,
    ord           INTEGER NOT NULL,
    text          TEXT NOT NULL,
    embedding     BLOB,
    FOREIGN KEY (transcript_id) REFERENCES transcripts(transcript_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_transcript ON chunks(transcript_id);

-- Tracks what's already been ingested/processed, keyed by source + native id.
CREATE TABLE IF NOT EXISTS sync_state (
    source       TEXT NOT NULL,
    native_id    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    note_path    TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL,
    PRIMARY KEY (source, native_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- sync_state ----------

def get_sync_hash(conn: sqlite3.Connection, source: str, native_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT content_hash FROM sync_state WHERE source = ? AND native_id = ?",
        (source, native_id),
    ).fetchone()
    return row["content_hash"] if row else None


def record_sync(
    conn: sqlite3.Connection,
    source: str,
    native_id: str,
    content_hash: str,
    note_path: str,
    processed_at: str,
) -> None:
    conn.execute(
        """INSERT INTO sync_state (source, native_id, content_hash, note_path, processed_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source, native_id) DO UPDATE SET
               content_hash = excluded.content_hash,
               note_path = excluded.note_path,
               processed_at = excluded.processed_at""",
        (source, native_id, content_hash, note_path, processed_at),
    )


# ---------- transcripts (index) ----------

def upsert_transcript(conn: sqlite3.Connection, rec: NoteRecord) -> None:
    conn.execute(
        """INSERT INTO transcripts
             (transcript_id, source, title, date, category, people, topics,
              action_items, summary, note_path, transcript_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(transcript_id) DO UPDATE SET
               source=excluded.source, title=excluded.title, date=excluded.date,
               category=excluded.category, people=excluded.people, topics=excluded.topics,
               action_items=excluded.action_items, summary=excluded.summary,
               note_path=excluded.note_path, transcript_text=excluded.transcript_text""",
        (
            rec.transcript_id, rec.source, rec.title, rec.date, rec.category,
            json.dumps(rec.people), json.dumps(rec.topics), json.dumps(rec.action_items),
            rec.summary, rec.note_path, rec.transcript_text,
        ),
    )


def delete_chunks(conn: sqlite3.Connection, transcript_id: str) -> None:
    conn.execute("DELETE FROM chunks WHERE transcript_id = ?", (transcript_id,))


def insert_chunk(
    conn: sqlite3.Connection,
    transcript_id: str,
    ord_: int,
    text: str,
    embedding: Optional[np.ndarray],
) -> None:
    blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
    conn.execute(
        "INSERT INTO chunks (transcript_id, ord, text, embedding) VALUES (?, ?, ?, ?)",
        (transcript_id, ord_, text, blob),
    )


def _row_to_note(row: sqlite3.Row) -> NoteRecord:
    return NoteRecord(
        transcript_id=row["transcript_id"],
        source=row["source"],
        title=row["title"],
        date=row["date"],
        category=row["category"],
        people=json.loads(row["people"]),
        topics=json.loads(row["topics"]),
        action_items=json.loads(row["action_items"]),
        summary=row["summary"],
        note_path=row["note_path"],
        transcript_text=row["transcript_text"],
    )


def all_transcripts(conn: sqlite3.Connection) -> list[NoteRecord]:
    rows = conn.execute("SELECT * FROM transcripts ORDER BY date DESC, title").fetchall()
    return [_row_to_note(r) for r in rows]


def get_transcript(conn: sqlite3.Connection, transcript_id: str) -> Optional[NoteRecord]:
    row = conn.execute(
        "SELECT * FROM transcripts WHERE transcript_id = ?", (transcript_id,)
    ).fetchone()
    return _row_to_note(row) if row else None


def transcripts_in_category(conn: sqlite3.Connection, category: str) -> list[NoteRecord]:
    rows = conn.execute(
        "SELECT * FROM transcripts WHERE category = ? ORDER BY date DESC", (category,)
    ).fetchall()
    return [_row_to_note(r) for r in rows]


def category_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT category, COUNT(*) AS n FROM transcripts GROUP BY category ORDER BY n DESC, category"
    ).fetchall()
    return [(r["category"], r["n"]) for r in rows]


def load_all_chunk_embeddings(
    conn: sqlite3.Connection,
) -> list[tuple[int, str, str, np.ndarray]]:
    """Return (chunk_id, transcript_id, text, embedding) for all chunks that have embeddings."""
    out: list[tuple[int, str, str, np.ndarray]] = []
    rows = conn.execute(
        "SELECT id, transcript_id, text, embedding FROM chunks WHERE embedding IS NOT NULL"
    ).fetchall()
    for r in rows:
        emb = np.frombuffer(r["embedding"], dtype=np.float32)
        out.append((r["id"], r["transcript_id"], r["text"], emb))
    return out


def clear_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM transcripts")
