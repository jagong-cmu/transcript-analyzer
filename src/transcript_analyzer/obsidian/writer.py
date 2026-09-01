"""Write insight notes into the Obsidian vault (the source of truth).

Notes are organized FLAT by recording date (date-prefixed filenames), NOT by
category. Categories are created on demand via the `categorize` command, which
writes non-destructive index (MOC) notes under `<insights_folder>/Categories/`.

  <insights_folder>/
    <insights_folder>.md                 hub, notes grouped by month
    <YYYY-MM-DD> <headline-slug>.md      one note per transcript (flat)
    Categories/<Category>.md             (created on demand by `categorize`)

Each note's H1 / indexed title is ``{headline}, July 26th, 2026``.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

from slugify import slugify

from ..config import Config
from ..models import Insight, Transcript
from ..titles import clean_headline, compose_display_title, format_long_date

_log = logging.getLogger(__name__)

CATEGORIES_SUBDIR = "Categories"
ATTACHMENTS_SUBDIR = "Attachments"

# The only vault namespaces synthesis may write into (namespace isolation:
# generated notes never land next to — or over — transcript notes).
SYNTH_SUBDIRS = ("Digests", "People", "Studies", "Prep", "Categories")

SYNTH_BEGIN = "<!-- synth:begin — generated; edits inside this block are overwritten -->"
SYNTH_END = "<!-- synth:end -->"

_ATX_HEADING_RE = re.compile(r"(#{1,6})(?:\s|$)")


def heading_level(line: str) -> int:
    """How deep a heading this line is, or 0 when it is not a heading at all.

    The one definition of "is this a heading". The reader finds its sections
    after `line.strip()`, which drops every kind of Unicode whitespace, so this
    asks the same way: any writer that decided separately what to escape would
    let a line it considered ordinary text open a section on the way back in.
    A line has to be one to six '#' followed by whitespace or nothing else to
    qualify, so a tag or a rank ('#hiring', '#1 priority') is not a heading.
    """
    m = _ATX_HEADING_RE.match(line.strip())
    return len(m.group(1)) if m else 0


def opens_section(line: str, max_level: int = 6) -> bool:
    """Whether the line opens a section no deeper than `max_level`.

    `max_level` is what lets one definition serve both jobs. The writer escapes
    with the default, because over-escaping the body is harmless and closes the
    injection hole. TERMINATION is level-aware: a '## Summary' section ends at
    the next heading of its own level or shallower, while a '### …' the vault
    owner wrote is nested INSIDE it and must stay in the indexed section — the
    note is the source of truth and hand edits are respected.
    """
    level = heading_level(line)
    return 0 < level <= max_level


def attachments_dir(cfg: Config) -> Path:
    return cfg.vault.insights_path / ATTACHMENTS_SUBDIR


def audio_for_stem(insights_root: Path, stem: str) -> Path:
    """The one definition of where the recording keyed to a note stem lives."""
    return insights_root / ATTACHMENTS_SUBDIR / f"{stem}.mp3"


def audio_path_for(cfg: Config, note_path: Path) -> Path:
    """Where the audio for a given note lives (matches the note's stem)."""
    return audio_for_stem(cfg.vault.insights_path, note_path.stem)


def note_for_audio(audio_path: Path) -> Path:
    """The note whose stem claims this recording — the inverse of `audio_for_stem`."""
    return audio_path.parent.parent / f"{audio_path.stem}.md"


def audio_partial(audio_path: Path) -> Path:
    """Where an in-flight download of that recording streams to.

    One definition, two readers: the downloader writes here, and
    `claimable_stem` looks for it, so a stem being streamed to counts as taken
    for the whole download instead of looking free until the last moment.
    """
    return audio_path.with_suffix(audio_path.suffix + ".part")


def move_audio_with_note(
    cfg: Config, old_note: Path, new_note: Path, transcript_id: str
) -> Path | None:
    """Make a note's recording follow it when the note's filename changes.

    Audio is keyed on the note stem, and the stem is derived from the LLM
    headline — so any reprocessing that re-words the headline renames the note.
    Left behind, the old mp3 is orphaned in Attachments/ AND the new stem does
    not exist, so a re-download is paid for a recording the vault already has.
    Returns the new audio path if something was moved.

    An mp3 carries no frontmatter, so the note at its stem is the only thing
    that can claim it — and that has to hold at BOTH ends of the move. The
    recording is ours to take only when the note at the source stem is ours,
    and a recording already sitting at the destination is ours to replace only
    when the note there is ours. Otherwise — an attachment the vault owner
    still embeds from a note they renamed in Obsidian, or a stem another note
    took while this transcript's recording was still downloading — nothing is
    moved or unlinked; the move is skipped and the file named.
    """
    old_audio = audio_path_for(cfg, old_note)
    new_audio = audio_path_for(cfg, new_note)
    if old_audio.resolve() == new_audio.resolve() or not old_audio.exists():
        return None
    if not owns_note(old_note, transcript_id):
        _log.warning(
            "leaving %s where it is: no note at that stem proves the recording "
            "is this transcript's to move (orphan, clean up by hand)",
            old_audio,
        )
        return None
    if new_audio.exists() and not owns_note(new_note, transcript_id):
        _log.warning(
            "leaving %s in place: no note at that stem proves the recording is "
            "this transcript's, so %s stays where it is (orphan, clean up by hand)",
            new_audio, old_audio,
        )
        return None
    new_audio.parent.mkdir(parents=True, exist_ok=True)
    if new_audio.exists():
        new_audio.unlink()
    old_audio.rename(new_audio)
    return new_audio


def _safe_filename(headline: str, when: str) -> str:
    slug = slugify(headline, max_length=80) or "untitled"
    return f"{when} {slug}.md"


def _wikilink(name: str) -> str:
    name = _one_line(name).replace("[", "").replace("]", "")
    return f"[[{name}]]"


_YAML_SHORT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _yaml_str(value: str) -> str:
    """A YAML double-quoted scalar that round-trips arbitrary text exactly.

    Inside double quotes YAML reads ``\\`` as an escape, folds real line breaks
    into spaces, and rejects most control characters outright — so an un-escaped
    one from an LLM headline, an action item or a transcript either corrupts the
    value or makes the whole note unparseable, and the indexer drops notes whose
    frontmatter won't load. Every such character is emitted as an escape.
    """
    out: list[str] = []
    for ch in str(value):
        short = _YAML_SHORT_ESCAPES.get(ch)
        if short is not None:
            out.append(short)
            continue
        cp = ord(ch)
        # C0/C1 controls plus the YAML 1.1 line separators, which fold like \n.
        if cp < 0x20 or 0x7F <= cp <= 0x9F or ch in ("\u2028", "\u2029"):
            out.append(f"\\x{cp:02x}" if cp < 0x100 else f"\\u{cp:04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _one_line(value: str) -> str:
    """Collapse a value onto a single line so it survives the note body.

    The body is line-oriented and the indexer wins from it (a body checkbox
    list replaces the frontmatter list outright), so a key point or action item
    carrying an interior newline would be silently truncated at the first line
    — and a continuation line starting with '## ' would even fake a heading and
    cut the transcript out of the index. Whitespace and control characters are
    folded to single spaces; no text is dropped.
    """
    text = "".join(
        " " if (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in ("\u2028", "\u2029"))
        else ch
        for ch in str(value)
    )
    return " ".join(text.split())


def _body_text(value: str) -> str:
    """Free text safe to place in the note body, keeping its line structure.

    An LLM value whose own line would open a section opens one the writer never
    opened: the real transcript stops being what the index, the dashboard and
    RAG read back. Escaped exactly when `opens_section` says so — the same
    predicate the reader uses — and left byte for byte otherwise, because the
    escape itself round-trips into the index.
    """
    out: list[str] = []
    for ln in str(value).splitlines() or [""]:
        if opens_section(ln):
            indent = len(ln) - len(ln.lstrip())
            ln = ln[:indent] + "\\" + ln[indent:]
        out.append(ln)
    return "\n".join(out)


def _quote_block(text: str) -> str:
    """Render text inside a collapsible Obsidian callout so the indexer can read it."""
    lines = ["> [!note]- Full transcript"]
    for ln in text.splitlines() or [""]:
        lines.append(f"> {ln}")
    return "\n".join(lines)


def _existing_transcript_id(path: Path) -> str | None:
    """Cheap read of the transcript_id from a note's frontmatter.

    None means UNKNOWN — no `transcript_id:` line, or the file could not be
    read at all — and is deliberately distinct from an id that is present but
    empty, because `owns_note` must never treat unknown as a match.
    """
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
    except (OSError, UnicodeDecodeError):
        return None
    return None


def owns_note(path: Path, transcript_id: str) -> bool:
    """Whether an existing file is PROVABLY this transcript's own note.

    Ownership is proven, never assumed. A file whose id we cannot read back —
    a hand-written note with no frontmatter, an unreadable or binary file — is
    the vault owner's, not ours, and the vault has no backup: writing or
    renaming over it destroys work nothing can recover.
    """
    if not transcript_id:
        return False
    return _existing_transcript_id(path) == transcript_id


def claimable_stem(
    note_path: Path, transcript_id: str, *, in_flight_download: bool = False
) -> bool:
    """Whether EVERY vault file keyed on this stem is free or provably ours.

    A stem names the note, its recording in Attachments/, and any download
    still streaming towards that recording — and the note is the only one that
    can carry proof, so the set is claimable only when the note there is ours,
    or when none of them exist. That is what keeps a still-embedded mp3 whose
    note the owner renamed away from being unlinked by, or played back inside,
    somebody else's note.

    `in_flight_download` is for the downloader itself, which is holding the
    partial it is about to become and must not read its own file as someone
    else's claim. Every other caller leaves it False.
    """
    if owns_note(note_path, transcript_id):
        return True
    if note_path.exists():
        return False
    audio = audio_for_stem(note_path.parent, note_path.stem)
    if audio.exists():
        return False
    return in_flight_download or not audio_partial(audio).exists()


def claim_note_path(base: Path, transcript_id: str) -> Path:
    """The path this transcript may safely occupy, preferring `base`.

    The one definition of "where may this transcript's note go", shared by the
    sync path (`note_path_for`) and the retitle migration, so the two cannot
    drift. Free or already ours is `base`; anything else — another
    transcript's note on the same date, a hand-written note, a file we cannot
    read, an attachment stem someone else still owns — falls through to
    `<stem> (<id6>).md` and keeps going rather than landing on files that are
    not ours.
    """
    if claimable_stem(base, transcript_id):
        return base
    short = transcript_id[:6] or "note"
    candidate = base.with_name(f"{base.stem} ({short}){base.suffix}")
    n = 2
    while not claimable_stem(candidate, transcript_id):
        candidate = base.with_name(f"{base.stem} ({short}-{n}){base.suffix}")
        n += 1
    _log.warning(
        "%s is not this transcript's to write; using %s instead",
        base.name, candidate.name,
    )
    return candidate


def note_headline(transcript: Transcript, insight: Insight) -> str:
    return clean_headline(insight.headline) or clean_headline(transcript.title) or "Untitled conversation"


def note_path_for(cfg: Config, transcript: Transcript, insight: Insight) -> Path:
    root = cfg.vault.insights_path
    headline = note_headline(transcript, insight)
    base = root / _safe_filename(headline, transcript.date.isoformat())
    # Guarantee uniqueness AND ownership: a filename we cannot prove is this
    # transcript's own (another transcript whose title slugifies the same on
    # the same date, or a note the vault owner wrote) gets a short-id suffix.
    return claim_note_path(base, transcript.id)


def render_note(transcript: Transcript, insight: Insight, audio_name: str | None = None) -> str:
    people_links = [_wikilink(p) for p in insight.people]
    headline = note_headline(transcript, insight)
    display_title = compose_display_title(headline, transcript.date)
    # The body is line-oriented, and the indexer reads its list items in
    # preference to the frontmatter, so every item has to fit on one line.
    action_items = [_one_line(a) for a in insight.action_items]
    key_points = [_one_line(kp) for kp in insight.key_points]

    fm_lines = ["---"]
    fm_lines.append(f"source: {transcript.source}")
    fm_lines.append(f"date: {transcript.date.isoformat()}")
    fm_lines.append(f"transcript_id: {transcript.id}")
    fm_lines.append(f"headline: {_yaml_str(headline)}")
    fm_lines.append("people:")
    for p in people_links:
        fm_lines.append(f"  - {_yaml_str(p)}")
    if transcript.attendees:
        # The email is the stable person-identity key — persist it.
        fm_lines.append("attendees:")
        for a in transcript.attendees:
            fm_lines.append(f"  - name: {_yaml_str(a.name)}")
            fm_lines.append(f"    email: {_yaml_str(a.email)}")
    fm_lines.append("topics:")
    for t in insight.topics:
        fm_lines.append(f"  - {_yaml_str(t)}")
    fm_lines.append("action_items:")
    for a in insight.action_items:
        fm_lines.append(f"  - {_yaml_str(a)}")
    if insight.sentiment:
        fm_lines.append(f"sentiment: {insight.sentiment}")
    fm_lines.append("---")

    body = [f"# {display_title}", ""]
    if people_links:
        body.append("**People:** " + ", ".join(people_links))
    body.append(f"**Source:** {transcript.source}  ·  **Date:** {format_long_date(transcript.date)}")
    body.append("")
    if audio_name:
        body.append("## Recording")
        body.append(f"![[{audio_name}]]")
        body.append("")
    body.append("## Summary")
    body.append(_body_text(insight.summary) or "_No summary._")
    body.append("")
    body.append("## Key Points")
    body.extend([f"- {kp}" for kp in key_points] or ["- _None._"])
    body.append("")
    body.append("## Action Items")
    body.extend([f"- [ ] {a}" for a in action_items] or ["- _None._"])
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
    cfg: Config,
    transcript: Transcript,
    insight: Insight,
    audio_name: str | None = None,
    *,
    path: Path | None = None,
) -> Path:
    """Write the note, at `path` when the caller already claimed one.

    `claim_note_path` reads the filesystem, so evaluating it a second time here
    could answer differently from the one the caller downloaded the audio
    against — and the embed baked into the body would name a stem the note no
    longer sits on. One claim, threaded through both.

    If something else did take that path while the recording streamed, the
    claim is redone and the recording NEVER follows: the stem belongs to
    whoever took it, so its mp3 is theirs and this note is written with no
    embed at all rather than one naming a file it does not own. Either way the
    name in the body and the file on disk cannot disagree, and nothing unowned
    is written over.
    """
    if path is None:
        path = note_path_for(cfg, transcript, insight)
    elif path.exists() and not owns_note(path, transcript.id):
        _log.warning(
            "%s was taken while the recording downloaded; re-claiming, and "
            "leaving anything at that stem to the note that owns it",
            path,
        )
        audio_name = None
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
