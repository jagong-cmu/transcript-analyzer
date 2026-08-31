"""Timestamp formatting and transcript line parsing."""
from transcript_analyzer.models import TranscriptSegment
from transcript_analyzer.transcript_fmt import (
    coerce_seconds,
    format_segments,
    format_timestamp,
    parse_transcript_lines,
    relative_seconds_from_iso,
)
from transcript_analyzer.connectors.pocket_api import PocketClient
from transcript_analyzer.connectors.granola import GranolaClient


def test_format_timestamp():
    assert format_timestamp(0) == "[0:00]"
    assert format_timestamp(83) == "[1:23]"
    assert format_timestamp(3723) == "[1:02:03]"


def test_coerce_seconds_ms():
    assert coerce_seconds(1.5) == 1.5
    assert coerce_seconds(15000) == 15.0


def test_format_and_parse_roundtrip():
    segs = [
        TranscriptSegment(text="Hello", speaker="Alice", start_sec=1.2),
        TranscriptSegment(text="there", speaker="Alice", start_sec=3.0),
        TranscriptSegment(text="Hi", speaker="Bob", start_sec=5.5),
    ]
    text = format_segments(segs)
    assert "[0:01] Alice: Hello" in text
    assert "[0:03] there" in text
    assert "[0:06] Bob: Hi" in text
    rows = parse_transcript_lines(text)
    timed = [r for r in rows if r["start_sec"] is not None]
    assert timed[0]["start_sec"] == 1.0
    assert timed[0]["ts_label"] == "0:01"
    assert "Alice: Hello" in timed[0]["text"]


def test_pocket_segments_include_start():
    detail = {
        "id": "rec1",
        "title": "Test",
        "recording_at": "2026-07-01T12:00:00Z",
        "transcript": {
            "segments": [
                {"text": "Hello", "speaker": "SPEAKER_00", "start": 0.5, "end": 2.0},
                {"text": "World", "speaker": "SPEAKER_01", "start": 2.5, "end": 4.0},
            ]
        },
    }
    text, segs = PocketClient._transcript_text(detail)
    assert segs[0].start_sec == 0.5
    assert "[0:00] Speaker 1: Hello" in text
    assert "Speaker 2: World" in text


def test_granola_relative_iso_times():
    detail = {
        "id": "n1",
        "title": "Call",
        "created_at": "2026-07-01T12:00:00Z",
        "owner": {"name": "Jonathan"},
        "attendees": [{"name": "Angela", "email": "a@x.com"}],
        "transcript": [
            {
                "text": "Hi",
                "speaker": {"name": "Jonathan", "source": "microphone"},
                "start_time": "2026-07-01T12:00:00Z",
                "end_time": "2026-07-01T12:00:02Z",
            },
            {
                "text": "Hello",
                "speaker": {"name": "Angela", "source": "speaker"},
                "start_time": "2026-07-01T12:00:05Z",
                "end_time": "2026-07-01T12:00:07Z",
            },
        ],
    }
    text, segs = GranolaClient._transcript_text(detail)
    assert segs[0].start_sec == 0.0
    assert segs[1].start_sec == 5.0
    assert "[0:00] Jonathan: Hi" in text
    assert "[0:05] Angela: Hello" in text


def test_relative_seconds_helper():
    rel = relative_seconds_from_iso(
        ["2026-07-01T12:00:00Z", "2026-07-01T12:01:00Z", None]
    )
    assert rel[0] == 0.0
    assert rel[1] == 60.0
    assert rel[2] is None


def test_unit_inference_is_per_transcript_not_per_value():
    """One unit for the whole recording, even across the ambiguity threshold.

    Per-value inference read everything under 10_000 as seconds and everything
    over it as milliseconds, so a recording that ran past ~2h46m jumped
    backwards mid-transcript. Absolute scale stays ambiguous for a long
    seconds-valued recording (nothing in the API distinguishes it from a short
    ms-valued one), but the ordering must survive.
    """
    detail = {
        "transcript": {
            "segments": [
                {"text": "start", "start": 0, "end": 5},
                {"text": "middle", "start": 9990, "end": 9995},
                {"text": "late", "start": 10800, "end": 10805},
            ]
        }
    }
    starts = [s.start_sec for s in PocketClient._transcript_segments(detail)]
    assert starts == sorted(starts), starts
    # A single divisor for all three, not one boundary inside the transcript.
    assert starts == [0.0, 9.99, 10.8]


def test_millisecond_transcript_scales_every_segment():
    detail = {
        "transcript": {
            "segments": [
                {"text": "a", "start": 500, "end": 1500},
                {"text": "b", "start": 15000, "end": 16000},
            ]
        }
    }
    segs = PocketClient._transcript_segments(detail)
    assert [s.start_sec for s in segs] == [0.5, 15.0]


def test_granola_leading_silent_segment_still_anchors_zero():
    detail = {
        "transcript": [
            {"text": "  ", "start_time": "2026-07-01T12:00:00Z"},
            {"text": "Hi", "start_time": "2026-07-01T12:00:10Z"},
        ]
    }
    segs = GranolaClient._transcript_segments(detail)
    assert [s.text for s in segs] == ["Hi"]
    assert segs[0].start_sec == 10.0
