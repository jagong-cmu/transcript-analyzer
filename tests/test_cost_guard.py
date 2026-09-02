"""The cost guard is the single most important new code under Option C:
unattended paid API in a 20-minute launchd loop."""
from types import SimpleNamespace

import pytest

from transcript_analyzer.db import get_conn, get_llm_spend
from transcript_analyzer.pipeline.llm import (
    LLM,
    LLMBudgetError,
    LLMKillSwitchError,
    _month,
)


def usage(inp=0, out=0, cread=0, cwrite=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cread,
        cache_creation_input_tokens=cwrite,
    )


def test_ledger_accumulates(cfg):
    llm = LLM(cfg)
    llm._record(usage(inp=100_000, out=10_000))
    llm._record(usage(inp=100_000, out=10_000))
    with get_conn(cfg.db_path) as conn:
        row = get_llm_spend(conn, _month())
    assert row["calls"] == 2
    assert row["input_tokens"] == 200_000
    assert row["output_tokens"] == 20_000
    # Opus rates: 2 * (0.1M * $5 + 0.01M * $25) / 1M = 2 * $0.75
    assert row["usd"] == pytest.approx(1.5)


def test_cache_tokens_priced_at_multipliers(cfg):
    llm = LLM(cfg)
    llm._record(usage(cread=1_000_000, cwrite=1_000_000))
    assert llm.month_spend_usd() == pytest.approx(5.0 * 0.1 + 5.0 * 1.25)


def test_monthly_ceiling_blocks_calls(cfg):
    llm = LLM(cfg)
    llm._record(usage(inp=1_000_000))  # $5 = the fixture's ceiling
    with pytest.raises(LLMBudgetError, match="Monthly spend ceiling"):
        llm._precheck()


def test_per_run_call_budget(cfg):
    llm = LLM(cfg)
    llm.calls_this_run = cfg.anthropic.max_calls_per_run
    with pytest.raises(LLMBudgetError, match="Per-run call budget"):
        llm._precheck()


def test_kill_switch(cfg):
    llm = LLM(cfg)
    cfg.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.kill_switch_path.touch()
    with pytest.raises(LLMKillSwitchError):
        llm._precheck()
    assert llm.health()["ok"] is False
    cfg.kill_switch_path.unlink()
    llm._precheck()  # under budget, no kill switch -> allowed


def test_health_reports_spend(cfg):
    llm = LLM(cfg)
    llm._record(usage(inp=100_000))
    h = llm.health()
    assert h["ok"] is True
    assert h["month_spend_usd"] == pytest.approx(0.5)
    assert h["monthly_budget_usd"] == 5.0


# ---------- per-stage models: the ledger must charge what actually ran ----------


def test_a_stage_is_billed_at_its_own_models_rates(cfg):
    """Sonnet work billed at Opus rates would move the ceiling that stops the
    unattended loop — in the wrong direction, and invisibly."""
    llm = LLM(cfg)  # default model is Opus-tier
    llm._record(usage(inp=1_000_000, out=1_000_000), model="claude-sonnet-5")
    # Sonnet 5 is $2/$10 per million, not Sonnet 4.6's $3/$15.
    assert llm.month_spend_usd() == pytest.approx(12.0)


def test_sonnet_5_pricing_is_the_published_rate(cfg):
    from transcript_analyzer.pipeline.llm import PRICING

    assert PRICING["claude-sonnet-5"] == (2.0, 10.0)
    assert PRICING["claude-opus-5"] == (5.0, 25.0)


def test_batched_calls_are_billed_at_half(cfg):
    from transcript_analyzer.pipeline.llm import BATCH_MULT

    llm = LLM(cfg)
    llm._record(usage(inp=1_000_000), model="claude-sonnet-5", discount=BATCH_MULT)
    assert llm.month_spend_usd() == pytest.approx(1.0)


def test_a_stage_picks_its_model_and_effort_from_config(cfg):
    llm = LLM(cfg)
    assert llm.model_for("lecture") == "claude-opus-5"
    assert llm.effort_for("lecture") == "medium"
    # An unnamed stage falls back to the configured default model.
    assert llm.model_for("synthesis") == cfg.anthropic.model
    assert llm.effort_for("synthesis") == ""


def test_effort_is_merged_into_output_config_not_over_it(cfg):
    """Structured output and effort share one output_config; setting effort
    must not drop the schema that makes the response parseable."""
    llm = LLM(cfg)
    kwargs = llm._with_stage(
        "extract", {"output_config": {"format": {"type": "json_schema"}}}
    )
    assert kwargs["output_config"]["effort"] == "low"
    assert kwargs["output_config"]["format"] == {"type": "json_schema"}


def test_a_stage_with_no_effort_sends_none(cfg):
    """Models older than 4.6 reject the parameter outright."""
    from dataclasses import replace

    cfg = replace(cfg, anthropic=replace(cfg.anthropic, stage_effort={"extract": ""}))
    kwargs = LLM(cfg)._with_stage("extract", {})
    assert "output_config" not in kwargs


def test_every_stage_a_call_site_names_is_priced(cfg):
    """A model with no PRICING row bills at the Opus fallback — fine for the
    guard, wrong for the ledger. Every stage we actually ship must be known."""
    from transcript_analyzer.pipeline.llm import PRICING

    llm = LLM(cfg)
    for stage in ("extract", "lecture", "backfill", "synthesis", ""):
        model = llm.model_for(stage)
        assert any(model.startswith(p) for p in PRICING), f"{stage} -> {model}"
