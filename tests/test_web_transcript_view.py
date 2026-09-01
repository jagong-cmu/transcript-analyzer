"""How the dashboard lays a transcript out, per the shapes a vault holds.

The served page is parsed into rows rather than searched as a string: the
question is what each transcript line is rendered AS, which is the thing that
broke once already — an un-migrated note (no timestamps anywhere, the default
state of every vault written before this change) had its lines rendered as
timed rows, so every line sat in the 64px timestamp column with an empty cell
beside it. A note that HAS timestamps looked fine, which is why a smoke test
of one timed note missed it.

Only the server's half is covered: which rows and controls are emitted. The
seek itself is the click handler in transcript.html, which needs a DOM this
suite does not have.
"""
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from transcript_analyzer.db import get_conn, upsert_transcript
from transcript_analyzer.models import NoteRecord
from transcript_analyzer.obsidian import writer

TIMED = (
    "[0:00] Speaker 1: Okay, we are recording.\n"
    "[1:36] Speaker 2: I will ask Angela to review the deck by Friday.\n"
    "[1:07:20] Speaker 1: Last item, and this one runs long."
)
UNTIMED = (
    "Dev: Did anyone update the status page during the outage?\n"
    "Me: No. It is not in the incident template, so nobody thinks to."
)


class _Transcript(HTMLParser):
    """The rendered transcript block as rows of (classes, cells)."""

    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.audio_src = None
        self._row = None
        self._cell = None  # "stamp" | "text" | None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "audio":
            self.audio_src = a.get("src")
            return
        if "t-line" in classes:
            self._row = {"classes": classes, "seek": None, "stamp": None, "text": ""}
            self.rows.append(self._row)
            self._cell = None
            return
        if self._row is None:
            return
        if "t-ts" in classes:
            self._cell = "stamp"
            self._row["stamp"] = ""
            # A button carries the seek offset; a span is a label only.
            self._row["seek"] = float(a["data-t"]) if tag == "button" else None
        elif "t-text" in classes:
            self._cell = "text"

    def handle_endtag(self, tag):
        if tag in ("button", "span"):
            self._cell = None

    def handle_data(self, data):
        if self._row is None or self._cell is None:
            return
        if self._cell == "stamp":
            self._row["stamp"] += data.strip()
        else:
            self._row["text"] += data


def _serve(cfg, app_mod, *, tid: str, text: str, with_audio: bool) -> _Transcript:
    """Index one note (and optionally its recording) and fetch its page."""
    note = cfg.vault.insights_path / f"2026-08-24 {tid}.md"
    note.write_text(f"# {tid}\n", encoding="utf-8")
    if with_audio:
        audio = writer.audio_path_for(cfg, note)
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 200)
    with get_conn(cfg.db_path) as conn:
        upsert_transcript(conn, NoteRecord(
            transcript_id=tid,
            source="pocket",
            title="Pricing deck rework with Angela, August 24th, 2026",
            date="2026-08-24",
            category="",
            summary="A summary.",
            note_path=str(note),
            transcript_text=text,
        ))
    with TestClient(app_mod.app) as client:
        r = client.get(f"/transcript/{tid}")
    assert r.status_code == 200
    return _Transcript(r.text)


def test_a_timed_transcript_seeks_the_recording_at_each_line(app_mod, cfg):
    doc = _serve(cfg, app_mod, tid="timed1", text=TIMED, with_audio=True)

    assert doc.audio_src == "/audio/timed1"
    # Every line offers a seek, and it lands on the second the line was spoken.
    assert [(r["stamp"], r["seek"]) for r in doc.rows] == [
        ("0:00", 0.0),
        ("1:36", 96.0),
        ("1:07:20", 4040.0),
    ]
    assert "Angela" in doc.rows[1]["text"]


def test_an_un_migrated_note_renders_as_plain_lines(app_mod, cfg):
    """The default state of a vault written before timestamps: no stamps, no
    recording. Nothing may be laid out in the timestamp column."""
    doc = _serve(cfg, app_mod, tid="untimed1", text=UNTIMED, with_audio=False)

    assert doc.audio_src is None
    assert len(doc.rows) == 2
    for row in doc.rows:
        assert "t-untimed" in row["classes"], "an untimed line kept the timestamp column"
        assert row["stamp"] is None and row["seek"] is None
    assert "status page" in doc.rows[0]["text"]


def test_a_line_the_owner_appended_below_a_timed_transcript_is_not_timed(app_mod, cfg):
    doc = _serve(
        cfg, app_mod, tid="mixed1",
        text=TIMED + "\nMe: (added this line myself in Obsidian)",
        with_audio=True,
    )

    timed, appended = doc.rows[:-1], doc.rows[-1]
    assert all(r["seek"] is not None for r in timed)
    assert "t-untimed" in appended["classes"]
    assert appended["seek"] is None and appended["stamp"] is None
    assert "added this line myself" in appended["text"]


def test_timestamps_without_a_recording_are_shown_but_not_clickable(app_mod, cfg):
    """Granola notes have timings and no audio API, so the stamps are labels."""
    doc = _serve(cfg, app_mod, tid="noaudio1", text=TIMED, with_audio=False)

    assert doc.audio_src is None
    assert [r["stamp"] for r in doc.rows] == ["0:00", "1:36", "1:07:20"]
    assert all(r["seek"] is None for r in doc.rows), "seek offered with no recording"
