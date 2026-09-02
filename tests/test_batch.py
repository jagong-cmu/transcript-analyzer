"""Batched calls bypass LLM.create, so the cost guard has to be re-applied here.

A batch is submitted all at once and billed all at once: discovering halfway
through that it exceeds the per-run budget is not a recoverable state.
"""
from types import SimpleNamespace

import pytest

from transcript_analyzer.pipeline import batch as batch_api
from transcript_analyzer.pipeline.llm import (
    LLM,
    LLMBudgetError,
    LLMKillSwitchError,
)


def requests(n):
    return [
        batch_api.BatchRequest(
            custom_id=f"t{i}", system="s", user="u",
            schema={"type": "object", "properties": {"x": {"type": "string"}}},
            max_tokens=100,
        )
        for i in range(n)
    ]


def message(text='{"x": 1}', in_tok=1_000_000, out_tok=0):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=in_tok, output_tokens=out_tok,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


class FakeBatches:
    def __init__(self, entries):
        self.entries = entries
        self.submitted = None

    def create(self, requests):
        self.submitted = requests
        return SimpleNamespace(id="batch_1", processing_status="in_progress")

    def retrieve(self, _id):
        return SimpleNamespace(
            id="batch_1", processing_status="ended",
            request_counts=SimpleNamespace(processing=0),
        )

    def results(self, _id):
        return iter(self.entries)


def entry(cid, result_type="succeeded", msg=None):
    return SimpleNamespace(
        custom_id=cid,
        result=SimpleNamespace(type=result_type, message=msg or message()),
    )


def fake_client(entries):
    batches = FakeBatches(entries)
    return SimpleNamespace(messages=SimpleNamespace(batches=batches)), batches


def test_a_batch_over_the_per_run_budget_is_refused_before_submission(cfg):
    llm = LLM(cfg)  # the fixture's budget is 3 calls
    client, batches = fake_client([])
    llm._client = client
    with pytest.raises(LLMBudgetError, match="refused before submission"):
        batch_api.run_batch(llm, requests(4))
    assert batches.submitted is None


def test_the_kill_switch_stops_a_batch_too(cfg):
    llm = LLM(cfg)
    client, batches = fake_client([])
    llm._client = client
    cfg.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.kill_switch_path.touch()
    try:
        with pytest.raises(LLMKillSwitchError):
            batch_api.run_batch(llm, requests(1))
        assert batches.submitted is None
    finally:
        cfg.kill_switch_path.unlink()


def test_results_are_keyed_by_custom_id_never_by_position(cfg):
    """The API returns results in any order."""
    llm = LLM(cfg)
    llm._client, _ = fake_client([
        entry("t2", msg=message('{"x": "second"}')),
        entry("t0", msg=message('{"x": "first"}')),
    ])
    out = batch_api.run_batch(llm, requests(3), stage="backfill")
    assert out.results["t0"] == {"x": "first"}
    assert out.results["t2"] == {"x": "second"}
    assert "t1" not in out.results


def test_a_failed_request_is_reported_not_silently_missing(cfg):
    llm = LLM(cfg)
    llm._client, _ = fake_client([entry("t0", result_type="errored"),
                                  entry("t1", msg=message('not json'))])
    out = batch_api.run_batch(llm, requests(2), stage="backfill")
    assert out.errors["t0"] == "errored"
    assert "invalid JSON" in out.errors["t1"]
    assert out.results == {}


def test_every_returned_call_is_billed_at_the_batch_discount(cfg):
    llm = LLM(cfg)
    llm._client, _ = fake_client([entry("t0"), entry("t1")])
    batch_api.run_batch(llm, requests(2), stage="backfill")
    # Sonnet 5 input is $2/M; two million-token calls at half price = $2.
    assert llm.month_spend_usd() == pytest.approx(2.0)
    assert llm.calls_this_run == 2


def test_the_stage_decides_the_model_and_effort_of_every_request(cfg):
    llm = LLM(cfg)
    client, batches = fake_client([entry("t0")])
    llm._client = client
    batch_api.run_batch(llm, requests(1), stage="backfill")
    params = batches.submitted[0]["params"]
    assert params["model"] == "claude-sonnet-5"
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"


def test_an_empty_batch_costs_nothing(cfg):
    llm = LLM(cfg)
    out = batch_api.run_batch(llm, [])
    assert out.results == {} and llm.calls_this_run == 0
