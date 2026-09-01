"""A lecture produces study notes and a PDF; a meeting produces neither.

The whole point of detecting a lecture is that it changes what the pipeline
buys. This drives sync end to end with a stubbed API so the branch — and the
links it puts in the note — are exercised, not asserted about in theory.
"""
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from transcript_analyzer import sync
from transcript_analyzer.db import all_transcripts, get_conn, known_course_rows
from transcript_analyzer.models import Transcript
from transcript_analyzer.obsidian import writer
from transcript_analyzer.pipeline import lecture as lecture_mod

LECTURE_TEXT = (
    "[0:12] Okay so today we are row reducing a three by three matrix.\n"
    "[1:30] The first pivot is the leading entry in row one.\n"
    "[2:05] Homework three is due Friday at midnight.\n"
)
MEETING_TEXT = "[0:03] Angela: I will review the pricing deck by Friday.\n"


def transcript(tid, text, title) -> Transcript:
    return Transcript(
        id=tid, source="pocket", native_id=f"n-{tid}", title=title,
        date=date(2026, 9, 1), text=text,
    )


def extraction(kind, code="", name="", title="A recording"):
    return {
        "title": title,
        "kind": kind,
        "course_code": code,
        "course_name": name,
        "abstract": "One paragraph abstract.",
        "detailed_summary": "A much longer summary of what happened, at length.",
        "key_points": ["A point"],
        "action_items": ["Do the thing"],
        "people": ["Angela Jin"],
        "topics": ["pricing"],
        "sentiment": "neutral",
    }


STUDY_PAYLOAD = {
    "overview": "The class row reduced a three by three matrix.",
    "sections": [
        {
            "heading": "Row reduction",
            "body": "Swap, scale, eliminate.",
            "anchor": "row reducing a three by three matrix",
            "visuals": [
                {"kind": "mermaid", "caption": "Order of row operations.",
                 "source": "flowchart TD\n  A-->B", "language": ""}
            ],
        }
    ],
    "key_terms": ["pivot — the leading nonzero entry"],
    "assessment": [{"text": "Homework 3 is due Friday.",
                    "quote": "Homework three is due Friday"}],
    "background": [{"heading": "RREF", "body": "Reduced row echelon form."}],
    "asr_repairs": [],
}


class StubLLM:
    """Answers each stage with the payload that stage's schema expects."""

    def __init__(self, extraction_payload):
        self.extraction_payload = extraction_payload
        self.stages = []

    def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
        self.stages.append(stage)
        return self.extraction_payload if stage == "extract" else STUDY_PAYLOAD

    def health(self):
        return {"ok": True, "kill_switch": False, "key_configured": True,
                "month_spend_usd": 0.0, "monthly_budget_usd": 5.0}


@pytest.fixture
def no_pdf_cfg(cfg):
    """PDF rendering off: the browser path has its own (guarded) test."""
    return replace(cfg, lecture=replace(cfg.lecture, pdf=False))


def run(cfg, llm, t):
    return sync.process_transcript(cfg, t, llm)


def test_a_lecture_gets_study_notes_linked_from_its_note(no_pdf_cfg):
    cfg = no_pdf_cfg
    llm = StubLLM(extraction("lecture", "21-241", "Linear Algebra", "Row reducing a 3x3 matrix"))
    res = run(cfg, llm, transcript("lec1", LECTURE_TEXT, "Lecture"))

    assert res["kind"] == "lecture"
    assert llm.stages == ["extract", "lecture"]

    study = cfg.vault.insights_path / writer.STUDY_SUBDIR
    written = list(study.glob("*.md"))
    assert len(written) == 1
    notes = written[0].read_text()
    assert "```mermaid" in notes and "Order of row operations." in notes
    assert "Homework 3 is due Friday." in notes
    assert "Background (not from lecture)" in notes

    note = Path(res["note_path"]).read_text()
    assert "## Study Notes" in note
    assert f"[[{written[0].stem}|Full study notes]]" in note
    # No PDF was rendered, so neither rendering of the lecture links one.
    assert "Printable PDF" not in note and "Printable PDF" not in notes
    # The study-notes overview becomes the note's detailed summary.
    assert "The class row reduced a three by three matrix." in note
    assert 'kind: "lecture"' in note and 'course_code: "21-241"' in note


def test_a_meeting_gets_a_detailed_summary_and_no_study_notes(no_pdf_cfg):
    cfg = no_pdf_cfg
    llm = StubLLM(extraction("meeting"))
    res = run(cfg, llm, transcript("mtg1", MEETING_TEXT, "Sync"))

    assert res["kind"] == "meeting"
    assert res["study_notes"] is None and res["study_pdf"] is None
    assert llm.stages == ["extract"]  # no lecture call was bought
    assert not (cfg.vault.insights_path / writer.STUDY_SUBDIR).exists()

    note = Path(res["note_path"]).read_text()
    assert "A much longer summary of what happened, at length." in note
    assert "## Study Notes" not in note


def test_week_two_of_a_course_binds_to_week_one(no_pdf_cfg):
    cfg = no_pdf_cfg
    run(cfg, StubLLM(extraction("lecture", "21-241", "Linear Algebra", "Week one")),
        transcript("lec1", LECTURE_TEXT, "Lecture 1"))
    # Next week the model writes the code differently and omits the name.
    run(cfg, StubLLM(extraction("lecture", "21241", "", "Week two")),
        transcript("lec2", LECTURE_TEXT, "Lecture 2"))

    with get_conn(cfg.db_path) as conn:
        rows = known_course_rows(conn)
        recs = {r.transcript_id: r for r in all_transcripts(conn)}
    assert {code for code, _n in rows} == {"21-241"}
    assert recs["lec2"].course_name == "Linear Algebra"


def test_the_lecture_pass_failing_still_leaves_a_usable_note(no_pdf_cfg, monkeypatch):
    """Study notes are an upgrade, never a precondition for the note."""
    cfg = no_pdf_cfg

    def boom(*a, **k):
        raise RuntimeError("diagram service exploded")

    monkeypatch.setattr(lecture_mod, "produce", boom)
    llm = StubLLM(extraction("lecture", "21-241", "Linear Algebra"))
    res = run(cfg, llm, transcript("lec3", LECTURE_TEXT, "Lecture"))

    assert res["study_notes"] is None
    note = Path(res["note_path"]).read_text()
    assert "A much longer summary of what happened, at length." in note
    assert 'kind: "lecture"' in note


def test_study_notes_can_be_turned_off_entirely(cfg):
    cfg = replace(cfg, lecture=replace(cfg.lecture, enabled=False))
    llm = StubLLM(extraction("lecture", "21-241", "Linear Algebra"))
    res = run(cfg, llm, transcript("lec4", LECTURE_TEXT, "Lecture"))
    assert res["study_notes"] is None
    assert llm.stages == ["extract"]


def test_the_study_notes_namespace_is_never_indexed_as_a_transcript(no_pdf_cfg):
    """The feedback-loop guard: synthesis output must not become input."""
    cfg = no_pdf_cfg
    llm = StubLLM(extraction("lecture", "21-241", "Linear Algebra"))
    run(cfg, llm, transcript("lec5", LECTURE_TEXT, "Lecture"))

    from transcript_analyzer.pipeline.indexer import reindex_all

    reindex_all(cfg)
    with get_conn(cfg.db_path) as conn:
        ids = {r.transcript_id for r in all_transcripts(conn)}
    assert ids == {"lec5"}


def test_a_missing_asset_cache_costs_the_pdf_and_nothing_else(cfg, monkeypatch):
    """The documented contract: no render assets, no PDF — the notes survive.

    The failure has to arrive at the lecture pass as a PdfRenderError. Any
    other exception is caught by sync's blanket handler, which throws away the
    whole study-notes result — the markdown included — over a missing library.
    """
    pytest.importorskip("playwright.sync_api")
    from transcript_analyzer.render import assets, pdf as pdf_render

    def no_assets(data_dir, dest):
        raise assets.AssetError("cdn unreachable and nothing cached")

    monkeypatch.setattr(pdf_render, "playwright_available", lambda: True)
    monkeypatch.setattr(assets, "stage_assets", no_assets)

    llm = StubLLM(extraction("lecture", "21-241", "Linear Algebra"))
    res = run(cfg, llm, transcript("lec6", LECTURE_TEXT, "Lecture"))

    assert res["study_notes"] is not None, "the markdown study notes were lost"
    assert res["study_pdf"] is None
    assert "Order of row operations." in Path(res["study_notes"]).read_text()

    note = Path(res["note_path"]).read_text()
    assert "|Full study notes]]" in note
    # And no download is offered for a file the vault does not hold.
    assert "Printable PDF" not in note


def test_the_note_links_the_pdf_exactly_when_one_was_written(cfg, monkeypatch):
    """The transcript note and the study note must agree about the PDF."""
    from transcript_analyzer.render import pdf as pdf_render

    monkeypatch.setattr(
        pdf_render,
        "render_pdf",
        lambda html, data_dir: pdf_render.RenderResult(pdf=b"%PDF-1.4 fake", kept=1),
    )
    llm = StubLLM(extraction("lecture", "21-241", "Linear Algebra"))
    res = run(cfg, llm, transcript("lec7", LECTURE_TEXT, "Lecture"))

    stem = Path(res["study_notes"]).stem
    assert Path(res["study_pdf"]).read_bytes() == b"%PDF-1.4 fake"
    assert f"[[{stem}.pdf|Printable PDF]]" in Path(res["note_path"]).read_text()
    assert f"[[{stem}.pdf|Printable PDF]]" in Path(res["study_notes"]).read_text()


def test_a_truncated_study_notes_response_fails_the_transcript(no_pdf_cfg):
    """A response cut off at lecture_max_tokens must be RETRIED, not absorbed.

    Swallowing it wrote a downgraded note and then recorded the transcript's
    hash as done, so the recording was skipped forever. Propagating instead
    leaves record_sync unreached and the next cycle reprocesses it.
    """
    from transcript_analyzer.pipeline.llm import LLMResponseError

    cfg = no_pdf_cfg

    class TruncatingLLM(StubLLM):
        def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
            if stage == "lecture":
                raise LLMResponseError(
                    "Structured output truncated at max_tokens; raise the limit."
                )
            return super().chat_json(
                system, user, schema, max_tokens=max_tokens, stage=stage, stream=stream
            )

    llm = TruncatingLLM(extraction("lecture", "21-241", "Linear Algebra"))
    t = transcript("lec8", LECTURE_TEXT, "Lecture")

    with pytest.raises(LLMResponseError):
        run(cfg, llm, t)

    # Nothing was recorded as synced, so the next cycle picks it up again.
    from transcript_analyzer.db import get_sync_note_path

    with get_conn(cfg.db_path) as conn:
        assert get_sync_note_path(conn, t.source, t.native_id) is None


def test_a_contained_study_failure_still_writes_the_note(no_pdf_cfg):
    """Only unparseable responses propagate; the rest stay contained."""
    cfg = no_pdf_cfg

    class BrokenLLM(StubLLM):
        def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
            if stage == "lecture":
                raise RuntimeError("diagram service exploded")
            return super().chat_json(
                system, user, schema, max_tokens=max_tokens, stage=stage, stream=stream
            )

    res = run(cfg, BrokenLLM(extraction("lecture", "21-241", "Linear Algebra")),
              transcript("lec9", LECTURE_TEXT, "Lecture"))

    assert res["study_notes"] is None
    assert "A much longer summary of what happened, at length." in Path(res["note_path"]).read_text()


def test_a_truncated_lecture_bills_once_and_marks_the_note(no_pdf_cfg):
    """Truncation at the output cap is DETERMINISTIC, so retrying it is waste.

    The same transcript against the same cap overflows identically forever, so
    an unbounded retry re-pays for a 32k-output Opus 5 call every sync cycle
    until the monthly ceiling halts ingestion for everything else. The note is
    written once, carrying a marker a reader can see, and the transcript is
    recorded as synced so no later cycle pays again.
    """
    from transcript_analyzer.pipeline.llm import LLMTruncatedError

    cfg = no_pdf_cfg

    class TruncatingLLM(StubLLM):
        def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
            self.stages.append(stage)
            if stage == "lecture":
                raise LLMTruncatedError(
                    "Structured output truncated at max_tokens; raise the limit."
                )
            return self.extraction_payload

    llm = TruncatingLLM(extraction("lecture", "21-241", "Linear Algebra"))
    t = transcript("lec10", LECTURE_TEXT, "Lecture")

    res = run(cfg, llm, t)

    assert res["study_notes"] is None and res["study_pdf"] is None
    assert "truncated at max_tokens" in res["study_error"]
    assert llm.stages == ["extract", "lecture"]

    # Recorded as synced under its own hash: the next cycle short-circuits and
    # buys nothing at all.
    from transcript_analyzer.db import get_sync_hash

    with get_conn(cfg.db_path) as conn:
        assert get_sync_hash(conn, t.source, t.native_id) == t.hash

    note = Path(res["note_path"]).read_text()
    assert "## Study Notes" in note
    assert "could not be generated" in note
    assert "truncated at max_tokens" in note
    assert "study_notes_error:" in note
    # And it never claims study notes exist.
    assert "Full study notes" not in note and "Printable PDF" not in note


def test_a_truncated_refresh_keeps_the_notes_already_on_disk(no_pdf_cfg):
    """A failed refresh is not the same as having none: keep linking them."""
    from transcript_analyzer.pipeline.llm import LLMTruncatedError

    cfg = no_pdf_cfg
    llm = StubLLM(extraction("lecture", "21-241", "Linear Algebra"))
    first = run(cfg, llm, transcript("lec11", LECTURE_TEXT, "Lecture"))
    study_stem = Path(first["study_notes"]).stem

    class TruncatingLLM(StubLLM):
        def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
            self.stages.append(stage)
            if stage == "lecture":
                raise LLMTruncatedError("Structured output truncated at max_tokens")
            return self.extraction_payload

    again = run(cfg, TruncatingLLM(extraction("lecture", "21-241", "Linear Algebra")),
                transcript("lec11", LECTURE_TEXT, "Lecture"))

    note = Path(again["note_path"]).read_text()
    assert f"[[{study_stem}|Full study notes]]" in note
    assert "could not be refreshed" in note
    assert "study_notes_error:" in note


def test_a_retryable_lecture_failure_is_bounded_not_endless(no_pdf_cfg):
    """A response that MIGHT be transient is retried — a bounded number of
    times, then written with the same terminal marker."""
    from transcript_analyzer.pipeline.llm import LLMResponseError

    cfg = no_pdf_cfg

    class BadJson(StubLLM):
        def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
            self.stages.append(stage)
            if stage == "lecture":
                raise LLMResponseError("Claude returned invalid JSON")
            return self.extraction_payload

    t = transcript("lec12", LECTURE_TEXT, "Lecture")
    payload = extraction("lecture", "21-241", "Linear Algebra")

    for _ in range(sync.STUDY_NOTE_MAX_ATTEMPTS - 1):
        with pytest.raises(LLMResponseError):
            run(cfg, BadJson(payload), t)

    res = run(cfg, BadJson(payload), t)
    assert "invalid JSON" in res["study_error"]
    assert "could not be generated" in Path(res["note_path"]).read_text()
