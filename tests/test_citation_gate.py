"""R3: the gate is semantic-ish, not syntactic — a claim must carry a span
that literally appears in the cited conversation, or it is dropped."""
from conftest import make_record

from transcript_analyzer.pipeline.synthesize import verify_claims


def claims_env():
    rec = make_record(
        tid="abc123",
        summary="Angela agreed to review the pricing deck by Friday.",
    )
    return {"abc123": rec}


def test_verbatim_quote_passes():
    kept, dropped = verify_claims(
        [{"text": "Angela owes a pricing review.", "source_id": "abc123",
          "quote": "review the pricing deck by Friday"}],
        claims_env(),
    )
    assert len(kept) == 1 and dropped == 0


def test_case_and_whitespace_normalized():
    kept, dropped = verify_claims(
        [{"text": "x", "source_id": "abc123",
          "quote": "Review   THE pricing\ndeck"}],
        claims_env(),
    )
    assert len(kept) == 1 and dropped == 0


def test_paraphrased_quote_dropped():
    kept, dropped = verify_claims(
        [{"text": "x", "source_id": "abc123",
          "quote": "Angela promised a deck review by end of week"}],
        claims_env(),
    )
    assert kept == [] and dropped == 1


def test_valid_link_wrong_source_dropped():
    # The exact failure R3 described: a fabricated claim with a valid-looking
    # citation must not pass just because the target exists.
    kept, dropped = verify_claims(
        [{"text": "x", "source_id": "nope999", "quote": "pricing deck"}],
        claims_env(),
    )
    assert kept == [] and dropped == 1


def test_empty_quote_dropped():
    kept, dropped = verify_claims(
        [{"text": "x", "source_id": "abc123", "quote": "  "}],
        claims_env(),
    )
    assert kept == [] and dropped == 1


def test_quote_from_transcript_body_passes():
    kept, dropped = verify_claims(
        [{"text": "x", "source_id": "abc123",
          "quote": "I will review the pricing deck"}],
        claims_env(),
    )
    assert len(kept) == 1 and dropped == 0
