"""On-demand categorization + scoped category rollups.

Notes are stored flat by date. This assigns each note to one of a user-provided
list of categories (using the Claude API) and writes non-destructive category
notes under `<insights_folder>/Categories/`. Each category note includes a
citation-gated overview, themes, and open threads synthesized from its members,
plus a mechanical conversation list and open commitments.

Category definitions may include a description that steers matching — Claude
uses the description (not just the label) when placing transcripts.

Usage:
    from transcript_analyzer.pipeline.organize import CategoryDef, categorize
    categorize(cfg, [CategoryDef("Fundraising", "LP updates, term sheets, raises")])
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

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
DEFS_META_KEY = "category_definitions"

SYSTEM = """You sort meeting/conversation notes into the user's categories.
Each category has a name and an optional description of what belongs in it.
Match the note to the category whose description best fits its content —
prefer the description over the bare name when they differ. You always
respond with a single JSON object choosing exactly one category name from
the allowed list, or "None" if none fit well. Do not invent categories."""

USER_TEMPLATE = """Allowed categories (pick ONE name exactly, or "None"):
{category_block}

Pick the ONE best category for this note based on the descriptions above.
Respond as JSON: {{"category": "<one of the allowed names or None>"}}.

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
Stay within the category's stated purpose when one is provided.

{CITE_RULES}"""


@dataclass(frozen=True)
class CategoryDef:
    name: str
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "description", (self.description or "").strip())


CategoryInput = Union[str, CategoryDef, dict]


def normalize_categories(categories: Sequence[CategoryInput]) -> list[CategoryDef]:
    """Accept names, CategoryDef, or {name, description} dicts."""
    out: list[CategoryDef] = []
    seen: set[str] = set()
    for raw in categories:
        if isinstance(raw, CategoryDef):
            d = raw
        elif isinstance(raw, dict):
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            d = CategoryDef(name, str(raw.get("description", "") or ""))
        else:
            name = str(raw).strip()
            if not name:
                continue
            # CLI form: "Name: description text"
            if ":" in name and not name.startswith("http"):
                left, right = name.split(":", 1)
                if left.strip() and right.strip():
                    d = CategoryDef(left.strip(), right.strip())
                else:
                    d = CategoryDef(name)
            else:
                d = CategoryDef(name)
        if not d.name or d.name in seen:
            continue
        seen.add(d.name)
        out.append(d)
    return out


def _category_block(defs: Sequence[CategoryDef]) -> str:
    lines = []
    for d in defs:
        if d.description:
            lines.append(f'- "{d.name}": {d.description}')
        else:
            lines.append(f'- "{d.name}" (no description — match on the name alone)')
    return "\n".join(lines)


def _schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"category": {"type": "string", "enum": names + [NONE_LABEL]}},
        "required": ["category"],
    }


def _classify(llm: LLM, note: NoteRecord, defs: Sequence[CategoryDef]) -> Optional[str]:
    names = [d.name for d in defs]
    user = USER_TEMPLATE.format(
        category_block=_category_block(defs),
        title=note.title,
        topics=", ".join(note.topics) or "(none)",
        summary=note.summary or "(no summary)",
    )
    try:
        data = llm.chat_json(SYSTEM, user, schema=_schema(names))
        choice = str(data.get("category", "")).strip()
    except LLMError:
        return None
    if choice == NONE_LABEL or choice not in names:
        return None
    return choice


def _sanitize_filename(name: str) -> str:
    return name.replace("/", "-").replace("\\", "-").strip() or "Category"


def category_note_path(cfg: Config, category: str) -> Path:
    return cfg.vault.insights_path / CATEGORIES_SUBDIR / f"{_sanitize_filename(category)}.md"


def save_category_definitions(cfg: Config, defs: Sequence[CategoryDef]) -> None:
    payload = [{"name": d.name, "description": d.description} for d in defs]
    with get_conn(cfg.db_path) as conn:
        set_meta(conn, DEFS_META_KEY, json.dumps(payload, ensure_ascii=False))


def load_category_definitions(cfg: Config) -> list[CategoryDef]:
    with get_conn(cfg.db_path) as conn:
        raw = get_meta(conn, DEFS_META_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return normalize_categories(data)


def _content_hash(notes: Iterable[NoteRecord], description: str = "") -> str:
    base = _hash_records(notes)
    if not description:
        return base
    return hashlib.sha256(f"{base}\n{description}".encode()).hexdigest()[:16]


def write_category_rollup(
    cfg: Config,
    llm: LLM,
    category: str,
    notes: list[NoteRecord],
    *,
    description: str = "",
    force: bool = False,
) -> dict:
    """Synthesize a citation-gated briefing into Categories/<Name>.md."""
    if not notes:
        return {"skipped": "empty"}

    path = category_note_path(cfg, category)
    digest_hash = _content_hash(notes, description)
    meta_key = f"category_hash:{category}"
    with get_conn(cfg.db_path) as conn:
        prev = get_meta(conn, meta_key)
    if prev == digest_hash and path.exists() and not force:
        return {"unchanged": len(notes)}

    by_id = {r.transcript_id: r for r in notes}
    corpus = "\n\n".join(
        _entry(r, with_open_items=True) for r in sorted(notes, key=lambda r: r.date)
    )
    purpose = f"Purpose: {description}\n" if description else ""
    user = (
        f"Category: {category}\n"
        f"{purpose}"
        f"{len(notes)} conversations in this category:\n\n{corpus}\n\n"
        "Write the category briefing."
    )
    data = llm.chat_json(CATEGORY_SYSTEM, user, schema=CATEGORY_SCHEMA)

    themes, d1 = verify_claims(data.get("themes", []), by_id)
    threads, d2 = verify_claims(data.get("open_threads", []), by_id)
    dropped = d1 + d2

    overview = str(data.get("overview", "")).strip()
    lines = [f"**{category}** · {len(notes)} conversation(s)", ""]
    if description:
        lines.append(f"_Scope: {description}_")
        lines.append("")
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


def _write_categories_hub(
    cfg: Config,
    assignments: dict[str, list[NoteRecord]],
    defs: Sequence[CategoryDef],
) -> None:
    cats_dir = cfg.vault.insights_path / CATEGORIES_SUBDIR
    cats_dir.mkdir(parents=True, exist_ok=True)
    desc_by_name = {d.name: d.description for d in defs}
    lines = [
        "# Categories",
        "",
        "_On-demand category index with scoped rollups. Notes stay organized by date._",
        "",
    ]
    for cat in sorted(assignments, key=lambda c: (-len(assignments[c]), c)):
        desc = desc_by_name.get(cat, "")
        if desc:
            lines.append(f"- [[{_sanitize_filename(cat)}]] ({len(assignments[cat])}) — {desc}")
        else:
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
        conn.execute("DELETE FROM meta WHERE key LIKE 'category_hash:%'")
        conn.execute("DELETE FROM meta WHERE key = ?", (DEFS_META_KEY,))
    if verbose:
        print(f"[reset] removed {removed} category note(s), cleared {cleared} assignment(s)")
    return {"mocs_removed": removed, "assignments_cleared": cleared}


def categorize(
    cfg: Optional[Config] = None,
    categories: Optional[Sequence[CategoryInput]] = None,
    llm: Optional[LLM] = None,
    verbose: bool = True,
    *,
    force_rollups: bool = True,
) -> dict:
    """Assign notes to categories, then synthesize a scoped rollup per category.

    Each category may include a description that steers matching. force_rollups
    defaults to True on categorize so a fresh sort always refreshes insights.
    """
    cfg = cfg or load_config()
    defs = normalize_categories(categories or [])
    if not defs:
        raise ValueError(
            "Provide at least one category, e.g. categorize(cfg, "
            "[CategoryDef('Hiring', 'Interviews and offer loops')])."
        )
    llm = llm or LLM(cfg)
    names = [d.name for d in defs]
    desc_by_name = {d.name: d.description for d in defs}

    with get_conn(cfg.db_path) as conn:
        notes = all_transcripts(conn)

    assignments: dict[str, list[NoteRecord]] = {n: [] for n in names}
    unassigned = 0
    for note in notes:
        cat = _classify(llm, note, defs)
        if cat:
            assignments[cat].append(note)
            if verbose:
                print(f"  {cat:<20} {note.title}")
        else:
            unassigned += 1

    _clear_old_mocs(cfg)
    rollups: dict[str, dict] = {}
    for cat, items in assignments.items():
        if not items:
            continue
        desc = desc_by_name.get(cat, "")
        if verbose:
            print(f"[categorize] synthesizing rollup for {cat} ({len(items)} notes)…")
        try:
            rollups[cat] = write_category_rollup(
                cfg, llm, cat, items, description=desc, force=force_rollups
            )
        except LLMError as e:
            if verbose:
                print(f"[categorize] rollup failed for {cat}: {e}; writing list-only stub")
            stub = [
                f"**{cat}** · {len(items)} conversation(s)",
                "",
            ]
            if desc:
                stub.extend([f"_Scope: {desc}_", ""])
            stub.extend(
                [
                    "_Scoped rollup failed; conversation list only._",
                    "",
                    "## Conversations",
                    *[
                        f"- [[{_stem(r)}]] · {r.date} ({r.source})"
                        for r in sorted(items, key=lambda r: r.date, reverse=True)
                    ],
                    _footer(0),
                ]
            )
            writer.write_managed(
                cfg, category_note_path(cfg, cat), "\n".join(stub), title=cat
            )
            rollups[cat] = {"error": str(e), "conversations": len(items)}

    _write_categories_hub(cfg, {c: v for c, v in assignments.items() if v}, defs)
    save_category_definitions(cfg, defs)

    with get_conn(cfg.db_path) as conn:
        clear_note_categories(conn)
        for cat, items in assignments.items():
            for n in items:
                set_note_category(conn, n.transcript_id, cat)

    summary = {
        "categories": {c: len(v) for c, v in assignments.items()},
        "definitions": [{"name": d.name, "description": d.description} for d in defs],
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
