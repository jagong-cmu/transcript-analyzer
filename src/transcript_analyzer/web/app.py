"""Local FastAPI dashboard: browse grouped conversations + insights, and chat (RAG)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..db import (
    all_transcripts,
    category_counts,
    get_conn,
    get_transcript,
    transcripts_in_category,
)
from ..pipeline.llm import LLM
from .. import rag

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


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with get_conn(cfg.db_path) as conn:
        cats = category_counts(conn)
        recent = all_transcripts(conn)[:12]
        action_items = []
        for rec in all_transcripts(conn):
            for a in rec.action_items:
                action_items.append({"text": a, "title": rec.title, "id": rec.transcript_id})
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "categories": cats,
            "recent": recent,
            "action_items": action_items[:25],
            "total": sum(n for _, n in cats),
        },
    )


@app.get("/category/{name}", response_class=HTMLResponse)
def category(request: Request, name: str):
    with get_conn(cfg.db_path) as conn:
        items = transcripts_in_category(conn, name)
        cats = category_counts(conn)
    return templates.TemplateResponse(
        request,
        "category.html",
        {"category": name, "items": items, "categories": cats},
    )


@app.get("/transcript/{tid}", response_class=HTMLResponse)
def transcript(request: Request, tid: str):
    with get_conn(cfg.db_path) as conn:
        rec = get_transcript(conn, tid)
        cats = category_counts(conn)
    if rec is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "transcript.html",
        {"rec": rec, "categories": cats, "obsidian_url": obsidian_uri(rec.note_path)},
    )


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    with get_conn(cfg.db_path) as conn:
        cats = category_counts(conn)
    return templates.TemplateResponse(request, "chat.html", {"categories": cats})


@app.post("/chat/ask")
def chat_ask(question: str = Form(...)):
    """Stream the answer via Server-Sent Events; send sources first as a JSON event."""
    llm = LLM(cfg)
    sources, tokens = rag.answer_stream(question, cfg=cfg, llm=llm)

    def event_gen():
        src_payload = [
            {"n": i + 1, "title": s.title, "id": s.transcript_id,
             "obsidian": obsidian_uri(s.note_path)}
            for i, s in enumerate(sources)
        ]
        yield f"event: sources\ndata: {json.dumps(src_payload)}\n\n"
        try:
            for tok in tokens:
                yield f"data: {json.dumps(tok)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps(' [error: ' + str(e) + ']')}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/health")
def health():
    llm = LLM(cfg)
    return llm.health()


def run():
    import uvicorn

    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port)


if __name__ == "__main__":
    run()
