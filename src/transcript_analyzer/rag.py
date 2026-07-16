"""Retrieval-augmented Q&A over the indexed transcripts (fully local)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np

from .config import Config, load_config
from .db import get_conn, get_transcript, load_all_chunk_embeddings
from .pipeline.llm import LLM


@dataclass
class Retrieved:
    transcript_id: str
    title: str
    note_path: str
    text: str
    score: float


SYSTEM = """You answer questions using ONLY the provided excerpts from the user's
meeting and conversation transcripts. Cite sources inline like [1], [2] that map to
the numbered excerpts. If the excerpts don't contain the answer, say so plainly.
Be concise and specific."""


def retrieve(cfg: Config, question: str, llm: LLM, k: int = 6) -> list[Retrieved]:
    with get_conn(cfg.db_path) as conn:
        rows = load_all_chunk_embeddings(conn)
        if not rows:
            return []
        qv = llm.embed_one(question)
        mat = np.vstack([r[3] for r in rows])
        sims = mat @ qv  # embeddings are normalized -> cosine similarity
        top_idx = np.argsort(-sims)[: max(k * 3, k)]

        # Keep the best chunk per transcript, then take top-k transcripts.
        best: dict[str, tuple[float, str]] = {}
        for i in top_idx:
            _cid, tid, text, _emb = rows[i]
            score = float(sims[i])
            if tid not in best or score > best[tid][0]:
                best[tid] = (score, text)

        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:k]
        out: list[Retrieved] = []
        for tid, (score, text) in ranked:
            rec = get_transcript(conn, tid)
            out.append(
                Retrieved(
                    transcript_id=tid,
                    title=rec.title if rec else tid,
                    note_path=rec.note_path if rec else "",
                    text=text,
                    score=score,
                )
            )
        return out


def _build_prompt(question: str, hits: list[Retrieved]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(f"[{i}] {h.title}\n{h.text.strip()}")
    context = "\n\n".join(blocks) if blocks else "(no excerpts found)"
    return f"Excerpts:\n\n{context}\n\nQuestion: {question}\n\nAnswer (cite [n]):"


def answer(
    question: str,
    cfg: Optional[Config] = None,
    llm: Optional[LLM] = None,
    k: int = 6,
) -> dict:
    cfg = cfg or load_config()
    llm = llm or LLM(cfg)
    hits = retrieve(cfg, question, llm, k=k)
    prompt = _build_prompt(question, hits)
    text = llm.chat(SYSTEM, prompt)
    return {"answer": text, "sources": hits}


def answer_stream(
    question: str,
    cfg: Optional[Config] = None,
    llm: Optional[LLM] = None,
    k: int = 6,
) -> tuple[list[Retrieved], Iterator[str]]:
    """Return (sources, token_iterator) so the caller can show citations + stream."""
    cfg = cfg or load_config()
    llm = llm or LLM(cfg)
    hits = retrieve(cfg, question, llm, k=k)
    prompt = _build_prompt(question, hits)
    return hits, llm.chat_stream(SYSTEM, prompt)
