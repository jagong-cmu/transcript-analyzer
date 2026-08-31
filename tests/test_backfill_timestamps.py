"""The timestamp backfill rewrites the ## Transcript section in place."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backfill_timestamps",
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_timestamps.py",
)
backfill_timestamps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill_timestamps)

NOTE = """# A note

## Summary
Something happened.

## Transcript
> [!note]- Full transcript
> old untimed text
"""


def test_replaces_the_transcript_section():
    out = backfill_timestamps._replace_transcript_section(NOTE, "[0:00] Hi")
    assert "old untimed text" not in out
    assert "> [0:00] Hi" in out
    assert "Something happened." in out


def test_transcript_text_is_never_a_regex_template():
    """Speech-to-text can emit backslashes; as a re.sub template they raised
    'bad escape' (and \\1 would have expanded a capture group)."""
    timed = "[0:00] Path is C:\\Users\\bob and group \\1"
    out = backfill_timestamps._replace_transcript_section(NOTE, timed)
    assert "C:\\Users\\bob" in out
    assert "\\1" in out


def test_appends_when_there_is_no_transcript_section():
    out = backfill_timestamps._replace_transcript_section("# A note\n", "[0:00] Hi")
    assert out.startswith("# A note")
    assert "## Transcript" in out
    assert "> [0:00] Hi" in out


def test_already_timed_detection():
    assert backfill_timestamps._already_timed("[0:00] Hi\n[1:23] There")
    assert backfill_timestamps._already_timed("plain\n[12:05] later")
    assert not backfill_timestamps._already_timed("plain text only")
    assert not backfill_timestamps._already_timed("")
