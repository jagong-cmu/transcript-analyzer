"""On-demand categorization + scoped category rollups.

Notes are stored flat by date. This assigns each note to one of a user-provided
list of categories (using the Claude API) and writes non-destructive category
notes under `<insights_folder>/Categories/`. Each category note includes a
citation-gated overview, themes, and open threads synthesized from its members,
plus a mechanical conversation list and open commitments.

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
    get_meta,
    set_meta,
    set_note_category,
)
from ..models import NoteRecord
from ..obsidian import writer
from ..obsidian.writer import CATEGORIES_SUBDIR
from .llm import LLM, LLMError
from .synthesize import (
    CLAIM_SCHEMA,
    CITE_RULES,
    _claim_line,
    _entry,
    _footer,
    _hash_records,
    _stem,
    verify_claims,
)

NONE_LABEL = "None"

SYSTEM = """You sort meeting/conversation notes into the user's categories. You always
respond with a single JSON object choosing exactly one category from the allowed list,
or "None" if none fit well. Do not invent categories."""

USER_TEMPLATE = """Allowed categories: {categories}, or "None".

Pick the ONE best category for this note. Respond as JSON: {{"category": "<one of the allowed values>"}}.

Title: {title}
Topics: {topics}
Summary: {summary}"""

CATEGORY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": "2-4 sentences: what this category covers and where things stand.",
        },
        "themes": {
            "type": "array",
            "items": CLAIM_SCHEMA,
            "description": "Recurring patterns, decisions, and insights across conversations.",
        },
        "open_threads": {
            "type": "array",
            "items": CLAIM_SCHEMA,
            "description": "Unresolved questions or follow-ups still hanging in this category.",
        },
    },
    "required": ["overview", "themes", "open_threads"],
}

CATEGORY_SYSTEM = f"""You write a scoped briefing for one category of the user's
conversations. All conversations below belong to this category. Surface
cross-cutting themes and decisions, and every thread still open. Prefer
patterns seen across multiple conversations over one-offs. Be concrete.

{CITE_RULES}"""


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
        data = llm.chat_json(SYSTEM, user, schema=_schema(categories))
        choice = str(data.get("category", "")).strip()
    except LLMError:
        return None
    if choice == NONE_LABEL or choice not in categories:
        return None
    return choice


def _sanitize_filename(name: str) -> str:
    return name.replace("/", "-").replace("\\", "-").strip() or "Category"


def category_note_path(cfg: Config, category: str) -> Path:
    return cfg.vault.insights_path / CATEGORIES_SUBDIR / f"{_sanitize_filename(category)}.md"


def write_category_rollup(
    cfg: Config,
    llm: LLM,
    category: str,
    notes: list[NoteRecord],
    *,
    force: bool = False,
) -> dict:
    """Synthesize a citation-gated briefing into Categories/<Name>.md."""
    if not notes:
        return {"skipped": "empty"}

    path = category_note_path(cfg, category)
    digest_hash = _hash_records(notes)
    meta_key = f"category_hash:{category}"
    with get_conn(cfg.db_path) as conn:
        prev = get_meta(conn, meta_key)
    if prev == digest_hash and path.exists() and not force:
        return {"unchanged": len(notes)}

    by_id = {r.transcript_id: r for r in notes}
    corpus = "\n\n".join(
        _entry(r, with_open_items=True) for r in sorted(notes, key=lambda r: r.date)
    )
    user = (
        f"Category: {category}\n"
        f"{len(notes)} conversations in this category:\n\n{corpus}\n\n"
        "Write the category briefing."
    )
    data = llm.chat_json(CATEGORY_SYSTEM, user, schema=CATEGORY_SCHEMA)

    themes, d1 = verify_claims(data.get("themes", []), by_id)
    threads, d2 = verify_claims(data.get("open_threads", []), by_id)
    dropped = d1 + d2

    overview = str(data.get("overview", "")).strip()
    lines = [f"**{category}** · {len(notes)} conversation(s)", ""]
    if overview:
        lines.append(overview)
        lines.append("")
    if themes:
        lines.append("## Themes")
        lines.extend(_claim_line(c) for c in themes)
        lines.append("")
    if threads:
        lines.append("## Open threads")
        lines.extend(_claim_line(c) for c in threads)
        lines.append("")

    open_items = [(item, r) for r in notes for item in r.open_action_items]
    if open_items:
        lines.append("## Open commitments")
        lines.extend(f"- [ ] {item} ([[{_stem(r)}]])" for item, r in open_items)
        lines.append("")

    lines.append("## Conversations")
    lines.extend(
        f"- [[{_stem(r)}]] · {r.date} ({r.source})"
        for r in sorted(notes, key=lambda r: r.date, reverse=True)
    )
    lines.append(_footer(dropped))

    writer.write_managed(cfg, path, "\n".join(lines), title=category)
    with get_conn(cfg.db_path) as conn:
        set_meta(conn, meta_key, digest_hash)
    return {
        "conversations": len(notes),
        "themes": len(themes),
        "open_threads": len(threads),
        "dropped_claims": dropped,
    }


def _write_categories_hub(cfg: Config, assignments: dict[str, list[NoteRecord]]) -> None:
    cats_dir = cfg.vault.insights_path / CATEGORIES_SUBDIR
    cats_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Categories",
        "",
        "_On-demand category index with scoped rollups. Notes stay organized by date._",
        "",
    ]
    for cat in sorted(assignments, key=lambda c: (-len(assignments[c]), c)):
        lines.append(f"- [[{_sanitize_filename(cat)}]] ({len(assignments[cat])})")
    (cats_dir / "Categories.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_old_mocs(cfg: Config) -> None:
    cats_dir = cfg.vault.insights_path / CATEGORIES_SUBDIR
    if cats_dir.exists():
        for p in cats_dir.glob("*.md"):
            p.unlink()


def reset_categories(cfg: Optional[Config] = None, verbose: bool = True) -> dict:
    """Remove all category assignments, rollup notes, and cached hashes."""
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
        # Drop change-detection hashes so the next categorize always regenerates.
        conn.execute("DELETE FROM meta WHERE key LIKE 'category_hash:%'")
    if verbose:
        print(f"[reset] removed {removed} category note(s), cleared {cleared} assignment(s)")
    return {"mocs_removed": removed, "assignments_cleared": cleared}


def categorize(
    cfg: Optional[Config] = None,
    categories: Optional[list[str]] = None,
    llm: Optional[LLM] = None,
    verbose: bool = True,
    *,
    force_rollups: bool = True,
) -> dict:
    """Assign notes to categories, then synthesize a scoped rollup per category.

    force_rollups defaults to True on categorize so a fresh sort always refreshes
    insights even if membership hashes collide with a prior run.
    """
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

    # Clear stale MOCs, then write rollups + hub for non-empty categories.
    _clear_old_mocs(cfg)
    rollups: dict[str, dict] = {}
    for cat, items in assignments.items():
        if not items:
            continue
        if verbose:
            print(f"[categorize] synthesizing rollup for {cat} ({len(items)} notes)…")
        try:
            rollups[cat] = write_category_rollup(
                cfg, llm, cat, items, force=force_rollups
            )
        except LLMError as e:
            # Still write a mechanical stub so the category isn't empty in the vault.
            if verbose:
                print(f"[categorize] rollup failed for {cat}: {e}; writing list-only stub")
            stub = [
                f"**{cat}** · {len(items)} conversation(s)",
                "",
                "_Scoped rollup failed; conversation list only._",
                "",
                "## Conversations",
                *[
                    f"- [[{_stem(r)}]] · {r.date} ({r.source})"
                    for r in sorted(items, key=lambda r: r.date, reverse=True)
                ],
                _footer(0),
            ]
            writer.write_managed(
                cfg, category_note_path(cfg, cat), "\n".join(stub), title=cat
            )
            rollups[cat] = {"error": str(e), "conversations": len(items)}

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
        "rollups": rollups,
    }
    if verbose:
        print(f"[categorize] {summary['assigned']}/{summary['total']} assigned, "
              f"{summary['unassigned']} left uncategorized; "
              f"{len(rollups)} rollup(s) written")
    return summary
