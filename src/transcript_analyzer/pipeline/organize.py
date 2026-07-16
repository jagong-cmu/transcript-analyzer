"""On-demand categorization.

Notes are stored flat by date. This assigns each note to one of a user-provided
list of categories (using the local LLM) and creates non-destructive category
index notes (MOCs) in the vault under `<insights_folder>/Categories/`. The note
files themselves are never moved; categories are an overlay.

Usage:
    from transcript_analyzer.pipeline.organize import categorize
    categorize(cfg, ["Fundraising", "Hiring", "Product"])
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Config, load_config
from ..db import (
    all_transcripts,
    clear_note_categories,
    get_conn,
    set_note_category,
)
from ..models import NoteRecord
from ..obsidian.writer import CATEGORIES_SUBDIR
from .llm import LLM

NONE_LABEL = "None"

SYSTEM = """You sort meeting/conversation notes into the user's categories. You always
respond with a single JSON object choosing exactly one category from the allowed list,
or "None" if none fit well. Do not invent categories."""

USER_TEMPLATE = """Allowed categories: {categories}, or "None".

Pick the ONE best category for this note. Respond as JSON: {{"category": "<one of the allowed values>"}}.

Title: {title}
Topics: {topics}
Summary: {summary}"""


def _schema(categories: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"category": {"type": "string", "enum": categories + [NONE_LABEL]}},
        "required": ["category"],
    }


def _classify(llm: LLM, note: NoteRecord, categories: list[str]) -> Optional[str]:
    user = USER_TEMPLATE.format(
        categories=", ".join(f'"{c}"' for c in categories),
        title=note.title,
        topics=", ".join(note.topics) or "(none)",
        summary=note.summary or "(no summary)",
    )
    try:
        data = llm.chat_json(SYSTEM, user, schema=_schema(categories),
                             options={"temperature": 0.0, "num_ctx": 4096})
        choice = str(data.get("category", "")).strip()
    except Exception:  # noqa: BLE001
        return None
    if choice == NONE_LABEL or choice not in categories:
        return None
    return choice


def _sanitize_filename(name: str) -> str:
    return name.replace("/", "-").replace("\\", "-").strip() or "Category"


def _write_moc(cfg: Config, category: str, notes: list[NoteRecord]) -> Path:
    cats_dir = cfg.vault.insights_path / CATEGORIES_SUBDIR
    cats_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# {category}", "", f"_{len(notes)} conversation(s)._", ""]
    for n in sorted(notes, key=lambda r: r.date, reverse=True):
        stem = Path(n.note_path).stem if n.note_path else n.title
        lines.append(f"- [[{stem}]] · {n.date}")
    path = cats_dir / f"{_sanitize_filename(category)}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_categories_hub(cfg: Config, assignments: dict[str, list[NoteRecord]]) -> None:
    cats_dir = cfg.vault.insights_path / CATEGORIES_SUBDIR
    lines = ["# Categories", "", "_On-demand category index. Notes stay organized by date._", ""]
    for cat in sorted(assignments, key=lambda c: (-len(assignments[c]), c)):
        lines.append(f"- [[{_sanitize_filename(cat)}]] ({len(assignments[cat])})")
    (cats_dir / "Categories.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_old_mocs(cfg: Config) -> None:
    cats_dir = cfg.vault.insights_path / CATEGORIES_SUBDIR
    if cats_dir.exists():
        for p in cats_dir.glob("*.md"):
            p.unlink()


def reset_categories(cfg: Optional[Config] = None, verbose: bool = True) -> dict:
    """Remove all category assignments and MOC notes. Notes stay date-organized."""
    import shutil

    cfg = cfg or load_config()
    cats_dir = cfg.vault.insights_path / CATEGORIES_SUBDIR
    removed = 0
    if cats_dir.exists():
        removed = len(list(cats_dir.glob("*.md")))
        shutil.rmtree(cats_dir, ignore_errors=True)
    with get_conn(cfg.db_path) as conn:
        cleared = conn.execute("SELECT COUNT(*) FROM note_categories").fetchone()[0]
        clear_note_categories(conn)
    if verbose:
        print(f"[reset] removed {removed} category note(s), cleared {cleared} assignment(s)")
    return {"mocs_removed": removed, "assignments_cleared": cleared}


def categorize(
    cfg: Optional[Config] = None,
    categories: Optional[list[str]] = None,
    llm: Optional[LLM] = None,
    verbose: bool = True,
) -> dict:
    cfg = cfg or load_config()
    categories = [c.strip() for c in (categories or []) if c.strip()]
    if not categories:
        raise ValueError("Provide at least one category, e.g. categorize(cfg, ['Hiring', 'Product']).")
    llm = llm or LLM(cfg)

    with get_conn(cfg.db_path) as conn:
        notes = all_transcripts(conn)

    assignments: dict[str, list[NoteRecord]] = {c: [] for c in categories}
    unassigned = 0
    for note in notes:
        cat = _classify(llm, note, categories)
        if cat:
            assignments[cat].append(note)
            if verbose:
                print(f"  {cat:<20} {note.title}")
        else:
            unassigned += 1

    # Rewrite MOC notes (clear stale) + DB assignments.
    _clear_old_mocs(cfg)
    for cat, items in assignments.items():
        if items:
            _write_moc(cfg, cat, items)
    _write_categories_hub(cfg, {c: v for c, v in assignments.items() if v})

    with get_conn(cfg.db_path) as conn:
        clear_note_categories(conn)
        for cat, items in assignments.items():
            for n in items:
                set_note_category(conn, n.transcript_id, cat)

    summary = {
        "categories": {c: len(v) for c, v in assignments.items()},
        "assigned": sum(len(v) for v in assignments.values()),
        "unassigned": unassigned,
        "total": len(notes),
    }
    if verbose:
        print(f"[categorize] {summary['assigned']}/{summary['total']} assigned, "
              f"{summary['unassigned']} left uncategorized")
    return summary
