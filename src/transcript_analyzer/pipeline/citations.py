"""The one definition of "does this quote actually appear in the source".

Two callers need the same answer and must not each own a copy of the rule:
`synthesize.verify_claims` gates digest/dossier/study/prep claims against a
whole NoteRecord, and `lecture` gates study-note sections, assessment claims
and ASR repairs against the transcript text alone. A second normalizer that
folded whitespace slightly differently would make a span that passes one gate
fail the other — and the gates exist precisely so that "cited" means the same
thing everywhere. Add call sites here, not another rule (see AGENTS.md).

Normalization is deliberately shallow: case and whitespace only. Stripping
punctuation would let a paraphrase through, which is the failure mode the gate
was built for.
"""
from __future__ import annotations


def normalize(s: str) -> str:
    """Casefolded, whitespace-collapsed text — the form both sides compare in."""
    return " ".join(str(s or "").casefold().split())


def quote_matches(quote: str, haystack: str, *, min_chars: int = 1) -> bool:
    """Whether `quote` appears verbatim (case/whitespace-normalized) in `haystack`.

    An empty quote never matches: a claim with no span is exactly the
    uncited claim the gate is here to drop. `min_chars` lets a caller demand
    a span long enough to mean something — a two-character "quote" would
    match almost any transcript and prove nothing.
    """
    q = normalize(quote)
    if len(q) < max(1, min_chars):
        return False
    return q in normalize(haystack)
