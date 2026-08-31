# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Setup and tests

`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`, then `.venv/bin/python -m pytest`.
Tests need no config.toml or network; `tests/conftest.py` builds a `Config` over a tmp vault.
Runtime code resolves config via `$TRANSCRIPT_ANALYZER_CONFIG`, else `config.toml`, else
`config.example.toml` (see `config.py:_config_file`) — set that env var to exercise the app
against a scratch vault instead of the real one.

## Sharp edges

- **The vault notes are the source of truth; the SQLite index is derived.** `obsidian/writer.py`
  emits frontmatter by hand-formatting strings, so every hand-written scalar must go through
  `writer._yaml_str` — it is the only thing escaping the backslashes, quotes, newlines and
  control characters that arrive in LLM headlines and transcript text. Frontmatter that is not
  valid YAML makes a note vanish from the index and the dashboard until it is fixed;
  `pipeline/indexer.py:parse_note` fails soft on such a note (logs a warning, skips that note
  alone) so one bad note no longer aborts `reindex_all`, but the note is still missing. Watch
  for the warning rather than assuming a note is indexed.
- Scripts under `scripts/` write to the *live* vault and call the Pocket/Granola/Anthropic
  APIs. Use `--dry-run`/`--limit` when exercising them.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
