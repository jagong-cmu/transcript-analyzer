"""Local FastAPI dashboard: synthesis briefing + browse + chat (RAG)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import rag
from ..config import load_config
from ..db import (
    all_transcripts,
    categories_for,
    category_counts,
    get_conn,
    get_meta,
    get_transcript,
    set_meta,
    transcripts_in_category,
)
from ..obsidian import writer
from ..pipeline import organize, synthesize
from ..pipeline.llm import LLM, LLMError
from ..pipeline.synthesize import LAST_RUN_KEY
from . import synth_reader

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Transcript Analyzer")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

cfg = load_config()


def obsidian_uri(note_path: str) -> str:
    """Build an obsidian://open deep link from an absolute note path."""
    try:
        rel = Path(note_path).resolve().relative_to(cfg.vault.path.resolve())
    except (ValueError, OSError):
        return ""
    return f"obsidian://open?vault={quote(cfg.vault.name)}&file={quote(str(rel.with_suffix('')))}"


templates.env.filters["obsidian"] = obsidian_uri


def _cats():
    with get_conn(cfg.db_path) as conn:
        return category_counts(conn)


def _by_stem():
    with get_conn(cfg.db_path) as conn:
        return synth_reader.stem_index(all_transcripts(conn))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    briefing = synth_reader.load_briefing(cfg)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "categories": _cats(),
            "digest": briefing["digest"],
            "commitments": briefing["commitments"][:12],
            "open_count": briefing["open_count"],
            "people": briefing["people"][:8],
            "people_total": len(briefing["people"]),
            "studies": briefing["studies"],
            "prep": briefing["prep"][:5],
            "status": briefing["status"],
            "total": briefing["total"],
            "digest_dates": synth_reader.list_digest_dates(cfg)[:14],
        },
    )


@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request):
    with get_conn(cfg.db_path) as conn:
        cats = category_counts(conn)
        records = all_transcripts(conn)
        action_items = []
        for rec in records:
            for a in rec.action_items:
                action_items.append(
                    {"text": a, "title": rec.title, "id": rec.transcript_id}
                )

    by_month: dict[str, list] = defaultdict(list)
    for rec in records:
        month = rec.date[:7] if len(rec.date) >= 7 else "undated"
        by_month[month].append(rec)
    timeline = sorted(by_month.items(), key=lambda kv: kv[0], reverse=True)

    return templates.TemplateResponse(
        request,
        "browse.html",
        {
            "categories": cats,
            "timeline": timeline,
            "action_items": action_items[:25],
            "total": len(records),
            "suggested_categories": ", ".join(name for name, _ in cats),
        },
    )


def _parse_categories(raw: str) -> list[str]:
    raw = raw.strip()
    if "," in raw:
        return [c.strip() for c in raw.split(",") if c.strip()]
    return [c for c in raw.split() if c]


@app.post("/categorize")
def categorize_now(categories: str = Form(...)):
    """Run Claude categorization across all notes into the given labels.
    Notes stay date-organized; this rewrites the Categories/ overlay + DB."""
    cats = _parse_categories(categories)
    if not cats:
        return JSONResponse(
            {"ok": False, "error": "Provide at least one category."}, status_code=400
        )
    try:
        summary = organize.categorize(cfg, categories=cats, verbose=False)
    except LLMError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "summary": summary})


@app.post("/categorize/reset")
def categorize_reset():
    """Clear all category assignments and MOC notes."""
    try:
        summary = organize.reset_categories(cfg, verbose=False)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "summary": summary})


@app.get("/commitments", response_class=HTMLResponse)
def commitments_page(request: Request):
    with get_conn(cfg.db_path) as conn:
        records = all_transcripts(conn)
    groups = synth_reader.commitments_from_records(records)
    return templates.TemplateResponse(
        request,
        "commitments.html",
        {
            "categories": _cats(),
            "commitments": groups,
            "open_count": sum(len(g.items) for g in groups),
            "status": synth_reader.synthesis_status(cfg),
        },
    )


@app.get("/people", response_class=HTMLResponse)
def people_index(request: Request):
    people = synth_reader.list_people(cfg, _by_stem())
    return templates.TemplateResponse(
        request,
        "people.html",
        {"categories": _cats(), "people": people, "status": synth_reader.synthesis_status(cfg)},
    )


@app.get("/people/{name}", response_class=HTMLResponse)
def person_page(request: Request, name: str):
    name = unquote(name)
    card = synth_reader.load_person(cfg, name, _by_stem())
    if card is None:
        return HTMLResponse("Person not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "person.html",
        {
            "categories": _cats(),
            "person": card,
            "obsidian_url": obsidian_uri(str(card.path)),
            "status": synth_reader.synthesis_status(cfg),
        },
    )


@app.get("/studies", response_class=HTMLResponse)
def studies_index(request: Request):
    studies = synth_reader.list_studies(cfg, _by_stem())
    return templates.TemplateResponse(
        request,
        "studies.html",
        {"categories": _cats(), "studies": studies, "status": synth_reader.synthesis_status(cfg)},
    )


@app.get("/studies/{name}", response_class=HTMLResponse)
def study_page(request: Request, name: str):
    name = unquote(name)
    card = synth_reader.load_study(cfg, name, _by_stem())
    if card is None:
        return HTMLResponse("Study not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "study.html",
        {
            "categories": _cats(),
            "study": card,
            "obsidian_url": obsidian_uri(str(card.path)),
            "status": synth_reader.synthesis_status(cfg),
        },
    )


@app.get("/prep", response_class=HTMLResponse)
def prep_index(request: Request):
    prep = synth_reader.list_prep(cfg, _by_stem())
    return templates.TemplateResponse(
        request,
        "prep.html",
        {"categories": _cats(), "prep": prep, "status": synth_reader.synthesis_status(cfg)},
    )


@app.get("/digests/{day}", response_class=HTMLResponse)
def digest_page(request: Request, day: str):
    digest = synth_reader.load_digest(cfg, day, _by_stem())
    if not digest.exists:
        return HTMLResponse("Digest not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "digest.html",
        {
            "categories": _cats(),
            "digest": digest,
            "digest_dates": synth_reader.list_digest_dates(cfg)[:30],
            "obsidian_url": obsidian_uri(str(digest.path)),
            "status": synth_reader.synthesis_status(cfg),
        },
    )


@app.post("/synthesize")
def synthesize_now(force: str = Form("false")):
    """Run the synthesis engine. force=true bypasses the once-per-day cadence guard
    and rewrites dossiers/studies even when inputs are unchanged."""
    if not cfg.synthesis.enabled:
        return JSONResponse(
            {"ok": False, "error": "synthesis is disabled in config.toml"}, status_code=400
        )
    do_force = force.lower() in ("1", "true", "yes", "on")

    from datetime import date

    llm = LLM(cfg)
    try:
        if do_force:
            summary = synthesize.run(cfg, llm, force=True, verbose=False)
            with get_conn(cfg.db_path) as conn:
                set_meta(conn, LAST_RUN_KEY, date.today().isoformat())
        else:
            summary = synthesize.maybe_run(cfg, llm, verbose=False)
            if summary is None:
                with get_conn(cfg.db_path) as conn:
                    last = get_meta(conn, LAST_RUN_KEY)
                return JSONResponse(
                    {
                        "ok": True,
                        "skipped": True,
                        "message": f"Already synthesized today ({last}). Use force to re-run.",
                        "last_run": last,
                    }
                )
    except LLMError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": True, "skipped": False, "summary": summary})


@app.get("/category/{name}", response_class=HTMLResponse)
def category(request: Request, name: str):
    name = unquote(name)
    with get_conn(cfg.db_path) as conn:
        items = transcripts_in_category(conn, name)
        cats = category_counts(conn)
        records = all_transcripts(conn)
    insight = synth_reader.load_category_insight(
        cfg, name, synth_reader.stem_index(records)
    )
    return templates.TemplateResponse(
        request,
        "category.html",
        {
            "category": name,
            "items": items,
            "categories": cats,
            "insight": insight,
            "obsidian_url": obsidian_uri(str(insight.path)) if insight.exists else "",
        },
    )


@app.get("/transcript/{tid}", response_class=HTMLResponse)
def transcript(request: Request, tid: str):
    with get_conn(cfg.db_path) as conn:
        rec = get_transcript(conn, tid)
        cats = category_counts(conn)
        note_cats = categories_for(conn, tid) if rec else []
    if rec is None:
        return HTMLResponse("Not found", status_code=404)
    has_audio = (
        writer.audio_path_for(cfg, Path(rec.note_path)).exists() if rec.note_path else False
    )
    return templates.TemplateResponse(
        request,
        "transcript.html",
        {
            "rec": rec,
            "categories": cats,
            "note_categories": note_cats,
            "obsidian_url": obsidian_uri(rec.note_path),
            "has_audio": has_audio,
        },
    )


@app.get("/audio/{tid}")
def audio(tid: str):
    with get_conn(cfg.db_path) as conn:
        rec = get_transcript(conn, tid)
    if rec is None or not rec.note_path:
        return HTMLResponse("Not found", status_code=404)
    path = writer.audio_path_for(cfg, Path(rec.note_path))
    if not path.exists():
        return HTMLResponse("No audio", status_code=404)
    return FileResponse(str(path), media_type="audio/mpeg", filename=path.name)


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    with get_conn(cfg.db_path) as conn:
        cats = category_counts(conn)
    return templates.TemplateResponse(request, "chat.html", {"categories": cats})


@app.post("/chat/ask")
def chat_ask(
    question: str = Form(...),
    category: str = Form(""),
    transcript_id: str = Form(""),
):
    """Stream the answer via Server-Sent Events. Optional category / transcript_id
    scopes the corpus to the page the user is viewing."""
    llm = LLM(cfg)
    cat = category.strip() or None
    tid = transcript_id.strip() or None

    def event_gen():
        try:
            for kind, payload in rag.stream_events(
                question, cfg=cfg, llm=llm, category=cat, transcript_id=tid
            ):
                if kind == "token":
                    yield f"data: {json.dumps(payload)}\n\n"
                elif kind == "sources":
                    src_payload = [
                        {
                            "n": s["n"],
                            "title": s["title"],
                            "id": s["id"],
                            "obsidian": obsidian_uri(s["note_path"]),
                        }
                        for s in payload
                    ]
                    yield f"event: sources\ndata: {json.dumps(src_payload)}\n\n"
        except ValueError as e:
            yield f"data: {json.dumps(' [error: ' + str(e) + ']')}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps(' [error: ' + str(e) + ']')}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/health")
def health():
    """API/key status plus the cost guard's view of this month's spend."""
    llm = LLM(cfg)
    return llm.health()


def run():
    import uvicorn

    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port)


if __name__ == "__main__":
    run()
