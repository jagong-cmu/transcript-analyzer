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
