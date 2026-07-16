"""Controlled taxonomy that grows over time.

The LLM proposes a category per transcript. We either match it to an existing
category (exact or by embedding similarity) or add it as a new one. This keeps
the category set small and consistent instead of exploding into near-duplicates.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

from ..config import Config
from .llm import LLM


class Taxonomy:
    def __init__(self, cfg: Config, llm: Optional[LLM] = None) -> None:
        self.cfg = cfg
        self.llm = llm or LLM(cfg)
        self.path: Path = cfg.taxonomy_path
        self.categories: list[str] = []
        self._embeddings: dict[str, np.ndarray] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.categories = list(data.get("categories", []))
        if not self.categories:
            self.categories = list(self.cfg.taxonomy.seed)
            self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps({"categories": self.categories}, indent=2))

    def _emb(self, name: str) -> np.ndarray:
        if name not in self._embeddings:
            self._embeddings[name] = self.llm.embed_one(name)
        return self._embeddings[name]

    def resolve(self, proposed: str) -> str:
        """Map a proposed category to an existing one, or add it as new."""
        proposed = (proposed or "").strip() or "Uncategorized"

        # Exact / case-insensitive match first.
        for c in self.categories:
            if c.lower() == proposed.lower():
                return c

        # Embedding similarity against existing categories.
        try:
            pv = self._emb(proposed)
            best, best_sim = None, -1.0
            for c in self.categories:
                sim = float(np.dot(pv, self._emb(c)))
                if sim > best_sim:
                    best, best_sim = c, sim
            if best is not None and best_sim >= self.cfg.taxonomy.merge_threshold:
                return best
        except Exception:  # noqa: BLE001 - if embeddings fail, just add the category
            pass

        # New category.
        self.categories.append(proposed)
        self._save()
        return proposed
