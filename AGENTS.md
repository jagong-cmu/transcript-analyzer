# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Setup and tests

`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`, then `.venv/bin/python -m pytest`.
Tests need no config.toml or network; `tests/conftest.py` builds a `Config` over a tmp vault.
Runtime code resolves config via `$TRANSCRIPT_ANALYZER_CONFIG`, else `config.toml`, else
`config.example.toml` (see `config.py:_config_file`) — set that env var to exercise the app
against a scratch vault instead of the real one.
`.github/workflows/tests.yml` runs that same suite on pushes to `main` and PRs against it,
across the Python versions `pyproject` declares (3.10–3.12) — a change has to work on the
3.10 floor, not only in the local venv.

## Sharp edges

- **The vault notes are the source of truth; the SQLite index is derived.** `obsidian/writer.py`
  emits frontmatter by hand-formatting strings, so every hand-written scalar must go through
  `writer._yaml_str` — it is the only thing escaping the backslashes, quotes, newlines and
  control characters that arrive in LLM headlines and transcript text. Frontmatter that is not
  valid YAML makes a note vanish from the index and the dashboard until it is fixed;
  `pipeline/indexer.py:parse_note` fails soft on such a note (logs a warning, skips that note
  alone) so one bad note no longer aborts `reindex_all`, but the note is still missing. Watch
  for the warning rather than assuming a note is indexed.
- **The note BODY is parsed back too, so it is an interface, not formatting.** Free text
  written into it must go through `writer._one_line` (list items, which the indexer prefers
  over the frontmatter list) or `writer._body_text` (the summary, where a heading-shaped line
  would open a section the writer never opened). And the `## Transcript` section has exactly
  one grammar — heading, optional blank run, contiguous `>` run — that `writer._quote_block`,
  `indexer._extract_transcript` and `scripts/backfill_timestamps.py` (`_is_transcript_heading`
  + `_section_end`) must all agree on: change one and change all three. A transcript's own
  blank line is emitted as `> `, which is what makes a truly blank line an unambiguous end of
  the callout. Disagreement here silently duplicates a transcript, or splices away whatever
  the vault owner appended below it.
- **Where writer and reader must agree, they share ONE definition rather than two matching
  rules.** "Does this line open a section?" is `writer.opens_section`, used by
  `_body_text` to decide what to escape and by `indexer.is_section_start` (and through it
  the backfill) to find sections. Its `max_level` is how the same definition also answers
  where a section ENDS (`indexer.is_section_end`): the writer escapes any heading shape,
  but a section closes only at its own level or shallower, so a `###` the vault owner nested
  under `## Action Items` keeps its commitments in the index. Re-deriving either test on
  either side is how the escape and the parse drifted apart before — twice. Add call sites,
  not second definitions.
- **Nothing is written, renamed or deleted at a vault path that is not PROVABLY that
  transcript's own.** `writer.owns_note` is the proof (the note's `transcript_id`, read
  back); unknown for ANY reason — no id line, unreadable, permission denied — means NOT
  ours. `writer.claimable_stem` carries that proof to the whole stem — the note, the
  `Attachments/<stem>.mp3` that only a note can claim, and any `writer.audio_partial` still
  streaming towards it (a marker that stops claiming once it is older than
  `writer.PARTIAL_DOWNLOAD_TTL_SECONDS`, since a crash leaves one behind and nothing sweeps
  them) — and `writer.claim_note_path`, built on it, is the one definition of "where may
  this note go". All seven destructive paths go through those three — write
  (`note_path_for`), the re-claim inside `writer.write_note` when the path its caller
  already claimed was taken while the recording streamed, rename
  (`scripts/retitle_notes.py:_target_path`), delete (`sync._is_stale_note`, via
  `sync._owned_prev_note`), the audio move at BOTH ends, source and destination
  (`writer.move_audio_with_note`; the move runs before the rename in `_rename_with_audio`,
  because after it the source stem no longer holds the note that proves the recording is
  ours), and the download's final replace (`connectors/pocket_api.download_audio`, re-proven
  after the stream because a check made minutes earlier is not a claim). A path that cannot
  be proven ours is left alone and logged: the vault has no backup, so an orphan to clean up
  by hand beats a deletion that cannot be undone. Extend a destructive path by adding a call
  site here, never an eighth ownership rule. The index owes the same proof: `sync_state` is
  keyed on `(source, native_id)`, so its `note_path` is NOT unique — a row is this note's
  only when `models.stable_id(source, native_id)` matches the note's `transcript_id`, and an
  ambiguous match means do nothing (`retitle_notes._rename_with_audio`,
  `backfill_timestamps._sync_row_for`).
- Scripts under `scripts/` write to the *live* vault and call the Pocket/Granola/Anthropic
  APIs. Use `--dry-run`/`--limit` when exercising them.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
