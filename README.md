# transcript-analyzer

Personal system that ingests your **Granola** + **Pocket AI** transcripts, extracts insights with
the **Claude API**, stores them as notes in your **Obsidian** vault, and — the important part —
**pushes synthesis back into the vault**: a daily digest, a live commitment tracker, dossiers on
the people you talk to repeatedly, research-study rollups, and prep notes for tomorrow's meetings.
A local **dashboard** surfaces the synthesis as a CEO briefing (digest, commitments, people,
prep) and lets you ask questions with citations.

Every recording gets a **detailed summary** you can read straight through instead of a two-line
abstract. Recordings the model classifies as **lectures** get more: study-grade notes with real,
rendered diagrams and a printable PDF, written into the vault beside the transcript note.

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
      lecture? ──> study-notes pass (Opus 5) ──────────┤
                       • Study Notes/<stem>.md          │
                       • Study Notes/<stem>.pdf         │
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
                                    • prep  • lectures  • chat  • browse
```

- **Pocket AI** and **Granola** are pulled via their official public APIs (incremental, with
  `created_at` high-water marks; `--force` for a full resync).
- A **junk filter** drops test recordings and background noise at ingest, before any billable
  LLM call.
- Insight notes are written **flat, organized by recording date** into `Transcript Insights/`
  (the canonical store). Each note’s title is a descriptive one-liner with a long date
  (e.g. `Pricing deck review with Angela, July 1st, 2026`). Attendee **emails are persisted**
  in frontmatter — they're the stable person-identity key that powers dossiers and meeting prep.
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
  note. Transcript lines include `[M:SS]` timestamps you can click in the dashboard to seek
  the recording. Granola's API exposes no audio (timestamps still appear for reference).
- Every note carries **two summaries**. The note body shows the long one, sized to the
  recording (~150 words for a short call, 600–900 for an hour meeting, full study-note
  treatment for a lecture). A one-paragraph `abstract:` stays in frontmatter as the retrieval
  field: digests, dossiers, rollups and chat all read it, and chat sends every abstract on
  every question, so that is the field that has to stay small.
- Each note is regenerated inside **managed markers**
  (`<!-- transcript-analyzer:begin -->` … `:end`). Anything you write below the end marker
  survives, and a checkbox you ticked stays ticked.

### Lectures

The extraction pass classifies each recording (`lecture` / `meeting` / `interview` /
`personal`) in the same call that writes the summary, so detection costs nothing extra. There
is no course registry: the model names the course, and the code is normalized against the
courses already in the vault, so week 2 of `21-241` rejoins week 1 even when spelled `21241`.

A lecture then buys a second pass (Opus 5 by default) that writes into
`Transcript Insights/Study Notes/`:

- **Grounded, and separated from what is not.** Every section carries a verbatim span from the
  transcript and is dropped if that span does not string-match; so is every "assigned or
  examinable" claim. Gap-filling background is allowed but never blended in — it renders in
  its own clearly marked *Background (not from lecture)* block.
- **Real diagrams, never generated images.** The model emits diagram *specs* — Mermaid for
  flows and timelines, KaTeX for math, fenced code for SML — which are rendered
  deterministically in the Playwright Chromium. A diagram that fails to render is **dropped**,
  from the PDF and the markdown alike. Nothing is faked to stand in for it.
- **ASR repairs are auditable.** Lecture audio is a single mic with no speaker labels; a real
  15150 transcript says "this new age of AR" where the professor said AI. Repairs are allowed,
  and each one is listed in the note's frontmatter with the span it replaced — a repair whose
  span is not in the transcript is dropped.
- The PDF is linked from the transcript note, from the study note, and from the dashboard's
  **Lectures** page. Regeneration is idempotent, and both files follow the note if it is
  renamed.

PDF rendering needs the Playwright Chromium (`pip install -e '.[pdf]' && playwright install
chromium`). Without it the markdown study notes are still written; set `[lecture] pdf = false`
to skip it deliberately.

## Cost guard

This runs unattended against a paid API, so the guards are hard, not advisory:

- `[anthropic] monthly_budget_usd` — spend ledger in SQLite; calls stop at the ceiling.
- `[anthropic] max_calls_per_run` — bounds any single sync/synthesis run.
- **Kill switch:** `touch data/llm.kill` stops all API calls immediately; delete to resume.
- `GET /health` on the dashboard shows this month's spend.

Expected cost at ~50 conversations/month with Opus 4.8: roughly $4–6/month. A lecture adds one
Opus 5 study-notes call on top of its extraction.

Models and reasoning effort are set **per stage**, not globally
(`[anthropic.stage_models]` / `[anthropic.stage_effort]`): extraction runs at `low` effort
because it only fills in a schema, and the study-notes pass gets the expensive model. Thinking
is on by default on Opus 5 and billed as output, so an unpinned stage is a silent cost.

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
# With descriptions (steers matching):
./.venv/bin/python scripts/categorize.py \
  "Fundraising: LP updates, term sheets, raise strategy" \
  "Hiring: interviews, offer loops, recruiting debriefs"
```

Claude uses each category’s description to place notes, synthesizes a scoped
briefing (themes + open threads) per category, and writes non-destructive
Category notes under `Transcript Insights/Categories/`. Re-run anytime with a
different list; `--reset` clears it. The Browse page has the same UI.

### Migrating a vault written before titles + timestamps

Both are one-shot; each supports `--dry-run` and `--limit` so you can check a
handful first, and both are safe to re-run: `retitle_notes.py` keeps a headline
a note already has, and `backfill_timestamps.py` skips a transcript that already
carries `[M:SS]` lines. `--force` overrides either.

```bash
# Retitle existing notes. Uses Claude by default; --cheap derives the headline
# from the note's summary with no API calls. Renames files + matching audio.
./.venv/bin/python scripts/retitle_notes.py --limit 5 --dry-run
./.venv/bin/python scripts/retitle_notes.py

# Re-summarize existing notes into the detailed-summary shape (and give
# existing lectures study notes + PDFs). DRY RUN BY DEFAULT; --apply writes,
# after copying every note it touches to data/backfill-backups/<timestamp>/.
python scripts/backfill_summaries.py                     # see what it would do
python scripts/backfill_summaries.py --apply --limit 5   # try five first
python scripts/backfill_summaries.py --apply --batch --max-calls 200

# Re-fetch timed segments from Pocket/Granola and rewrite the ## Transcript
# section in place. No insight LLM calls.
./.venv/bin/python scripts/backfill_timestamps.py --source pocket --dry-run
./.venv/bin/python scripts/backfill_timestamps.py
```

A backfill note skipped with "N sync_state rows point at this note" cannot be
traced to a single recording, so it is left alone rather than filled in with
someone else's transcript; `scripts/backfill_timestamps.py --help` describes how
to resolve it by hand.

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
- `src/transcript_analyzer/pipeline/lecture.py` — the lecture profile: study-notes prompt pack,
  the citation/ASR/diagram gates, and the write order that keeps the PDF and the markdown in step
- `src/transcript_analyzer/pipeline/batch.py` — Message Batches API (half price) for the backfill
- `src/transcript_analyzer/pipeline/citations.py` — the one verbatim-quote match, shared by the
  synthesis citation gate and the lecture gates
- `src/transcript_analyzer/courses.py` — course identity for lectures, with no registry in config
- `src/transcript_analyzer/render/` — `study.py` (markdown + HTML, pure), `pdf.py` (Playwright,
  and the gate that drops diagrams that will not draw), `assets.py` (local mermaid/KaTeX mirror)
- `src/transcript_analyzer/obsidian/writer.py` — transcript notes + managed-region synthesis writes
- `src/transcript_analyzer/titles.py` — note titles (headline + long date), shared by the writer,
  indexer and insight extraction
- `src/transcript_analyzer/transcript_fmt.py` — `[M:SS]` transcript lines, shared by both
  connectors and the dashboard's seek links
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
- A response cut off at the model's output cap is *deterministic* — the same recording would
  overflow again every cycle — so it is recorded instead of retried, and billed once. The note
  is written with a visible warning callout and a queryable `extract_error:` /
  `study_notes_error:` key, keeping whatever the last complete pass wrote (summary, key points,
  action items, and the filename) rather than downgrading it.
- Nothing is written, renamed or deleted at a vault path that is not provably that
  transcript's own. A study note carries `transcript_id`; the PDF beside it is claimed through
  that note, exactly as an mp3 in `Attachments/` is claimed through the note at its stem.
- The study-notes pass is an upgrade, never a precondition: if it fails, the note is still
  written with its detailed summary.
- `scripts/backfill_summaries.py` is on demand only, dry-run by default, backs up every note it
  rewrites outside the vault, and skips any note whose `transcript_id` does not match.

## Tests

```bash
./.venv/bin/python -m pytest
```
