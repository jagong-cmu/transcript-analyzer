"""Thin wrappers around a local Ollama server (chat + embeddings)."""
from __future__ import annotations

import json
from typing import Iterator, Optional

import httpx
import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import Config, load_config


class OllamaError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or load_config()
        self.host = self.cfg.ollama.host.rstrip("/")
        self.timeout = self.cfg.ollama.timeout

    # ---------- health ----------

    def health(self) -> dict:
        """Return {'ok': bool, 'models': [...], 'missing': [...], 'error': str|None}."""
        result = {"ok": False, "models": [], "missing": [], "error": None}
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=10)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            result["models"] = models
            needed = {self.cfg.ollama.chat_model, self.cfg.ollama.embed_model}
            # Ollama lists models with a tag (e.g. "qwen2.5:3b"); accept a bare-name match too.
            base = {m.split(":")[0] for m in models}
            missing = [
                n for n in needed
                if n not in models and n.split(":")[0] not in base
            ]
            result["missing"] = missing
            result["ok"] = not missing
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"
        return result

    # ---------- chat ----------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def chat(
        self,
        system: str,
        user: str,
        *,
        as_json: bool = False,
        options: Optional[dict] = None,
    ) -> str:
        payload = {
            "model": self.cfg.ollama.chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": options or {"temperature": 0.2},
        }
        if as_json:
            payload["format"] = "json"
        try:
            r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise OllamaError(f"chat request failed: {e}") from e
        return r.json()["message"]["content"]

    def chat_json(self, system: str, user: str, options: Optional[dict] = None) -> dict:
        """Chat that must return a JSON object. Tolerates code fences / stray text."""
        raw = self.chat(system, user, as_json=True, options=options)
        return _parse_json_object(raw)

    def chat_stream(self, system: str, user: str) -> Iterator[str]:
        payload = {
            "model": self.cfg.ollama.chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "options": {"temperature": 0.3},
        }
        with httpx.stream(
            "POST", f"{self.host}/api/chat", json=payload, timeout=self.timeout
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = obj.get("message", {}).get("content", "")
                if token:
                    yield token
                if obj.get("done"):
                    break

    # ---------- embeddings ----------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def embed_one(self, text: str) -> np.ndarray:
        payload = {"model": self.cfg.ollama.embed_model, "prompt": text}
        try:
            r = httpx.post(f"{self.host}/api/embeddings", json=payload, timeout=self.timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise OllamaError(f"embeddings request failed: {e}") from e
        vec = np.array(r.json()["embedding"], dtype=np.float32)
        return _normalize(vec)

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed_one(t) for t in texts]


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        # strip ```json ... ``` fences
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Best effort: grab the outermost {...}
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise
