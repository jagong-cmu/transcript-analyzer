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
import time
from collections import defaultdict
from itertools import islice
from pathlib import Path
from typing import Iterator

import frontmatter
from slugify import slugify

from ..config import Config
from ..models import AsrRepair, Insight, Transcript
from ..titles import clean_headline, compose_display_title, format_long_date

_log = logging.getLogger(__name__)

# How long an in-flight download marker keeps its claim on a stem. Well past
# the downloader's own 300s timeout, so a live stream is never mistaken for an
# abandoned one — and a marker a crash left behind stops blocking eventually.
PARTIAL_DOWNLOAD_TTL_SECONDS = 20 * 60

CATEGORIES_SUBDIR = "Categories"
ATTACHMENTS_SUBDIR = "Attachments"
# Lecture study notes and their rendered PDF. Adding a namespace here without
# adding it to indexer.EXCLUDED_SUBDIRS breaks the feedback-loop guard and the
# indexer starts ingesting synthesis output as if it were a transcript.
STUDY_SUBDIR = "Study Notes"

# The only vault namespaces synthesis may write into (namespace isolation:
# generated notes never land next to — or over — transcript notes).
SYNTH_SUBDIRS = ("Digests", "People", "Studies", "Prep", "Categories", STUDY_SUBDIR)

SYNTH_BEGIN = "<!-- synth:begin — generated; edits inside this block are overwritten -->"
SYNTH_END = "<!-- synth:end -->"

# The transcript note's own managed region. A separate pair from the synthesis
# one because the rule is different: a transcript note is regenerated from the
# recording, but a ticked checkbox inside the region is a designed hand edit
# and is carried across (see `write_note`). Everything below the end marker is
# the vault owner's and is preserved untouched.
NOTE_BEGIN = (
    "<!-- transcript-analyzer:begin — regenerated from the recording; "
    "write below the end marker to keep your own notes -->"
)
NOTE_END = "<!-- transcript-analyzer:end -->"

_ATX_HEADING_RE = re.compile(r"(#{1,6})(?:\s|$)")
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")


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

    `max_level` is what lets one definition serve both jobs — ESCAPING what
    would break a section open, and finding where a section ENDS — at the same
    bound. A '## Summary' section ends at the next heading of its own level or
    shallower, while a '### …' is nested INSIDE it: that is true of a heading
    the vault owner wrote (their commitments stay indexed) and of one the model
    wrote inside a long summary (it stays real structure instead of picking up
    a backslash the reader can see). Escaping past that bound is not harmless
    any more, so `_body_text` passes the section it is writing into.
    """
    level = heading_level(line)
    return 0 < level <= max_level


def is_section_start(line: str, heading: str) -> bool:
    """Whether `line` opens the named section ('## transcript', lowercase).

    Lives beside `opens_section` because the writer is what emits these
    headings; the indexer and the timestamp backfill call in rather than
    re-deriving the test (see AGENTS.md: add call sites, not definitions).
    """
    return opens_section(line) and line.strip().lower() == heading


def is_section_end(line: str, heading: str) -> bool:
    """Whether `line` closes the section that `heading` opened.

    The same predicate as the start, bounded to the section's own level: a
    sibling or shallower heading ends it, a deeper one the vault owner wrote
    ('### Context' under '## Summary') is nested inside and stays indexed.
    """
    return opens_section(line, max_level=heading_level(heading))


def transcript_bounds(lines: list[str]) -> tuple[int, int] | None:
    """(heading index, last line of the callout) for the '## Transcript' section.

    THE one definition of the transcript section's grammar: the heading, an
    optional run of blank lines, then the contiguous run of '>' lines that is
    the callout — exactly what `_quote_block` emits. It ends there; a blank
    line, a callout the owner appended, or any later section is theirs. When
    no callout follows the heading, the section is the heading alone, so
    nothing of theirs is ever spliced away. A transcript's own blank line is
    written as '> ', which is what keeps it inside the run.

    Three readers depend on this agreeing exactly — the indexer's transcript
    extraction, the timestamp backfill's rewrite, and `write_note`'s recovery
    of a hand-written tail from a note that predates the managed markers.
    Disagreement silently duplicates a transcript or splices away the owner's
    own text.
    """
    start = next(
        (i for i, ln in enumerate(lines) if is_section_start(ln, "## transcript")),
        None,
    )
    if start is None:
        return None
    i = start + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not lines[i].startswith(">"):
        return (start, start)
    while i + 1 < len(lines) and lines[i + 1].startswith(">"):
        i += 1
    return (start, i)


def parse_action_items(body: str) -> list[tuple[str, bool]]:
    """(text, done) pairs from the '## Action Items' checkbox list.

    The note is the source of truth: ticking a box in Obsidian closes the
    commitment, and a commitment the owner filed under their own '### …'
    sub-heading is still one of theirs. Shared by the indexer (which turns
    these into the commitment tracker's rows) and by `write_note` (which
    carries ticked boxes across a regeneration).
    """
    out: list[tuple[str, bool]] = []
    in_section = False
    for ln in body.splitlines():
        if is_section_start(ln, "## action items"):
            in_section = True
            continue
        if in_section:
            if is_section_end(ln, "## action items"):
                break
            m = _CHECKBOX_RE.match(ln)
            if m:
                out.append((m.group(2), m.group(1).lower() == "x"))
    return out


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


def study_stem(note_stem: str) -> str:
    """The stem the study notes and their PDF share, derived from the note's.

    Deliberately NOT the note's own stem: two vault files with the same name
    make every `[[wikilink]]` to it ambiguous, and Obsidian would start
    resolving the hub's links to the study note instead of the transcript.
    """
    return f"{note_stem} (study notes)"


def study_note_for(insights_root: Path, note_stem: str) -> Path:
    """The one definition of where a note's study notes live."""
    return insights_root / STUDY_SUBDIR / f"{study_stem(note_stem)}.md"


def study_note_path_for(cfg: Config, note_path: Path) -> Path:
    return study_note_for(cfg.vault.insights_path, note_path.stem)


def study_pdf_for(study_md: Path) -> Path:
    """The PDF keyed to a study note — the inverse pairing of note and audio.

    A PDF carries no frontmatter we read, so the study note sitting at its
    stem is the only thing that can prove whose it is. Same shape as
    `audio_for_stem` / `note_for_audio`, same reason.
    """
    return study_md.with_suffix(".pdf")


def audio_partial(audio_path: Path) -> Path:
    """Where an in-flight download of that recording streams to.

    One definition, two readers: the downloader writes here, and
    `claimable_stem` looks for it, so a stem being streamed to counts as taken
    for the whole download instead of looking free until the last moment.
    """
    return audio_path.with_suffix(audio_path.suffix + ".part")


def partial_claims_stem(audio_path: Path) -> bool:
    """Whether an in-flight download still holds a claim on this stem.

    The marker carries no owner, and a process killed mid-stream (a laptop
    losing power) leaves one behind that nothing cleans up — so the claim is
    bounded by age rather than trusted forever: past
    `PARTIAL_DOWNLOAD_TTL_SECONDS` it is treated as abandoned and the stem is
    claimable again. A live download is never mistaken for an abandoned one,
    the window being far longer than the download's own timeout.
    """
    try:
        age = time.time() - audio_partial(audio_path).stat().st_mtime
    except OSError:
        return False
    return age < PARTIAL_DOWNLOAD_TTL_SECONDS


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


def _body_text(value: str, within: str = "") -> str:
    """Free text safe to place in the note body, keeping its line structure.

    An LLM value whose own line would CLOSE the section it sits in opens one
    the writer never opened: the real transcript stops being what the index,
    the dashboard and RAG read back. `within` is the heading of the section
    this text goes under, and the escape is bounded to exactly the levels that
    would end it — the same bound `indexer.is_section_end` reads it back with,
    so the two cannot disagree.

    That bound matters now that a detailed summary is long enough to have its
    own structure: a '###' nested under '## Summary' is INSIDE the section, so
    it survives the round trip and must not be escaped — a backslash the
    reader can see is a defect, not a safety measure. Without `within` the
    old, maximal bound applies: every heading shape is escaped.
    """
    max_level = heading_level(within) if within else 6
    out: list[str] = []
    for ln in str(value).splitlines() or [""]:
        if opens_section(ln, max_level=max_level):
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

    `in_flight_download` is for a downloader about to replace the destination
    with the partial it has just finished, which would otherwise read that
    partial as a claim against itself. Every other caller leaves it False.
    """
    if owns_note(note_path, transcript_id):
        return True
    if note_path.exists():
        return False
    audio = audio_for_stem(note_path.parent, note_path.stem)
    if audio.exists():
        return False
    return in_flight_download or not partial_claims_stem(audio)


def claimable_study_stem(study_md: Path, transcript_id: str) -> bool:
    """Whether the study-notes stem — the .md AND its .pdf — is free or ours.

    The same proof as `claimable_stem`, over the pair of files a study stem
    names. The markdown study note carries `transcript_id` in its frontmatter,
    so unlike audio BOTH ends of a study move can be proven directly; the PDF
    is claimed through the note at its stem, and a PDF with no note beside it
    is somebody else's file to leave alone.
    """
    if owns_note(study_md, transcript_id):
        return True
    if study_md.exists():
        return False
    return not study_pdf_for(study_md).exists()


def _claim_ladder(base: Path, transcript_id: str) -> Iterator[Path]:
    """Every path this transcript might occupy, in the order it would take them.

    THE suffix ladder, shared by every namespace that claims a stem, so the
    "not ours -> `<stem> (<id6>)`" fallback cannot drift between them — and
    shared by the READERS, so a stem that landed further up the ladder is not
    invisible to the dashboard or left behind by a rename. Infinite: the
    claimer stops at the first free rung, a reader bounds its own walk.
    """
    yield base
    short = transcript_id[:6] or "note"
    yield base.with_name(f"{base.stem} ({short}){base.suffix}")
    n = 2
    while True:
        yield base.with_name(f"{base.stem} ({short}-{n}){base.suffix}")
        n += 1


# How far a reader walks the ladder looking for a stem already taken. The
# claimer only advances past a rung something else occupies, so a note this
# deep means dozens of colliding stems on one day; beyond that, not found.
LADDER_SCAN_DEPTH = 32


def _claim_path(base: Path, transcript_id: str, claimable) -> Path:
    """The path this transcript may safely occupy, preferring `base`.

    `claimable` is the namespace's own proof of what a stem covers (the note
    plus its recording; or the study note plus its PDF).
    """
    for candidate in _claim_ladder(base, transcript_id):
        if not claimable(candidate, transcript_id):
            continue
        if candidate != base:
            _log.warning(
                "%s is not this transcript's to write; using %s instead",
                base.name, candidate.name,
            )
        return candidate
    raise AssertionError("the claim ladder is infinite")  # pragma: no cover


def claim_note_path(base: Path, transcript_id: str) -> Path:
    """The path this transcript's NOTE may safely occupy, preferring `base`.

    The one definition of "where may this transcript's note go", shared by the
    sync path (`note_path_for`) and the retitle migration, so the two cannot
    drift. Free or already ours is `base`; anything else — another
    transcript's note on the same date, a hand-written note, a file we cannot
    read, an attachment stem someone else still owns — falls through to
    `<stem> (<id6>).md` and keeps going rather than landing on files that are
    not ours.
    """
    return _claim_path(base, transcript_id, claimable_stem)


def claim_study_path(base: Path, transcript_id: str) -> Path:
    """The same rule for the study-notes stem (the .md and its .pdf)."""
    return _claim_path(base, transcript_id, claimable_study_stem)


def resolve_study_note(base: Path, transcript_id: str) -> Path | None:
    """The study note this transcript PROVABLY owns on `base`'s ladder, or None.

    The inverse of `claim_study_path`, over the same rungs: a stem the claimer
    pushed to `<stem> (<id6>).md` because someone else held the base is still
    this transcript's, and a reader that only ever looked at the base would
    treat those study notes as missing — invisible to the dashboard, left
    behind by a rename.

    Ownership is not weakened to find one: `owns_note` is the whole test, so
    an absent note, a note with no id, or somebody else's file all answer
    None, and nothing is written, moved or served against them.
    """
    for candidate in islice(_claim_ladder(base, transcript_id), LADDER_SCAN_DEPTH):
        if owns_note(candidate, transcript_id):
            return candidate
    return None


def move_study_with_note(
    cfg: Config, old_note: Path, new_note: Path, transcript_id: str
) -> Path | None:
    """Make a note's study notes and PDF follow it when its filename changes.

    Same both-ends proof as `move_audio_with_note`, and the same failure
    posture: a stem that cannot be proven ours is left alone and named in a
    warning. The vault has no backup, so an orphaned PDF to delete by hand
    beats a move that overwrites somebody else's file. Returns the new study
    note path when something moved.

    The SOURCE is resolved through the claim ladder, because that is where the
    write may have put it: study notes that landed at `<stem> (<id6>)` are
    still this transcript's and must follow it. The DESTINATION stays the
    plain stem — a rename takes the name it asked for or none at all.
    """
    old_base = study_note_path_for(cfg, old_note)
    new_md = study_note_path_for(cfg, new_note)
    if old_base.resolve() == new_md.resolve():
        return None
    old_md = resolve_study_note(old_base, transcript_id)
    if old_md is None:
        if old_base.exists():
            _log.warning(
                "leaving %s where it is: its transcript_id does not prove the study "
                "notes are this transcript's to move (orphan, clean up by hand)",
                old_base,
            )
        return None
    if old_md.resolve() == new_md.resolve():
        return None
    if not claimable_study_stem(new_md, transcript_id):
        _log.warning(
            "leaving %s in place: %s is not this transcript's stem to take "
            "(orphan, clean up by hand)",
            old_md, new_md,
        )
        return None
    new_md.parent.mkdir(parents=True, exist_ok=True)
    old_pdf, new_pdf = study_pdf_for(old_md), study_pdf_for(new_md)
    if new_md.exists():
        new_md.unlink()
    old_md.rename(new_md)
    if old_pdf.exists():
        if new_pdf.exists():
            new_pdf.unlink()
        old_pdf.rename(new_pdf)
    return new_md


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


def render_note(
    transcript: Transcript,
    insight: Insight,
    audio_name: str | None = None,
    *,
    study_stem_name: str | None = None,
    has_study_pdf: bool = False,
    asr_repairs: list | None = None,
    checked: frozenset[str] = frozenset(),
) -> str:
    """The full generated text of a transcript note.

    Frontmatter, then everything between the managed markers. The caller
    splices this over whatever the note held before, keeping the vault
    owner's own text below `NOTE_END` (see `write_note`).

    `checked` carries the action items the reader had already ticked, so a
    regeneration does not silently reopen closed commitments — ticking a box
    in Obsidian is the designed way to close one.

    `has_study_pdf` is whether a PDF was actually written at that stem: the
    renderer may have been unavailable, and the note must not offer a
    download of a file the vault does not hold. Same gate the study notes'
    own PDF link uses.
    """
    people_links = [_wikilink(p) for p in insight.people]
    headline = note_headline(transcript, insight)
    display_title = compose_display_title(headline, transcript.date)
    # The body is line-oriented, and the indexer reads its list items in
    # preference to the frontmatter, so every item has to fit on one line.
    action_items = [_one_line(a) for a in insight.action_items]
    key_points = [_one_line(kp) for kp in insight.key_points]
    repairs = list(asr_repairs or [])

    fm_lines = ["---"]
    fm_lines.append(f"source: {transcript.source}")
    fm_lines.append(f"date: {transcript.date.isoformat()}")
    fm_lines.append(f"transcript_id: {transcript.id}")
    fm_lines.append(f"headline: {_yaml_str(headline)}")
    fm_lines.append(f"kind: {_yaml_str(insight.kind)}")
    if insight.course_code:
        fm_lines.append(f"course_code: {_yaml_str(insight.course_code)}")
    if insight.course_name:
        fm_lines.append(f"course_name: {_yaml_str(insight.course_name)}")
    # The one-paragraph retrieval abstract. It lives in frontmatter, not the
    # body, because the body's '## Summary' is now the long-form summary the
    # reader gets — and every corpus-wide reader still needs the short one.
    fm_lines.append(f"abstract: {_yaml_str(_one_line(insight.summary))}")
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
    if repairs:
        # Every ASR repair is listed for audit: the reader can see exactly
        # which spoken text was rewritten, and check it against the transcript.
        fm_lines.append("asr_repairs:")
        for r in repairs:
            fm_lines.append(f"  - heard: {_yaml_str(r.heard)}")
            fm_lines.append(f"    corrected: {_yaml_str(r.corrected)}")
    if study_stem_name:
        fm_lines.append(f"study_notes: {_yaml_str(study_stem_name)}")
    if insight.sentiment:
        fm_lines.append(f"sentiment: {insight.sentiment}")
    fm_lines.append("---")

    body = [NOTE_BEGIN, "", f"# {display_title}", ""]
    if people_links:
        body.append("**People:** " + ", ".join(people_links))
    body.append(f"**Source:** {transcript.source}  ·  **Date:** {format_long_date(transcript.date)}")
    body.append("")
    if audio_name:
        body.append("## Recording")
        body.append(f"![[{audio_name}]]")
        body.append("")
    if study_stem_name:
        body.append("## Study Notes")
        link = f"- [[{study_stem_name}|Full study notes]]"
        if has_study_pdf:
            link += f"  ·  [[{study_stem_name}.pdf|Printable PDF]]"
        body.append(link)
        body.append("")
    body.append("## Summary")
    body.append(
        _body_text(insight.detailed_summary or insight.summary, within="## Summary")
        or "_No summary._"
    )
    body.append("")
    body.append("## Key Points")
    body.extend([f"- {kp}" for kp in key_points] or ["- _None._"])
    body.append("")
    body.append("## Action Items")
    body.extend(
        [f"- [{'x' if a in checked else ' '}] {a}" for a in action_items]
        or ["- _None._"]
    )
    body.append("")
    if insight.topics:
        body.append("## Topics")
        body.append(" ".join(f"#{slugify(t)}" for t in insight.topics))
        body.append("")
    body.append("## Transcript")
    body.append(_quote_block(transcript.text))
    body.append("")
    body.append(NOTE_END)

    return "\n".join(fm_lines) + "\n\n" + "\n".join(body) + "\n"


def _owner_tail(path: Path) -> str:
    """Whatever the vault owner wrote below the generated region, if anything.

    Two shapes, because notes written before the managed markers existed have
    no end marker: after `NOTE_END` when it is there, and otherwise after the
    transcript callout, whose end `transcript_bounds` is the one definition
    of. Anything this returns is appended back verbatim after the regenerated
    region — the note is the source of truth and hand edits are respected.

    The marker is matched as a whole LINE, never as a substring: the callout
    writes every transcript line as `> …`, so a recording (or a hand-typed
    tail) that happens to contain the marker text would otherwise be found
    first and every regeneration would splice from inside the transcript.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines = text.splitlines()
    end = next((i for i, ln in enumerate(lines) if ln.strip() == NOTE_END), None)
    if end is not None:
        tail = "\n".join(lines[end + 1:])
    else:
        bounds = transcript_bounds(lines)
        if bounds is None:
            return ""
        tail = "\n".join(lines[bounds[1] + 1:])
    # Leading blank lines are the separator, not content: returning them and
    # then adding one back would grow the gap by a line on EVERY regeneration.
    # The caller re-emits exactly one.
    return tail.lstrip("\n") if tail.strip() else ""


def _checked_items(path: Path) -> frozenset[str]:
    """Action items the reader has already ticked in this note."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    return frozenset(t for t, done in parse_action_items(text) if done)


def _existing_study_link(
    cfg: Config, note_path: Path, transcript_id: str
) -> tuple[str | None, bool]:
    """(study stem, whether its PDF exists) for study notes already on disk.

    Resolved through `resolve_study_note_path`, so the ladder is walked and
    `owns_note` is the proof — a stem that is not provably this transcript's
    answers None and is never linked.
    """
    study = resolve_study_note_path(cfg, note_path, transcript_id)
    if study is None:
        return None, False
    return study.stem, study_pdf_for(study).exists()


def _existing_asr_repairs(path: Path) -> list[AsrRepair]:
    """The ASR repairs already recorded in a note, read back off its own frontmatter.

    Parsed with the same reader the indexer uses, because `_yaml_str` escaping
    means only real YAML round-trips what was written. Callers gate this on
    the same ownership proof `_owner_tail` and `_checked_items` use: a note we
    cannot prove is ours is never opened for its content.
    """
    try:
        raw = frontmatter.load(str(path)).metadata.get("asr_repairs")
    except Exception:  # noqa: BLE001 - an unreadable note simply carries nothing
        return []
    out: list[AsrRepair] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        heard = str(item.get("heard") or "").strip()
        corrected = str(item.get("corrected") or "").strip()
        if heard and corrected:
            out.append(AsrRepair(heard=heard, corrected=corrected))
    return out


def write_note(
    cfg: Config,
    transcript: Transcript,
    insight: Insight,
    audio_name: str | None = None,
    *,
    path: Path | None = None,
    study_stem_name: str | None = None,
    has_study_pdf: bool = False,
    asr_repairs: list | None = None,
) -> Path:
    """Write the note, at `path` when the caller already claimed one.

    `claim_note_path` reads the filesystem, so evaluating it a second time here
    could answer differently from the one the caller downloaded the audio
    against — and the embed baked into the body would name a stem the note no
    longer sits on. One claim, threaded through both.

    If something else did take that path while the recording streamed, the
    claim is redone and the recording NEVER follows: the stem belongs to
    whoever took it, so its mp3 is theirs and this note is written with no
    embed at all rather than one naming a file it does not own — and it stays
    without one until the transcript itself changes. Either way the name in the
    body and the file on disk cannot disagree, and nothing unowned is written
    over.

    Regeneration is a SPLICE, not an overwrite: the generated region replaces
    what was there, and anything the owner wrote below it — plus the boxes
    they ticked inside it, and the study notes an earlier run left on disk —
    comes back. Only a note we can prove is ours is ever read for that; a
    stranger's file is not opened for its content.

    Carrying the study link here rather than at each caller is deliberate:
    this is the only place that knows which path was FINALLY claimed, and the
    study stem is derived from that path. `study_stem_name` therefore means
    "what this run produced"; absent, the note keeps what the vault already
    holds.
    """
    if path is None:
        path = note_path_for(cfg, transcript, insight)
    elif path.exists() and not owns_note(path, transcript.id):
        _log.warning(
            "%s was taken while the recording downloaded; re-claiming, and "
            "leaving anything at that stem to the note that owns it",
            path,
        )
        # Both the recording and the study notes were written against the
        # stem that was just taken, so neither is this note's to name any
        # more: the links are dropped rather than pointing at another
        # transcript's files. They stay orphans until this one changes again.
        audio_name = None
        study_stem_name = None
        has_study_pdf = False
        path = note_path_for(cfg, transcript, insight)
    ours = owns_note(path, transcript.id)
    tail = _owner_tail(path) if ours else ""
    checked = _checked_items(path) if ours else frozenset()
    if ours and not study_stem_name:
        # This run produced no study notes — the recording is not a lecture,
        # the profile is off, or the pass failed — but an earlier one may have
        # left some, and they are still on disk and still served by the
        # dashboard. Dropping the link would make the note disagree with the
        # vault, so it is carried across exactly like a ticked checkbox is.
        study_stem_name, has_study_pdf = _existing_study_link(cfg, path, transcript.id)
        if study_stem_name and asr_repairs is None:
            asr_repairs = _existing_asr_repairs(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = render_note(
        transcript,
        insight,
        audio_name=audio_name,
        study_stem_name=study_stem_name,
        has_study_pdf=has_study_pdf,
        asr_repairs=asr_repairs,
        checked=checked,
    )
    # `generated` already ends in a newline, so one more is exactly one blank
    # line between the managed region and the owner's own text.
    path.write_text(generated + (f"\n{tail}" if tail else ""), encoding="utf-8")
    return path


def write_managed(
    cfg: Config,
    path: Path,
    generated: str,
    *,
    title: str = "",
    transcript_id: str = "",
) -> Path:
    """Write generated content into a synthesis note, preserving user edits.

    Only the region between the synth markers is ever rewritten; anything the
    user adds outside it survives regeneration (R9). Refuses to write outside
    the synthesis namespaces (Digests/People/Studies/Prep/Categories/Study Notes).

    `transcript_id` is for the per-transcript namespace (Study Notes/), where a
    file is one transcript's rather than the namespace's as a whole: it is
    stamped into the frontmatter so the note can later prove whose it is, and
    an existing file that does NOT prove it is refused rather than rewritten.
    The corpus-wide notes (a digest, a dossier) pass none and are unaffected.
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
    if transcript_id and resolved.exists() and not owns_note(resolved, transcript_id):
        raise ValueError(
            f"refusing to rewrite {path}: its transcript_id does not prove it "
            f"belongs to {transcript_id}"
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
        head = ["---", "synth: true"]
        if transcript_id:
            head.append(f"transcript_id: {transcript_id}")
        head += ["---", ""]
        if title:
            head.append(f"# {title}")
            head.append("")
        new_text = "\n".join(head) + region + "\n"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(new_text, encoding="utf-8")
    return resolved


def claim_study_note_path(cfg: Config, note_path: Path, transcript_id: str) -> Path:
    """The study-notes path this transcript may occupy for a given note.

    Separate from the write because the CONTENT names it: the markdown links
    its own PDF by stem, and the ladder may have moved that stem. One claim,
    threaded through the link and the write, the same way `note_path_for` is
    threaded through the audio embed and `write_note`.
    """
    return claim_study_path(study_note_path_for(cfg, note_path), transcript_id)


def resolve_study_note_path(
    cfg: Config, note_path: Path, transcript_id: str
) -> Path | None:
    """The study note a transcript note actually has, ladder included, or None.

    The reader's counterpart to `claim_study_note_path`: same base, same
    rungs, `owns_note` as the only proof. Every reader of the study namespace
    goes through here rather than assuming the base stem, so notes the ladder
    moved are neither invisible nor left behind.
    """
    return resolve_study_note(study_note_path_for(cfg, note_path), transcript_id)


def write_study_note(
    cfg: Config,
    note_path: Path,
    transcript_id: str,
    generated: str,
    *,
    title: str = "",
    path: Path | None = None,
) -> Path:
    """Write a lecture's markdown study notes beside (not over) anything else.

    The claim covers the whole study stem — the markdown and the PDF that
    shares its name — so the PDF written next has a note at its stem that
    proves whose it is.
    """
    if path is None:
        path = claim_study_note_path(cfg, note_path, transcript_id)
    return write_managed(
        cfg, path, generated, title=title, transcript_id=transcript_id
    )


def write_study_pdf(study_md: Path, transcript_id: str, pdf_bytes: bytes) -> Path | None:
    """Write the rendered PDF at a study stem that is PROVABLY this one's.

    A PDF carries no frontmatter, so the study note beside it is the only
    proof — exactly the rule that governs an mp3 in Attachments/. No note, or
    a note that is someone else's, means the PDF is not written and the reason
    is logged; nothing unowned is replaced.
    """
    if not owns_note(study_md, transcript_id):
        _log.warning(
            "not writing %s: no study note at that stem proves it is this "
            "transcript's", study_pdf_for(study_md),
        )
        return None
    pdf = study_pdf_for(study_md)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(pdf_bytes)
    return pdf


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
