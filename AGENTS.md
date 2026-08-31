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
  emits frontmatter by hand-formatting strings, and `pipeline/indexer.py:parse_note` returns
  `None` on any load error — so frontmatter that is not valid YAML makes a note *silently
  disappear* from the index and the dashboard, with no error logged. Route every hand-written
  frontmatter scalar through `writer._yaml_str`.
- `reindex_all` calls `parse_note` in a bare loop with no per-note try/except: anything that
  raises there takes down the whole index, not one note. Fail soft inside `parse_note`.
- Scripts under `scripts/` write to the *live* vault and call the Pocket/Granola/Anthropic
  APIs. Use `--dry-run`/`--limit` when exercising them.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
