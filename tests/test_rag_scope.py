"""Scoped Ask corpus selection."""
from conftest import make_record
from transcript_analyzer.db import get_conn, upsert_transcript, set_note_category
from transcript_analyzer import rag


def _seed(cfg, records, categories=None):
    categories = categories or {}
    with get_conn(cfg.db_path) as conn:
        for r in records:
            upsert_transcript(conn, r)
        for tid, cat in categories.items():
            set_note_category(conn, tid, cat)


def test_load_scoped_all(cfg):
    _seed(cfg, [
        make_record(tid="a", title="A", date_str="2026-07-01"),
        make_record(tid="b", title="B", date_str="2026-07-02"),
    ])
    records, label = rag._load_scoped_records(cfg)
    assert len(records) == 2
    assert "all" in label


def test_load_scoped_category(cfg):
    _seed(
        cfg,
        [
            make_record(tid="a", title="Fundraise", date_str="2026-07-01"),
            make_record(tid="b", title="Hire", date_str="2026-07-02"),
            make_record(tid="c", title="Fundraise 2", date_str="2026-07-03"),
        ],
        categories={"a": "Fundraising", "c": "Fundraising", "b": "Hiring"},
    )
    records, label = rag._load_scoped_records(cfg, category="Fundraising")
    assert {r.transcript_id for r in records} == {"a", "c"}
    assert "Fundraising" in label


def test_load_scoped_transcript(cfg):
    _seed(cfg, [make_record(tid="only", title="Solo chat", date_str="2026-07-01")])
    records, label = rag._load_scoped_records(cfg, transcript_id="only")
    assert len(records) == 1
    assert records[0].transcript_id == "only"
    assert "Solo chat" in label


def test_load_scoped_missing_raises(cfg):
    import pytest
    with pytest.raises(ValueError, match="No transcript"):
        rag._load_scoped_records(cfg, transcript_id="missing")
    with pytest.raises(ValueError, match="has no conversations"):
        rag._load_scoped_records(cfg, category="Ghost")
