"""Server-side handling of a note title, which is LLM/vault free text now.

SCOPE, precisely: these cover the two things the SERVER controls — that a
Jinja-rendered page never emits the title as live markup, and that the /ask
`sources` event hands the title to the client as a JSON data value rather than
as a markup fragment.

NOT COVERED HERE: the client-side render of that event. _ask_panel.html builds
its source chips with createElement/textContent, and nothing in this file would
fail if that were reverted to an innerHTML concatenation — asserting on it needs
a DOM, i.e. a JS runtime this suite deliberately does not have. Do not read a
green run here as proof the browser-side sink is safe.
"""
import json
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from transcript_analyzer.db import get_conn, upsert_transcript

NASTY_TITLE = "<img src=x onerror=fetch('/categorize/reset',{method:'POST'})>"


class _Document(HTMLParser):
    """The served page parsed into elements + text, rather than searched as a
    string: 'is there a live <img> in this document' is the actual question."""

    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.text: list[str] = []
        self._skip = 0
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)

    def tags(self) -> set[str]:
        return {t for t, _ in self.elements}

    def all_text(self) -> str:
        return "".join(self.text)


def _index_record(cfg, title: str) -> str:
    from transcript_analyzer.models import NoteRecord

    rec = NoteRecord(
        transcript_id="xss1",
        source="granola",
        title=title,
        date="2026-07-01",
        category="",
        people=[],
        topics=[],
        action_items=[],
        open_action_items=[],
        attendees=[],
        summary="A summary.",
        note_path="",
        transcript_text="Angela: hello.",
    )
    with get_conn(cfg.db_path) as conn:
        upsert_transcript(conn, rec)
    return rec.transcript_id


def test_jinja_renders_the_title_as_text_on_the_transcript_page(app_mod, cfg):
    tid = _index_record(cfg, NASTY_TITLE)

    with TestClient(app_mod.app) as client:
        r = client.get(f"/transcript/{tid}")

    assert r.status_code == 200
    doc = _Document(r.text)
    assert "img" not in doc.tags(), "the title was parsed as a live element"
    assert not any("onerror" in attrs for _tag, attrs in doc.elements)
    # It is still shown — as text.
    assert NASTY_TITLE in doc.all_text()


def test_jinja_renders_the_title_as_text_on_the_browse_page(app_mod, cfg):
    _index_record(cfg, NASTY_TITLE)

    with TestClient(app_mod.app) as client:
        r = client.get("/browse")

    assert r.status_code == 200
    doc = _Document(r.text)
    assert "img" not in doc.tags()
    assert NASTY_TITLE in doc.all_text()


def test_ask_sources_event_carries_the_title_as_json_data(app_mod, cfg, monkeypatch):
    """The server hands the Ask panel data, not markup: the title arrives as a
    JSON string value that round-trips exactly. What the panel then DOES with
    it is the uncovered client-side half described in the module docstring."""

    def fake_stream_events(question, **kwargs):
        yield ("sources", [{"n": 1, "title": NASTY_TITLE, "id": "xss1", "note_path": ""}])
        yield ("token", "answer text")

    monkeypatch.setattr(app_mod.rag, "stream_events", fake_stream_events)
    monkeypatch.setattr(app_mod, "LLM", lambda _cfg: object())

    with TestClient(app_mod.app) as client:
        r = client.post("/chat/ask", data={"question": "what happened?"})

    assert r.status_code == 200
    events = {}
    for block in r.text.split("\n\n"):
        kind, payload = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                payload += line[5:].strip()
        if payload:
            events.setdefault(kind, []).append(payload)

    sources = json.loads(events["sources"][0])
    assert [s["title"] for s in sources] == [NASTY_TITLE]
    assert [s["id"] for s in sources] == ["xss1"]
