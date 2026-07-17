"""Category rollups: citation gate + change detection + categorize wiring."""
from conftest import make_record

from transcript_analyzer.db import get_conn, set_note_category, upsert_transcript
from transcript_analyzer.obsidian.writer import SYNTH_BEGIN
from transcript_analyzer.pipeline.organize import (
    categorize,
    reset_categories,
    write_category_rollup,
)
from transcript_analyzer.web import synth_reader


class FakeLLM:
    def __init__(self, classify_map=None, rollup=None):
        self.classify_map = classify_map or {}
        self.rollup = rollup or {
            "overview": "Fundraising is the active thread.",
            "themes": [],
            "open_threads": [],
        }
        self.calls = 0

    def chat_json(self, system, user, schema, **kw):
        self.calls += 1
        props = schema.get("properties", {})
        if "category" in props:
            for needle, cat in self.classify_map.items():
                if needle.lower() in user.lower():
                    return {"category": cat}
            return {"category": "None"}
        return self.rollup


def test_write_category_rollup_citation_gate(cfg):
    notes = [
        make_record(
            tid="t1",
            title="2026-07-01 lp-call",
            open_items=["Send deck"],
            summary="Angela agreed to review the pricing deck by Friday.",
        )
    ]
    llm = FakeLLM(
        rollup={
            "overview": "Pricing is the bottleneck.",
            "themes": [
                {
                    "text": "Angela will review the deck.",
                    "source_id": "t1",
                    "quote": "review the pricing deck by Friday",
                },
                {
                    "text": "Fabricated theme.",
                    "source_id": "t1",
                    "quote": "nowhere in the source",
                },
            ],
            "open_threads": [
                {
                    "text": "Still waiting on legal.",
                    "source_id": "t1",
                    "quote": "review the pricing deck",
                }
            ],
        }
    )
    out = write_category_rollup(cfg, llm, "Fundraising", notes, force=True)
    assert out["dropped_claims"] == 1
    assert out["themes"] == 1
    path = cfg.vault.insights_path / "Categories" / "Fundraising.md"
    text = path.read_text()
    assert SYNTH_BEGIN in text
    assert "Angela will review the deck." in text
    assert "Fabricated theme." not in text
    assert "- [ ] Send deck" in text
    assert "[[2026-07-01 lp-call]]" in text


def test_category_rollup_change_detection(cfg):
    notes = [make_record(tid="t1", title="2026-07-01 a")]
    llm = FakeLLM(
        rollup={
            "overview": "Steady.",
            "themes": [
                {
                    "text": "Angela will review.",
                    "source_id": "t1",
                    "quote": "review the pricing deck",
                }
            ],
            "open_threads": [],
        }
    )
    write_category_rollup(cfg, llm, "Product", notes, force=True)
    assert llm.calls == 1
    out = write_category_rollup(cfg, llm, "Product", notes, force=False)
    assert out == {"unchanged": 1}
    assert llm.calls == 1


def test_categorize_writes_rollups(cfg):
    notes = [
        make_record(tid="t1", title="2026-07-01 raise", summary="LP call about pricing deck review."),
        make_record(tid="t2", title="2026-07-02 hire", summary="Interview loop."),
    ]
    with get_conn(cfg.db_path) as conn:
        for n in notes:
            upsert_transcript(conn, n)

    llm = FakeLLM(
        classify_map={"raise": "Fundraising", "hire": "Hiring"},
        rollup={
            "overview": "Scoped overview.",
            "themes": [
                {
                    "text": "Pricing came up.",
                    "source_id": "t1",
                    "quote": "pricing deck",
                }
            ],
            "open_threads": [],
        },
    )
    # Rollup FakeLLM returns same payload for both categories; t2 claim will drop for Hiring.
    summary = categorize(cfg, ["Fundraising", "Hiring"], llm=llm, verbose=False)
    assert summary["assigned"] == 2
    assert "Fundraising" in summary["rollups"]
    assert "Hiring" in summary["rollups"]
    assert (cfg.vault.insights_path / "Categories" / "Fundraising.md").exists()
    assert (cfg.vault.insights_path / "Categories" / "Hiring.md").exists()

    by_stem = synth_reader.stem_index(notes)
    insight = synth_reader.load_category_insight(cfg, "Fundraising", by_stem)
    assert insight.exists
    assert "Scoped overview" in insight.overview


def test_reset_clears_hashes(cfg):
    notes = [make_record(tid="t1", title="2026-07-01 a")]
    with get_conn(cfg.db_path) as conn:
        upsert_transcript(conn, notes[0])
        set_note_category(conn, "t1", "X")
    write_category_rollup(
        cfg,
        FakeLLM(rollup={"overview": "x", "themes": [], "open_threads": []}),
        "X",
        notes,
        force=True,
    )
    reset_categories(cfg, verbose=False)
    assert not (cfg.vault.insights_path / "Categories").exists()
    with get_conn(cfg.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM meta WHERE key LIKE 'category_hash:%'"
        ).fetchone()["c"]
    assert n == 0
