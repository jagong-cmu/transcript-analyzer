"""The dashboard's lecture surface: a link to the PDF, and the long summary.

Existence on disk is the whole test for "is there a PDF" — study notes are
written only for lectures, and only when the renderer produced one — so a
dashboard that answers from the index alone would offer a download of a file
the vault does not hold.
"""
from fastapi.testclient import TestClient

from transcript_analyzer.db import get_conn, upsert_transcript
from transcript_analyzer.models import NoteRecord
from transcript_analyzer.obsidian import writer


def seed(cfg, app_mod, *, kind="lecture", with_pdf=True, tid="lec1"):
    note = cfg.vault.insights_path / f"2026-09-01 {tid}.md"
    note.write_text(
        f"---\nsource: pocket\ndate: 2026-09-01\ntranscript_id: {tid}\n---\n",
        encoding="utf-8",
    )
    if kind == "lecture":
        study = writer.write_study_note(cfg, note, tid, "study notes body")
        if with_pdf:
            writer.write_study_pdf(study, tid, b"%PDF-1.4 rendered")
    rec = NoteRecord(
        transcript_id=tid, source="pocket", title=f"Row reduction {tid}, September 1st, 2026",
        date="2026-09-01", category="",
        summary="One paragraph abstract.",
        detailed_summary="### Opening\n\nThe class opened with a **recap**.",
        kind=kind, course_code="21-241", course_name="Linear Algebra",
        note_path=str(note), transcript_text="[0:01] hello",
    )
    with get_conn(cfg.db_path) as conn:
        upsert_transcript(conn, rec)
    return note


def test_a_lecture_offers_its_pdf_and_the_long_summary(cfg, app_mod):
    seed(cfg, app_mod)
    client = TestClient(app_mod.app)

    page = client.get("/transcript/lec1").text
    assert "/study/lec1" in page
    # The body summary is the long one, rendered as markup, not escaped text.
    assert "<h3>Opening</h3>" in page
    assert "<strong>recap</strong>" in page

    pdf = client.get("/study/lec1")
    assert pdf.status_code == 200
    assert pdf.content == b"%PDF-1.4 rendered"
    assert pdf.headers["content-type"] == "application/pdf"


def test_a_lecture_without_a_rendered_pdf_offers_no_download(cfg, app_mod):
    """The renderer may have been unavailable; the page must not lie."""
    seed(cfg, app_mod, with_pdf=False)
    client = TestClient(app_mod.app)
    assert "/study/lec1" not in client.get("/transcript/lec1").text
    assert client.get("/study/lec1").status_code == 404


def test_a_meeting_has_no_study_surface_at_all(cfg, app_mod):
    seed(cfg, app_mod, kind="meeting", tid="mtg1")
    client = TestClient(app_mod.app)
    assert "/study/mtg1" not in client.get("/transcript/mtg1").text
    assert client.get("/study/mtg1").status_code == 404


def test_the_lectures_page_groups_by_course(cfg, app_mod):
    seed(cfg, app_mod, tid="lec1")
    seed(cfg, app_mod, tid="lec2", with_pdf=False)
    seed(cfg, app_mod, kind="meeting", tid="mtg1")

    page = TestClient(app_mod.app).get("/lectures").text
    assert "21-241 · Linear Algebra" in page
    assert "/transcript/lec1" in page and "/transcript/lec2" in page
    assert "/transcript/mtg1" not in page  # not a lecture
    assert "/study/lec1" in page and "/study/lec2" not in page


def test_the_lectures_page_is_fine_with_an_empty_vault(cfg, app_mod):
    page = TestClient(app_mod.app).get("/lectures")
    assert page.status_code == 200
    assert "No lectures yet" in page.text
