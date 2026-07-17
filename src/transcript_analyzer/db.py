"""SQLite index: transcripts (from vault notes), sync state, LLM spend ledger.

This DB is a *derived* index. The Obsidian markdown notes are the source of
truth; the indexer rebuilds these tables by parsing the vault notes. The one
exception is llm_spend, which is primary data (the cost guard's ledger).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .models import NoteRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id     TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    title             TEXT NOT NULL,
    date              TEXT NOT NULL,
    category          TEXT NOT NULL,
    people            TEXT NOT NULL DEFAULT '[]',
    topics            TEXT NOT NULL DEFAULT '[]',
    action_items      TEXT NOT NULL DEFAULT '[]',
    open_action_items TEXT NOT NULL DEFAULT '[]',
    attendees         TEXT NOT NULL DEFAULT '[]',
    summary           TEXT NOT NULL DEFAULT '',
    note_path         TEXT NOT NULL DEFAULT '',
    transcript_text   TEXT NOT NULL DEFAULT ''
);

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

-- On-demand category assignments (populated by the `categorize` command).
-- A note may belong to multiple categories.
CREATE TABLE IF NOT EXISTS note_categories (
    transcript_id TEXT NOT NULL,
    category      TEXT NOT NULL,
    PRIMARY KEY (transcript_id, category),
    FOREIGN KEY (transcript_id) REFERENCES transcripts(transcript_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_note_categories_cat ON note_categories(category);

-- Claude API spend ledger (per calendar month). Primary data, not derived.
CREATE TABLE IF NOT EXISTS llm_spend (
    month              TEXT PRIMARY KEY,  -- YYYY-MM
    calls              INTEGER NOT NULL DEFAULT 0,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    usd                REAL NOT NULL DEFAULT 0
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    # Embeddings were removed with the move to the Claude API (agentic
    # retrieval over whole notes replaced vector RAG).
    conn.execute("DROP TABLE IF EXISTS chunks")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transcripts)")}
    if cols:
        for col in ("open_action_items", "attendees"):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE transcripts ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'"
                )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")  # wait out concurrent writers
    conn.execute("PRAGMA journal_mode = WAL")     # better concurrent read/write
    _migrate(conn)
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


def get_sync_note_path(conn: sqlite3.Connection, source: str, native_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT note_path FROM sync_state WHERE source = ? AND native_id = ?",
        (source, native_id),
    ).fetchone()
    return row["note_path"] if row and row["note_path"] else None


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
              action_items, open_action_items, attendees, summary, note_path,
              transcript_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(transcript_id) DO UPDATE SET
               source=excluded.source, title=excluded.title, date=excluded.date,
               category=excluded.category, people=excluded.people, topics=excluded.topics,
               action_items=excluded.action_items,
               open_action_items=excluded.open_action_items,
               attendees=excluded.attendees,
               summary=excluded.summary,
               note_path=excluded.note_path, transcript_text=excluded.transcript_text""",
        (
            rec.transcript_id, rec.source, rec.title, rec.date, rec.category,
            json.dumps(rec.people), json.dumps(rec.topics), json.dumps(rec.action_items),
            json.dumps(rec.open_action_items),
            json.dumps([a.model_dump() for a in rec.attendees]),
            rec.summary, rec.note_path, rec.transcript_text,
        ),
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
        open_action_items=json.loads(row["open_action_items"]),
        attendees=json.loads(row["attendees"]),
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
        """SELECT t.* FROM transcripts t
             JOIN note_categories nc ON nc.transcript_id = t.transcript_id
            WHERE nc.category = ? ORDER BY t.date DESC""",
        (category,),
    ).fetchall()
    return [_row_to_note(r) for r in rows]


def category_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        """SELECT category, COUNT(*) AS n FROM note_categories
            GROUP BY category ORDER BY n DESC, category"""
    ).fetchall()
    return [(r["category"], r["n"]) for r in rows]


def categories_for(conn: sqlite3.Connection, transcript_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT category FROM note_categories WHERE transcript_id = ? ORDER BY category",
        (transcript_id,),
    ).fetchall()
    return [r["category"] for r in rows]


def clear_note_categories(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM note_categories")


def set_note_category(conn: sqlite3.Connection, transcript_id: str, category: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO note_categories (transcript_id, category) VALUES (?, ?)",
        (transcript_id, category),
    )


def clear_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM transcripts")


# ---------- meta (key/value, e.g. incremental-sync high-water marks) ----------

def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------- llm_spend (cost-guard ledger) ----------

def add_llm_spend(
    conn: sqlite3.Connection,
    month: str,
    calls: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    usd: float,
) -> None:
    conn.execute(
        """INSERT INTO llm_spend
             (month, calls, input_tokens, output_tokens,
              cache_read_tokens, cache_write_tokens, usd)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(month) DO UPDATE SET
               calls = calls + excluded.calls,
               input_tokens = input_tokens + excluded.input_tokens,
               output_tokens = output_tokens + excluded.output_tokens,
               cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
               cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
               usd = usd + excluded.usd""",
        (month, calls, input_tokens, output_tokens,
         cache_read_tokens, cache_write_tokens, usd),
    )


def get_llm_spend(conn: sqlite3.Connection, month: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM llm_spend WHERE month = ?", (month,)
    ).fetchone()
