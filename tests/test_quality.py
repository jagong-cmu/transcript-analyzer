"""R5: junk must be filtered at ingest — it is billable under the API and
would otherwise be faithfully synthesized into the digest."""
from conftest import make_transcript

from transcript_analyzer.pipeline.quality import junk_reason


def test_real_transcript_passes(cfg):
    assert junk_reason(make_transcript("User interview at SFO"), cfg) is None


def test_short_transcript_is_junk(cfg):
    t = make_transcript("Quick note", text="uh hello")
    assert "too short" in junk_reason(t, cfg)


def test_junk_titles(cfg):
    for title in (
        "background-noise-recording",
        "Hello Testing",
        "Getting started with Pocket",
        "Your call has been forward",
    ):
        t = make_transcript(title)
        assert junk_reason(t, cfg) is not None, title
