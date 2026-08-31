"""Category rollups: citation gate + change detection + categorize wiring."""
from conftest import make_record

from transcript_analyzer.db import get_conn, get_meta, set_note_category, upsert_transcript
from transcript_analyzer.obsidian.writer import SYNTH_BEGIN
from transcript_analyzer.pipeline.organize import (
    DEFS_META_KEY,
    CategoryDef,
    categorize,
    load_category_definitions,
    normalize_categories,
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
        self.users: list[str] = []

    def chat_json(self, system, user, schema, **kw):
        self.calls += 1
        self.users.append(user)
        props = schema.get("properties", {})
        if "category" in props:
            for needle, cat in self.classify_map.items():
                if needle.lower() in user.lower():
                    return {"category": cat}
            return {"category": "None"}
        return self.rollup


def test_normalize_name_colon_description():
    defs = normalize_categories(
        [
            "Fundraising: LP updates and term sheets",
            "Hiring",
            {"name": "Product", "description": "Roadmap"},
        ]
    )
    assert defs[0] == CategoryDef("Fundraising", "LP updates and term sheets")
    assert defs[1] == CategoryDef("Hiring", "")
    assert defs[2] == CategoryDef("Product", "Roadmap")


def test_classify_prompt_includes_description(cfg):
    notes = [
        make_record(
            tid="t1",
            title="2026-07-01 raise",
            summary="Talked to an LP about the round.",
        )
    ]
    with get_conn(cfg.db_path) as conn:
        upsert_transcript(conn, notes[0])
    llm = FakeLLM(classify_map={"raise": "Fundraising"})
    categorize(
        cfg,
        [CategoryDef("Fundraising", "Investor updates, term sheets, raise strategy")],
        llm=llm,
        verbose=False,
    )
    assert any("Investor updates, term sheets, raise strategy" in u for u in llm.users)
    defs = load_category_definitions(cfg)
    assert defs[0].description.startswith("Investor updates")


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
    out = write_category_rollup(
        cfg, llm, "Fundraising", notes, description="LP conversations", force=True
    )
    assert out["dropped_claims"] == 1
    assert out["themes"] == 1
    path = cfg.vault.insights_path / "Categories" / "Fundraising.md"
    text = path.read_text()
    assert SYNTH_BEGIN in text
    assert "Angela will review the deck." in text
    assert "Fabricated theme." not in text
    assert "_Scope: LP conversations_" in text
    assert "- [ ] Send deck" in text
    assert "[[2026-07-01 lp-call]]" in text
    assert "Purpose: LP conversations" in llm.users[-1]


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
    write_category_rollup(cfg, llm, "Product", notes, description="new scope", force=False)
    assert llm.calls == 2


def test_categorize_writes_rollups(cfg):
    notes = [
        make_record(
            tid="t1", title="2026-07-01 raise", summary="LP call about pricing deck review."
        ),
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
    categorize(
        cfg,
        [CategoryDef("X", "desc")],
        llm=FakeLLM(classify_map={"a": "X"}),
        verbose=False,
    )
    reset_categories(cfg, verbose=False)
    assert not (cfg.vault.insights_path / "Categories").exists()
    with get_conn(cfg.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM meta WHERE key LIKE 'category_hash:%'"
        ).fetchone()["c"]
        assert get_meta(conn, DEFS_META_KEY) is None
    assert n == 0
