# transcript-analyzer

Personal system that ingests your **Granola** + **Pocket AI** transcripts, extracts insights and
categories with a **local Ollama** model, stores them as notes in your **Obsidian** vault, and
serves a local **dashboard** to browse grouped conversations and **ask questions** (RAG chat).

Everything runs locally. The only outbound network call is the Granola API pull; the LLM (Ollama)
is on `localhost`, and your transcripts never leave your machine for analysis.

## How it works

```
Granola API ──┐                         ┌─> Obsidian notes  (source of truth for insights)
              ├─> sync ─> Ollama ────────┤
Pocket folder ┘   (insights + category)  └─> SQLite + embeddings (derived index)
                                                      │
                                     FastAPI dashboard (localhost:8787)
                                       • categories  • insights  • RAG chat with citations
```

- **Pocket AI** already writes markdown into a vault folder (`Pocket AI Recordings`) — we read it.
- **Granola** is pulled via its **official public API** (`public-api.granola.ai/v1`) using an API key.
- Insight notes are written to `Transcript Insights/<Category>/…` in your vault (the canonical store).
- A SQLite + embedding index is rebuilt *by parsing those notes*, so the dashboard uses your vault.

## Setup

```bash
cd transcript-analyzer
python3 -m venv .venv
./.venv/bin/pip install -e .
cp config.example.toml config.toml      # then edit config.toml
```

Edit `config.toml`:
- `[vault] path` / `name` — your Obsidian vault (defaults point at `~/Documents/Obsidian Vault`).
- `[ollama] chat_model` — recommend `ollama pull qwen2.5:7b-instruct` for better insights
  (a 3B model works but is weaker). `embed_model` stays `nomic-embed-text`.
- `[granola] token` — paste your Granola **API key** (starts with `grn_`) to enable Granola sync
  (leave blank to skip). Create one in the Granola desktop app (Business plan).

Make sure Ollama is running and has the models:

```bash
ollama pull qwen2.5:7b-instruct   # or keep qwen2.5:3b
ollama pull nomic-embed-text
```

## Usage

```bash
# One-off sync (all configured sources)
./.venv/bin/python scripts/run_sync.py

# Just Pocket, first 3, dry run (no writes) — good for a first test
./.venv/bin/python scripts/run_sync.py --source pocket --limit 3 --dry-run

# Granola only
./.venv/bin/python scripts/run_sync.py --source granola

# Start the dashboard
./.venv/bin/python -m transcript_analyzer.web.app
# -> http://127.0.0.1:8787
```

### Background automation (launchd)

```bash
bash scripts/install_launchd.sh          # sync every [sync.interval_seconds]; dashboard always on
launchctl list | grep transcript         # verify
bash scripts/install_launchd.sh uninstall
```

## Layout

- `src/transcript_analyzer/connectors/` — `pocket.py` (vault markdown), `granola.py` (API)
- `src/transcript_analyzer/pipeline/` — `llm.py` (Ollama), `insights.py`, `categorize.py`, `indexer.py`
- `src/transcript_analyzer/obsidian/writer.py` — writes insight notes + category indexes
- `src/transcript_analyzer/sync.py` — orchestrator (`--source`, `--limit`, `--dry-run`, `--force`)
- `src/transcript_analyzer/rag.py` — retrieval + local answer with citations
- `src/transcript_analyzer/web/` — FastAPI dashboard + templates
- `data/` — SQLite index, taxonomy, logs (gitignored)

## Notes

- **Granola API**: uses the official public API — `GET /notes` (cursor pagination, `created_after`
  filter) and `GET /notes/{id}?include=transcript`. Sync is incremental via a `created_at`
  high-water mark stored in the `meta` table (`--force` ignores it for a full resync).
- The category taxonomy grows automatically; near-duplicate proposals are merged by embedding
  similarity (`[taxonomy] merge_threshold`).
