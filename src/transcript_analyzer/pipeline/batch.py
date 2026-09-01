"""Bulk structured-output calls through the Message Batches API.

Used by the backfill, which re-summarizes the whole vault in one go: batched
requests are billed at half the standard rates, which is the difference
between a backfill that fits the monthly ceiling and one that does not.

The cost guard still applies. Batched calls do not go through `LLM.create`, so
this module does the same three things by hand and in the same order: it
prechecks (kill switch, per-run call budget, monthly ceiling) BEFORE submitting
anything, refuses a batch larger than the remaining per-run call budget rather
than discovering that halfway through, and records every returned usage into
the ledger at the batch discount.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .llm import BATCH_MULT, LLM, LLMBudgetError, LLMError, LLMResponseError, strict_schema

_log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 30
# The API's own ceiling is 24h; most batches finish inside one. A backfill that
# has waited four hours has something wrong with it worth looking at.
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60


@dataclass
class BatchRequest:
    """One structured-output call, keyed by an id the caller chooses."""

    custom_id: str
    system: str
    user: str
    schema: dict
    max_tokens: int


@dataclass
class BatchOutcome:
    """Results keyed by custom_id, plus the ids that did not succeed.

    Results arrive in ANY order, so nothing here is positional.
    """

    results: dict[str, dict]
    errors: dict[str, str]


def run_batch(
    llm: LLM,
    requests: list[BatchRequest],
    *,
    stage: str = "",
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    on_progress: Optional[Callable[[str], None]] = None,
) -> BatchOutcome:
    """Submit, wait, and parse. Raises LLMError if the batch cannot run at all."""
    if not requests:
        return BatchOutcome(results={}, errors={})

    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    llm._precheck()
    budget = llm.cfg.anthropic.max_calls_per_run
    if llm.calls_this_run + len(requests) > budget:
        raise LLMBudgetError(
            f"this batch is {len(requests)} calls and the per-run budget is "
            f"{budget} ({llm.calls_this_run} already used). Raise "
            f"[anthropic] max_calls_per_run — the whole batch is billed at once, "
            f"so it is refused before submission rather than partway through."
        )

    model = llm.model_for(stage)
    effort = llm.effort_for(stage)
    payload = []
    for req in requests:
        output_config: dict = {
            "format": {"type": "json_schema", "schema": strict_schema(req.schema)}
        }
        if effort:
            output_config["effort"] = effort
        payload.append(
            Request(
                custom_id=req.custom_id,
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=req.max_tokens,
                    system=req.system,
                    messages=[{"role": "user", "content": req.user}],
                    output_config=output_config,
                ),
            )
        )

    batch = _submit(llm, payload)
    _log.info("submitted batch %s (%d requests, model %s)", batch.id, len(payload), model)
    if on_progress:
        on_progress(f"batch {batch.id} submitted: {len(payload)} requests on {model}")

    batch = _await_batch(
        llm, batch.id, poll_seconds, timeout_seconds, on_progress=on_progress
    )
    return _collect(llm, batch.id, model)


def _submit(llm: LLM, payload: list):
    import anthropic

    try:
        return llm.client.messages.batches.create(requests=payload)
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Claude API unreachable: {e}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e


def _await_batch(
    llm: LLM,
    batch_id: str,
    poll_seconds: int,
    timeout_seconds: int,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
):
    import anthropic

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            batch = llm.client.messages.batches.retrieve(batch_id)
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API unreachable: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        if batch.processing_status == "ended":
            return batch
        if time.monotonic() > deadline:
            raise LLMError(
                f"batch {batch_id} still {batch.processing_status} after "
                f"{timeout_seconds}s. It keeps running: re-run with "
                f"--resume-batch {batch_id} once it ends."
            )
        if on_progress:
            counts = getattr(batch, "request_counts", None)
            on_progress(
                f"batch {batch_id}: {batch.processing_status}"
                + (f", {counts.processing} processing" if counts else "")
            )
        time.sleep(poll_seconds)


def collect(llm: LLM, batch_id: str, *, stage: str = "") -> BatchOutcome:
    """Read a finished batch's results — the resume path for an interrupted run."""
    return _collect(llm, batch_id, llm.model_for(stage))


def _collect(llm: LLM, batch_id: str, model: str) -> BatchOutcome:
    import json

    import anthropic

    results: dict[str, dict] = {}
    errors: dict[str, str] = {}
    try:
        stream = llm.client.messages.batches.results(batch_id)
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e

    for entry in stream:
        cid = str(entry.custom_id)
        kind = entry.result.type
        if kind != "succeeded":
            errors[cid] = kind
            continue
        message = entry.result.message
        # Bill it: a batched call is real spend, at half the standard rates.
        llm._record(message.usage, model=model, discount=BATCH_MULT)
        try:
            results[cid] = llm._parse_json_message(message)
        except LLMResponseError as e:
            errors[cid] = str(e)
    return BatchOutcome(results=results, errors=errors)
