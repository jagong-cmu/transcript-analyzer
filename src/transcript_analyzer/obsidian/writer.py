"""Write insight notes into the Obsidian vault (the source of truth).

Notes are organized FLAT by recording date (date-prefixed filenames), NOT by
category. Categories are created on demand via the `categorize` command, which
writes non-destructive index (MOC) notes under `<insights_folder>/Categories/`.

  <insights_folder>/
    <insights_folder>.md                 hub, notes grouped by month
    <YYYY-MM-DD> <title>.md              one note per transcript (flat)
    Categories/<Category>.md             (created on demand by `categorize`)
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from slugify import slugify

from ..config import Config
from ..models import Insight, Transcript

CATEGORIES_SUBDIR = "Categories"
ATTACHMENTS_SUBDIR = "Attachments"

# The only vault namespaces synthesis may write into (namespace isolation:
# generated notes never land next to — or over — transcript notes).
SYNTH_SUBDIRS = ("Digests", "People", "Studies", "Prep", "Categories")

SYNTH_BEGIN = "<!-- synth:begin — generated; edits inside this block are overwritten -->"
SYNTH_END = "<!-- synth:end -->"


def attachments_dir(cfg: Config) -> Path:
    return cfg.vault.insights_path / ATTACHMENTS_SUBDIR


def audio_path_for(cfg: Config, note_path: Path) -> Path:
    """Where the audio for a given note note lives (matches the note's stem)."""
    return attachments_dir(cfg) / f"{note_path.stem}.mp3"


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


def _existing_transcript_id(path: Path) -> str:
    """Cheap read of the transcript_id from a note's frontmatter, if present."""
    try:
        fences = 0
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip() == "---":
                fences += 1
                if fences >= 2:
                    break
                continue
            if ln.startswith("transcript_id:"):
                return ln.split(":", 1)[1].strip()
    except OSError:
        return ""
    return ""


def note_path_for(cfg: Config, transcript: Transcript, insight: Insight) -> Path:
    root = cfg.vault.insights_path
    base = root / _safe_filename(transcript.title, transcript.date.isoformat())
    # Guarantee uniqueness: if a DIFFERENT transcript already owns this filename
    # (two titles that slugify identically on the same date), append a short id.
    if base.exists() and _existing_transcript_id(base) not in ("", transcript.id):
        stem = base.stem
        return root / f"{stem} ({transcript.id[:6]}).md"
    return base


def render_note(transcript: Transcript, insight: Insight, audio_name: str | None = None) -> str:
    people_links = [_wikilink(p) for p in insight.people]

    fm_lines = ["---"]
    fm_lines.append(f"source: {transcript.source}")
    fm_lines.append(f"date: {transcript.date.isoformat()}")
    fm_lines.append(f"transcript_id: {transcript.id}")
    fm_lines.append("people:")
    for p in people_links:
        fm_lines.append(f'  - "{p}"')
    if transcript.attendees:
        # The email is the stable person-identity key — persist it.
        fm_lines.append("attendees:")
        for a in transcript.attendees:
            fm_lines.append(f'  - name: "{a.name.replace(chr(34), chr(39))}"')
            fm_lines.append(f'    email: "{a.email}"')
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
    if audio_name:
        body.append("## Recording")
        body.append(f"![[{audio_name}]]")
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
    body.append("## Transcript")
    body.append(_quote_block(transcript.text))
    body.append("")

    return "\n".join(fm_lines) + "\n\n" + "\n".join(body)


def write_note(
    cfg: Config, transcript: Transcript, insight: Insight, audio_name: str | None = None
) -> Path:
    path = note_path_for(cfg, transcript, insight)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(transcript, insight, audio_name=audio_name), encoding="utf-8")
    return path


def write_managed(cfg: Config, path: Path, generated: str, *, title: str = "") -> Path:
    """Write generated content into a synthesis note, preserving user edits.

    Only the region between the synth markers is ever rewritten; anything the
    user adds outside it survives regeneration (R9). Refuses to write outside
    the synthesis namespaces (Digests/, People/, Studies/, Prep/).
    """
    root = cfg.vault.insights_path.resolve()
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"synthesis write outside the insights folder: {path}")
    if not rel.parts or rel.parts[0] not in SYNTH_SUBDIRS:
        raise ValueError(
            f"synthesis may only write under {SYNTH_SUBDIRS}, got: {rel}"
        )

    region = f"{SYNTH_BEGIN}\n{generated.strip()}\n{SYNTH_END}"
    if resolved.exists():
        text = resolved.read_text(encoding="utf-8")
        begin = text.find(SYNTH_BEGIN)
        end = text.find(SYNTH_END)
        if begin != -1 and end != -1 and end > begin:
            new_text = text[:begin] + region + text[end + len(SYNTH_END):]
        else:
            # User removed the markers — append a fresh region, never clobber.
            new_text = text.rstrip() + "\n\n" + region + "\n"
    else:
        head = ["---", "synth: true", "---", ""]
        if title:
            head.append(f"# {title}")
            head.append("")
        new_text = "\n".join(head) + region + "\n"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(new_text, encoding="utf-8")
    return resolved


def rebuild_indexes(cfg: Config) -> None:
    """Regenerate the hub note listing all transcript notes grouped by month."""
    root = cfg.vault.insights_path
    if not root.exists():
        return
    folder = cfg.vault.insights_folder

    # Flat transcript notes live directly under root (skip the hub + Categories/).
    notes = [p for p in root.glob("*.md") if p.stem != folder]
    by_month: dict[str, list[Path]] = defaultdict(list)
    for n in notes:
        # filename starts with YYYY-MM-DD
        month = n.stem[:7] if len(n.stem) >= 7 and n.stem[4] == "-" else "undated"
        by_month[month].append(n)

    hub = [f"# {folder}", "", f"_{len(notes)} conversation(s), organized by date._", ""]
    for month in sorted(by_month, reverse=True):
        hub.append(f"## {month}")
        for n in sorted(by_month[month], reverse=True):
            hub.append(f"- [[{n.stem}]]")
        hub.append("")
    (root / f"{folder}.md").write_text("\n".join(hub) + "\n", encoding="utf-8")
