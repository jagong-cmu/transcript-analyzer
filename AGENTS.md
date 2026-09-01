# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Setup and tests

`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`, then `.venv/bin/python -m pytest`.
Study-note PDFs need the Playwright Chromium (`.venv/bin/playwright install chromium`); the one
test that starts a browser skips without it, so CI stays browser-free and the diagram gates that
always run are the deterministic ones in `tests/test_lecture_profile.py`.
Tests need no config.toml or network; `tests/conftest.py` builds a `Config` over a tmp vault.
Runtime code resolves config via `$TRANSCRIPT_ANALYZER_CONFIG`, else `config.toml`, else
`config.example.toml` (see `config.py:_config_file`) — set that env var to exercise the app
against a scratch vault instead of the real one.
`.github/workflows/tests.yml` runs that same suite on pushes to `main` and PRs against it,
across the Python versions `pyproject` declares (3.10–3.12) — a change has to work on the
3.10 floor, not only in the local venv.

## Sharp edges

- **A new output namespace goes in `writer.SYNTH_SUBDIRS` AND
  `indexer.EXCLUDED_SUBDIRS`.** One without the other breaks the feedback-loop guard and the
  indexer starts ingesting synthesis output as if it were a transcript, in an unattended
  20-minute loop. `Study Notes/` (lecture study notes + their PDFs) is the newest one;
  `tests/test_study_ownership.py` asserts the pairing.
- **The vault notes are the source of truth; the SQLite index is derived.** `obsidian/writer.py`
  emits frontmatter by hand-formatting strings, so every hand-written scalar must go through
  `writer._yaml_str` — it is the only thing escaping the backslashes, quotes, newlines and
  control characters that arrive in LLM headlines and transcript text. Frontmatter that is not
  valid YAML makes a note vanish from the index and the dashboard until it is fixed;
  `pipeline/indexer.py:parse_note` fails soft on such a note (logs a warning, skips that note
  alone) so one bad note no longer aborts `reindex_all`, but the note is still missing. Watch
  for the warning rather than assuming a note is indexed.
- **Two summaries, and only the SHORT one may reach the corpus.** The note body's
  `## Summary` is the long summary a reader reads straight through; the one-paragraph
  `abstract:` in frontmatter is the retrieval field, and it is what `NoteRecord.summary`
  carries. Every corpus-wide reader (digests, dossiers, study rollups, category rollups,
  `rag.py`) reads `rec.summary`, and Ask sends every one of them on every question — so
  putting the long summary in that field silently triples the corpus (~58k tokens to ~165k).
  The bound is ONE function, `titles.retrieval_abstract` (`titles.ABSTRACT_CHARS`), applied to
  the model's own `abstract` field in `insights.insight_from_payload` AND to the fallback
  `indexer.parse_note` uses for a note written before the split — a bound enforced on the
  fallbacks only is not a bound. `rec.detailed_summary` is the long one.
- **The note BODY is parsed back too, so it is an interface, not formatting.** Free text
  written into it must go through `writer._one_line` (list items, which the indexer prefers
  over the frontmatter list) or `writer._body_text` (the summary, where a heading-shaped line
  would open a section the writer never opened). And the `## Transcript` section has exactly
  one grammar — heading, optional blank run, contiguous `>` run — which is now ONE function,
  `writer.transcript_bounds`, called by `indexer.extract_transcript`,
  `scripts/backfill_timestamps.py` and `writer._owner_tail`. A transcript's own blank line is
  emitted as `> `, which is what makes a truly blank line an unambiguous end of the callout.
  Disagreement here silently duplicates a transcript, or splices away whatever the vault owner
  appended below it.
- **The body escape is bounded to the level that would CLOSE the section, not to every
  heading.** `writer._body_text(value, within="## Summary")` escapes only what
  `indexer.is_section_end` would read as ending that section, so a `###` the model wrote
  inside a long summary survives as real structure. Escaping every heading shape was harmless
  when summaries were two sentences and is a visible backslash now that they are not — and the
  extraction prompt asks for `###` precisely because of this bound. Change one, change both.
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
- **A study stem is claimed the way an audio stem is: through the note that carries the id.**
  `Study Notes/<note stem> (study notes).md` holds `transcript_id`; the `.pdf` beside it has no
  frontmatter, so that note is the only proof of whose it is — `writer.claimable_study_stem`,
  `claim_study_path`, `write_study_pdf` and `move_study_with_note` are the four call sites, all
  built on `owns_note` and on the ONE suffix ladder, now `writer._claim_ladder` (shared with
  `claim_note_path`). READERS walk that same ladder through `writer.resolve_study_note` /
  `resolve_study_note_path` — the dashboard's `web/app.py:_study_paths`, the retitle link
  rewrite, and `move_study_with_note`'s source end — because a stem the ladder pushed to
  `<stem> (<id6>)` is still ours, and a reader that assumed the base stem made those notes
  invisible and left them behind on a rename. The resolver PROVES with `owns_note` and answers
  None otherwise, so nothing unowned is served, moved or written. The study stem is deliberately
  NOT the note's own stem: two vault files with one name make every `[[wikilink]]` to it
  ambiguous.
- **A transcript note is regenerated, not overwritten.** `write_note` splices the region
  between `writer.NOTE_BEGIN`/`NOTE_END`, keeps whatever the owner wrote below the end marker,
  and carries ticked checkboxes across (ticking one is how a commitment is closed — reopening
  it is data loss). A note that predates the markers has its tail recovered from the end of the
  transcript callout, which is why `writer.transcript_bounds` is one definition used by the
  writer, `indexer._extract_transcript` and `scripts/backfill_timestamps.py` alike. Only a note
  we can PROVE is ours is ever read for that content. Study notes an EARLIER run left are
  carried the same way: `study_stem_name` means "what this run produced", and when it is absent
  `write_note` re-links whatever `resolve_study_note_path` proves is still ours (with the PDF
  link gated on the `.pdf` existing, and `asr_repairs:` read back off our own frontmatter).
  That lives in `write_note`, not in its callers, because it is the only place that knows which
  path was FINALLY claimed — a lecture pass that is off, skipped or contained must not make the
  note claim study notes are gone while the dashboard still serves them.
  A RENAME writes to a stem that does not exist yet, so every one of those carry-acrosses would
  find nothing there: `write_note(previous=…)` names the note being renamed away from, and
  `writer._carry_source` picks whichever of destination-then-previous `owns_note` proves —
  `previous` is a hint, never a permission. sync must therefore write the new note BEFORE
  deleting the old one; reversing that order loses the tail, the ticks and the study link with
  nothing left to recover them from.
- **A diagram renders or it is dropped.** The transcript has no visual channel, so every
  diagram is reconstructed from speech. `render/pdf.py` validates each spec in the same page it
  is about to print (`mermaid.parse`, KaTeX with `throwOnError`) and removes the whole figure —
  caption included — when it fails; `lecture.prune_visuals` then removes the same ones from the
  markdown so the two renderings never disagree. Nothing is ever drawn to stand in for a failed
  diagram. The PDF is rendered BEFORE the markdown is written for exactly that reason, and the
  markdown is written BEFORE the PDF bytes because it is the ownership proof for the stem.
- **An unattended pass must not leave half-state a retry cannot undo.** `sync.process_transcript`
  runs the lecture pass BEFORE `_maybe_download_audio` because `_study_notes_for` propagates a
  truncated response (`LLMResponseError`) rather than absorbing it: failing after the download
  would strand an mp3 at a stem no note ever occupies, and `claimable_stem` then refuses that
  stem forever, so every retry lands one rung further up `_claim_ladder` and re-fetches the
  whole recording. Order side effects so the propagating step comes first. The same rule makes
  `LLMResponseError` a PER-NOTE failure in `scripts/backfill_summaries.py` — only the kill
  switch and the budget end a run, since a `--batch` run has already been billed for every
  payload it would discard.
- **Models and effort are per STAGE, not global.** `LLM.create/stream/chat_json` take
  `stage=` and resolve the model and `output_config.effort` from
  `[anthropic.stage_models]` / `[anthropic.stage_effort]`; `_record` prices the model that
  actually ran, not `self.model`. Thinking is ON BY DEFAULT on Opus 5 and billed as output, so
  a new stage that only fills in a schema must be pinned `low` or it is a silent cost. A model
  with no `PRICING` row bills at the Opus fallback — add the row when adding the model.
- Scripts under `scripts/` write to the *live* vault and call the Pocket/Granola/Anthropic
  APIs. Use `--dry-run`/`--limit` when exercising them. `backfill_summaries.py` is the
  exception that is dry-run by DEFAULT (`--apply` writes) and backs up every note it rewrites
  to `data/backfill-backups/<timestamp>/`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
