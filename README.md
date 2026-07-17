# transcript-analyzer

Personal system that ingests your **Granola** + **Pocket AI** transcripts, extracts insights with
the **Claude API**, stores them as notes in your **Obsidian** vault, and — the important part —
**pushes synthesis back into the vault**: a daily digest, a live commitment tracker, dossiers on
the people you talk to repeatedly, research-study rollups, and prep notes for tomorrow's meetings.
A local **dashboard** surfaces the synthesis as a CEO briefing (digest, commitments, people,
prep) and lets you ask questions with citations.

> **Privacy note:** transcript *storage* is local (your Obsidian vault + a local SQLite index),
> but analysis is not — transcripts are sent to the Anthropic API for insight extraction,
> synthesis, and chat. If a conversation includes other people (interviewees, colleagues), their
> words are sent too. Don't enable this system for conversations where that's not acceptable.

## How it works

```
Granola API ──┐                              ┌─> Obsidian notes   (source of truth)
              ├─> sync ──> Claude API ────────┤
Pocket API  ──┘   (junk filter -> insights)   └─> SQLite index    (derived by parsing notes)
                                                        │
                     daily synthesis (Claude API) ──────┤
                       • Digests/YYYY-MM-DD.md          │
                       • Digests/Commitments.md         │
                       • People/<Name>.md   (dossiers)  │
                       • Studies/<Name>.md  (rollups)   │
                       • Prep/<date> <mtg>.md           │
                                                        │
                                  FastAPI dashboard (localhost:8787)
                                    • today’s digest  • commitments  • people
                                    • prep  • run synthesis  • chat  • browse
```

- **Pocket AI** and **Granola** are pulled via their official public APIs (incremental, with
  `created_at` high-water marks; `--force` for a full resync).
- A **junk filter** drops test recordings and background noise at ingest, before any billable
  LLM call.
- Insight notes are written **flat, organized by recording date** into `Transcript Insights/`
  (the canonical store). Attendee **emails are persisted** in frontmatter — they're the stable
  person-identity key that powers dossiers and meeting prep.
- **Synthesis runs at most once per day** (not per sync cycle) and writes only into
  `Digests/`, `People/`, `Studies/`, and `Prep/` — inside managed regions
  (`<!-- synth:begin -->` … `<!-- synth:end -->`), so anything you write outside the markers
  survives regeneration. Every LLM claim must carry a verbatim quote from the conversation it
  cites; claims that fail this gate are dropped in code, not trusted from the prompt.
- The **dashboard home** is a CEO briefing: it reads those synthesis notes (plus live
  commitments from the index), links every claim back to its source conversation, and can
  trigger synthesis from the UI.
- **Commitments** are pure extraction (no LLM): every unchecked `- [ ]` across your notes,
  linked back. Tick the box in the conversation note to close one.- **Chat** is agentic retrieval: Claude reads *every* conversation summary in context (no
  embeddings, no top-k) and pulls full transcripts on demand — speaker labels, dates, and
  proper nouns stay exact.
- **Pocket audio** is downloaded into `Transcript Insights/Attachments/` and embedded in each
  note. Granola's API exposes no audio.

## Cost guard

This runs unattended against a paid API, so the guards are hard, not advisory:

- `[anthropic] monthly_budget_usd` — spend ledger in SQLite; calls stop at the ceiling.
- `[anthropic] max_calls_per_run` — bounds any single sync/synthesis run.
- **Kill switch:** `touch data/llm.kill` stops all API calls immediately; delete to resume.
- `GET /health` on the dashboard shows this month's spend.

Expected cost at ~50 conversations/month with Opus 4.8: roughly $4–6/month.

## Setup

```bash
cd transcript-analyzer
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml      # then edit config.toml
```

Edit `config.toml`:
- `[vault] path` / `name` — your Obsidian vault.
- `[anthropic] api_key` — your Claude API key (or set `ANTHROPIC_API_KEY`). Default model is
  `claude-opus-4-8`; set `model = "claude-sonnet-5"` to cut costs ~60%.
- `[granola] token` / `[pocket] api_key` — source API keys (leave blank to skip a source).
- `[synthesis]` — set `self_names`/`self_emails` so you don't get a dossier on yourself, and
  declare `[[synthesis.studies]]` blocks for research rollups.
- `[calendar] ics_url` — optional secret ICS feed for tomorrow's-meeting prep notes.

## Usage

```bash
# One-off sync (all configured sources; runs synthesis if it hasn't run today)
./.venv/bin/python scripts/run_sync.py

# Just Pocket, first 3, dry run (no writes) — good for a first test
./.venv/bin/python scripts/run_sync.py --source pocket --limit 3 --dry-run

# Synthesis on demand
./.venv/bin/python scripts/synthesize.py                 # all steps
./.venv/bin/python scripts/synthesize.py --only digest   # one step
./.venv/bin/python scripts/synthesize.py --force         # ignore change detection

# Start the dashboard
./.venv/bin/python -m transcript_analyzer.web.app
# -> http://127.0.0.1:8787
```

### Categorizing (on demand)

Notes are organized by date. To group them into categories *you* choose, run:

```bash
./.venv/bin/python scripts/categorize.py Fundraising Hiring Product Personal
```

Claude assigns each note to one of your categories (or none), synthesizes a
scoped briefing (themes + open threads) per category, and writes non-destructive
Category notes under `Transcript Insights/Categories/`. Re-run anytime with a
different list; `--reset` clears it.

### Background automation (launchd)

```bash
bash scripts/install_launchd.sh          # sync every [sync.interval_seconds]; dashboard always on
launchctl list | grep transcript         # verify
bash scripts/install_launchd.sh uninstall
```

## Layout

- `src/transcript_analyzer/connectors/` — `pocket_api.py`, `pocket.py` (vault fallback), `granola.py`
- `src/transcript_analyzer/pipeline/` — `llm.py` (Claude API + cost guard), `quality.py` (junk
  filter), `insights.py`, `synthesize.py` (digest/dossiers/studies/prep), `organize.py`, `indexer.py`
- `src/transcript_analyzer/obsidian/writer.py` — transcript notes + managed-region synthesis writes
- `src/transcript_analyzer/calendar_feed.py` — ICS feed for meeting prep
- `src/transcript_analyzer/sync.py` — orchestrator (`--source`, `--limit`, `--dry-run`, `--force`,
  `--no-synthesis`)
- `src/transcript_analyzer/rag.py` — agentic retrieval chat with citations
- `src/transcript_analyzer/web/` — FastAPI briefing dashboard (reads synthesis notes + triggers runs)
- `tests/` — pytest suite (citation gate, cost guard, junk filter, indexer guards, managed regions)
- `data/` — SQLite index, spend ledger, logs, `llm.kill` switch (gitignored)

## Safety notes

- The indexer only reads flat notes with a `transcript_id` in frontmatter and skips the
  synthesis folders explicitly — synthesis output is never re-ingested (no feedback loop).
- Synthesis never edits transcript notes; it writes only inside its own namespaces, and only
  between its own markers.
- Insight-extraction failures are counted (`insight_failures_total` in the meta table) and
  surfaced in sync logs — an LLM failure never silently writes an empty note.

## Tests

```bash
./.venv/bin/python -m pytest
```
