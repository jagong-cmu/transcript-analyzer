"""Claude API client with hard cost guards.

Every call funnels through LLM.create() / LLM.stream(), which enforce — in
order — the kill switch (data/llm.kill), the per-run call budget, and the
monthly spend ceiling, then record actual usage into the llm_spend ledger
(SQLite). This code runs unattended in a launchd loop against a paid API;
the guards are the difference between "burns CPU" and "burns money".

Retries are the SDK's own (max_retries=2, billable attempts included in the
recorded usage). Do not stack another retry layer on top.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

from ..config import Config, load_config
from ..db import add_llm_spend, get_conn, get_llm_spend

# USD per million tokens: (input, output). Longest-prefix match on model id;
# unknown models fall back to Opus rates (conservative for the budget check).
PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_RATES = (5.0, 25.0)
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


class LLMError(RuntimeError):
    """Base class for all LLM failures."""


class LLMConfigError(LLMError):
    """No API key configured."""


class LLMKillSwitchError(LLMError):
    """data/llm.kill exists — all API calls are stopped."""


class LLMBudgetError(LLMError):
    """Monthly spend ceiling or per-run call budget reached."""


class LLMResponseError(LLMError):
    """The API answered but the response was unusable (e.g. bad JSON)."""


def _month() -> str:
    return datetime.now().strftime("%Y-%m")


def strict_schema(schema: dict) -> dict:
    """Prepare a JSON Schema for structured outputs: every object gets
    additionalProperties: false and a full `required` list."""
    out: dict = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            out[k] = strict_schema(v)
        elif isinstance(v, list):
            out[k] = [strict_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


class LLM:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or load_config()
        self.model = self.cfg.anthropic.model
        self.calls_this_run = 0
        self._client = None

    # ---------- client ----------

    @property
    def client(self):
        if self._client is None:
            import anthropic

            key = self.cfg.anthropic.api_key.strip() or None
            try:
                self._client = anthropic.Anthropic(
                    api_key=key, timeout=float(self.cfg.anthropic.timeout)
                )
            except anthropic.AnthropicError as e:
                raise LLMConfigError(
                    "No Anthropic API key found. Set [anthropic] api_key in "
                    "config.toml or the ANTHROPIC_API_KEY environment variable."
                ) from e
        return self._client

    # ---------- cost guard ----------

    def _rates(self) -> tuple[float, float]:
        best = ""
        for prefix in PRICING:
            if self.model.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        return PRICING[best] if best else _DEFAULT_RATES

    def month_spend_usd(self) -> float:
        with get_conn(self.cfg.db_path) as conn:
            row = get_llm_spend(conn, _month())
        return float(row["usd"]) if row else 0.0

    def _precheck(self) -> None:
        if self.cfg.kill_switch_path.exists():
            raise LLMKillSwitchError(
                f"Kill switch is on ({self.cfg.kill_switch_path}). "
                "Delete the file to re-enable Claude API calls."
            )
        if self.calls_this_run >= self.cfg.anthropic.max_calls_per_run:
            raise LLMBudgetError(
                f"Per-run call budget reached ({self.cfg.anthropic.max_calls_per_run} calls). "
                "Raise [anthropic] max_calls_per_run if this run legitimately needs more."
            )
        ceiling = self.cfg.anthropic.monthly_budget_usd
        spent = self.month_spend_usd()
        if spent >= ceiling:
            raise LLMBudgetError(
                f"Monthly spend ceiling reached (${spent:.2f} of ${ceiling:.2f} for {_month()}). "
                "Raise [anthropic] monthly_budget_usd to continue."
            )

    def _record(self, usage: Any) -> None:
        self.calls_this_run += 1
        if usage is None:
            return
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        in_rate, out_rate = self._rates()
        usd = (
            in_tok * in_rate
            + cache_write * in_rate * CACHE_WRITE_MULT
            + cache_read * in_rate * CACHE_READ_MULT
            + out_tok * out_rate
        ) / 1_000_000
        with get_conn(self.cfg.db_path) as conn:
            add_llm_spend(conn, _month(), 1, in_tok, out_tok, cache_read, cache_write, usd)

    # ---------- health ----------

    def health(self) -> dict:
        import os

        key_configured = bool(
            self.cfg.anthropic.api_key.strip() or os.environ.get("ANTHROPIC_API_KEY")
        )
        kill = self.cfg.kill_switch_path.exists()
        spent = self.month_spend_usd()
        ceiling = self.cfg.anthropic.monthly_budget_usd
        return {
            "ok": key_configured and not kill and spent < ceiling,
            "model": self.model,
            "key_configured": key_configured,
            "kill_switch": kill,
            "month_spend_usd": round(spent, 4),
            "monthly_budget_usd": ceiling,
            "calls_this_run": self.calls_this_run,
        }

    # ---------- core calls ----------

    def create(self, **kwargs) -> Any:
        """Guarded, usage-recorded messages.create(). Accepts the full
        Messages API surface (system, messages, tools, output_config, ...)."""
        import anthropic

        self._precheck()
        kwargs.setdefault("model", self.model)
        kwargs.setdefault("max_tokens", self.cfg.anthropic.max_tokens)
        try:
            msg = self.client.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API unreachable: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        self._record(msg.usage)
        return msg

    @contextmanager
    def stream(self, **kwargs):
        """Guarded, usage-recorded messages.stream() context manager."""
        import anthropic

        self._precheck()
        kwargs.setdefault("model", self.model)
        kwargs.setdefault("max_tokens", self.cfg.anthropic.max_tokens)
        try:
            with self.client.messages.stream(**kwargs) as s:
                try:
                    yield s
                finally:
                    # get_final_message() drains any unread remainder, so
                    # usage is complete even if the consumer stopped early.
                    try:
                        self._record(s.get_final_message().usage)
                    except Exception:  # noqa: BLE001 - never mask the caller's error
                        self.calls_this_run += 1
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API unreachable: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e

    # ---------- convenience ----------

    def chat(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> str:
        kwargs: dict = {
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        msg = self.create(**kwargs)
        return "".join(b.text for b in msg.content if b.type == "text")

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """Chat constrained to a JSON object matching `schema` (structured
        outputs via output_config.format)."""
        kwargs: dict = {
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "format": {"type": "json_schema", "schema": strict_schema(schema)}
            },
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        msg = self.create(**kwargs)
        if msg.stop_reason == "max_tokens":
            raise LLMResponseError(
                "Structured output truncated at max_tokens; raise the limit."
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMResponseError(f"Claude returned invalid JSON: {e}") from e

    def chat_stream(self, system: str, user: str) -> Iterator[str]:
        with self.stream(
            system=system, messages=[{"role": "user", "content": user}]
        ) as s:
            yield from s.text_stream
