"""Write insight notes into the Obsidian vault (the source of truth).

Layout inside the vault:
  <insights_folder>/                        e.g. "Transcript Insights"
    <insights_folder>.md                     top-level hub (map of content)
    <Category>.md                            per-category index (MOC)
    <Category>/<YYYY-MM-DD> <title>.md        one note per transcript
"""
from __future__ import annotations

from pathlib import Path

from slugify import slugify

from ..config import Config
from ..models import Insight, Transcript

_TRANSCRIPT_HEADING = "## Transcript"


def _safe_filename(title: str, when: str) -> str:
    slug = slugify(title, max_length=80) or "untitled"
    return f"{when} {slug}.md"


def _wikilink(name: str) -> str:
    name = name.strip().replace("[", "").replace("]", "")
    return f"[[{name}]]"


def _quote_block(text: str) -> str:
    """Render text inside a collapsible Obsidian callout so the indexer can read it."""
    lines = ["> [!note]- Full transcript"]
    for ln in text.splitlines() or [""]:
        lines.append(f"> {ln}")
    return "\n".join(lines)


def note_path_for(cfg: Config, transcript: Transcript, insight: Insight) -> Path:
    category = insight.category or "Uncategorized"
    fname = _safe_filename(transcript.title, transcript.date.isoformat())
    return cfg.vault.insights_path / category / fname


def render_note(transcript: Transcript, insight: Insight) -> str:
    people_links = [_wikilink(p) for p in insight.people]

    fm_lines = ["---"]
    fm_lines.append(f"source: {transcript.source}")
    fm_lines.append(f"date: {transcript.date.isoformat()}")
    fm_lines.append(f'category: "{insight.category}"')
    fm_lines.append(f"transcript_id: {transcript.id}")
    fm_lines.append("people:")
    for p in people_links:
        fm_lines.append(f'  - "{p}"')
    fm_lines.append("topics:")
    for t in insight.topics:
        fm_lines.append(f'  - "{t}"')
    fm_lines.append("action_items:")
    for a in insight.action_items:
        fm_lines.append(f'  - "{a.replace(chr(34), chr(39))}"')
    if insight.sentiment:
        fm_lines.append(f"sentiment: {insight.sentiment}")
    fm_lines.append("---")

    body = [f"# {transcript.title}", ""]
    if people_links:
        body.append("**People:** " + ", ".join(people_links))
    body.append(f"**Source:** {transcript.source}  ·  **Date:** {transcript.date.isoformat()}")
    body.append("")
    body.append("## Summary")
    body.append(insight.summary or "_No summary._")
    body.append("")
    body.append("## Key Points")
    body.extend([f"- {kp}" for kp in insight.key_points] or ["- _None._"])
    body.append("")
    body.append("## Action Items")
    body.extend([f"- [ ] {a}" for a in insight.action_items] or ["- _None._"])
    body.append("")
    if insight.topics:
        body.append("## Topics")
        body.append(" ".join(f"#{slugify(t)}" for t in insight.topics))
        body.append("")
    body.append(_TRANSCRIPT_HEADING)
    body.append(_quote_block(transcript.text))
    body.append("")

    return "\n".join(fm_lines) + "\n\n" + "\n".join(body)


def write_note(cfg: Config, transcript: Transcript, insight: Insight) -> Path:
    path = note_path_for(cfg, transcript, insight)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(transcript, insight), encoding="utf-8")
    return path


def rebuild_indexes(cfg: Config) -> None:
    """Regenerate the hub note + per-category MOC notes from the notes on disk."""
    root = cfg.vault.insights_path
    if not root.exists():
        return
    folder = cfg.vault.insights_folder

    categories: dict[str, list[Path]] = {}
    for cat_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        notes = sorted(cat_dir.glob("*.md"))
        if notes:
            categories[cat_dir.name] = notes

    # Per-category MOC notes.
    for cat, notes in categories.items():
        lines = [f"# {cat}", "", f"_{len(notes)} conversation(s)._", ""]
        for n in notes:
            lines.append(f"- [[{n.stem}]]")
        (root / f"{cat}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Top-level hub note.
    hub = [f"# {folder}", "", "Auto-generated index of transcript insights.", ""]
    total = sum(len(v) for v in categories.values())
    hub.append(f"_{total} conversation(s) across {len(categories)} categories._")
    hub.append("")
    for cat, notes in sorted(categories.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        hub.append(f"- [[{cat}]] ({len(notes)})")
    (root / f"{folder}.md").write_text("\n".join(hub) + "\n", encoding="utf-8")
