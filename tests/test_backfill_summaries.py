"""The backfill rewrites the whole vault, so it has to be the careful one.

Dry run by default, a backup before the first write, hand edits preserved,
and never a note it cannot prove is the record's own.
"""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

from transcript_analyzer.models import Insight, Transcript
from transcript_analyzer.obsidian import writer
from transcript_analyzer.pipeline.indexer import index_note

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_summaries.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_summaries", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backfill_mod(cfg, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "load_config", lambda: cfg)
    return module


PAYLOAD = {
    "title": "Ignored, the note keeps its own headline",
    "kind": "meeting",
    "course_code": "",
    "course_name": "",
    "abstract": "A one paragraph abstract.",
    "detailed_summary": "A long summary that a reader can read straight through.",
    "key_points": ["A point"],
    "action_items": ["Send the deck", "Book the follow-up"],
    "people": ["Angela Jin"],
    "topics": ["pricing"],
    "sentiment": "neutral",
}


class StubLLM:
    def __init__(self, payload=None):
        self.payload = payload or PAYLOAD
        self.calls = 0

    def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
        self.calls += 1
        return self.payload


def seed_legacy_note(cfg, tid="t1", name="2026-07-01 pricing chat.md"):
    """A note in the OLD shape: short body summary, no `abstract:`."""
    path = cfg.vault.insights_path / name
    path.write_text(
        f"---\nsource: pocket\ndate: 2026-07-01\ntranscript_id: {tid}\n"
        'headline: "Pricing chat with Angela"\n'
        "action_items:\n  - \"Send the deck\"\n---\n\n"
        "# Pricing chat with Angela, July 1st, 2026\n\n"
        "## Summary\nAngela will review the deck.\n\n"
        "## Action Items\n- [ ] Send the deck\n\n"
        "## Transcript\n> [!note]- Full transcript\n"
        "> [0:03] Angela: I will review the pricing deck by Friday.\n",
        encoding="utf-8",
    )
    index_note(cfg, path)
    return path


def run(mod, llm, monkeypatch, **kw):
    monkeypatch.setattr(mod, "LLM", lambda cfg: llm)
    opts = dict(
        apply=False, limit=None, force=False, lectures_only=False,
        study_notes=False, use_batch=False, max_calls=None, resume_batch=None,
    )
    opts.update(kw)
    return mod.backfill(**opts)


def test_dry_run_is_the_default_and_writes_nothing(cfg, backfill_mod, monkeypatch):
    path = seed_legacy_note(cfg)
    before = path.read_text()
    llm = StubLLM()

    summary = run(backfill_mod, llm, monkeypatch)

    assert summary["planned"] == 1 and summary["written"] == 0
    assert path.read_text() == before
    assert llm.calls == 0  # a dry run does not even pay for the extraction
    assert not (cfg.data_dir / "backfill-backups").exists()


def test_apply_rewrites_the_note_and_backs_up_the_original(cfg, backfill_mod, monkeypatch):
    path = seed_legacy_note(cfg)
    original = path.read_text()

    summary = run(backfill_mod, StubLLM(), monkeypatch, apply=True)

    assert summary["written"] == 1
    text = path.read_text()
    assert "A long summary that a reader can read straight through." in text
    assert 'abstract: "A one paragraph abstract."' in text

    backups = list((cfg.data_dir / "backfill-backups").glob("*/*.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == original


def test_the_headline_and_filename_are_not_churned(cfg, backfill_mod, monkeypatch):
    """A backfill re-summarizes; renaming is retitle_notes.py's job, and it
    would break every wikilink the owner has written by hand."""
    path = seed_legacy_note(cfg)
    run(backfill_mod, StubLLM(), monkeypatch, apply=True)
    assert path.exists()
    assert 'headline: "Pricing chat with Angela"' in path.read_text()


def test_hand_edits_survive_the_rewrite(cfg, backfill_mod, monkeypatch):
    path = seed_legacy_note(cfg)
    path.write_text(
        path.read_text().replace("- [ ] Send the deck", "- [x] Send the deck")
        + "\n## My own notes\nShe seemed hesitant.\n",
        encoding="utf-8",
    )

    run(backfill_mod, StubLLM(), monkeypatch, apply=True)
    text = path.read_text()
    assert "She seemed hesitant." in text          # written below the transcript
    assert "- [x] Send the deck" in text           # a closed commitment stays closed
    assert "- [ ] Book the follow-up" in text      # and a new one is open


def test_a_note_that_is_not_the_records_own_is_skipped(cfg, backfill_mod, monkeypatch):
    path = seed_legacy_note(cfg, tid="t1")
    # The owner deleted the note and a different recording took the filename.
    path.write_text(
        path.read_text().replace("transcript_id: t1", "transcript_id: someone-else"),
        encoding="utf-8",
    )
    before = path.read_text()

    summary = run(backfill_mod, StubLLM(), monkeypatch, apply=True)
    assert summary["written"] == 0
    assert summary["skipped"]["not_ours"] == 1
    assert path.read_text() == before


def test_an_already_backfilled_note_is_not_paid_for_twice(cfg, backfill_mod, monkeypatch):
    transcript = Transcript(
        id="t2", source="pocket", native_id="n2", title="raw",
        date=date(2026, 7, 2), text="[0:01] Angela: hello.",
    )
    path = writer.write_note(
        cfg, transcript,
        Insight(headline="Already done", summary="abstract", detailed_summary="long"),
    )
    index_note(cfg, path)

    llm = StubLLM()
    summary = run(backfill_mod, llm, monkeypatch, apply=True)
    assert summary["planned"] == 0 and llm.calls == 0
    assert summary["skipped"]["done"] == 1

    # --force is the way to redo it deliberately.
    forced = run(backfill_mod, llm, monkeypatch, apply=True, force=True)
    assert forced["written"] == 1 and llm.calls == 1


def test_the_per_run_call_budget_is_not_silently_exceeded(cfg, backfill_mod, monkeypatch):
    for i in range(4):
        seed_legacy_note(cfg, tid=f"t{i}", name=f"2026-07-0{i + 1} chat {i}.md")
    # The fixture's budget is 3 calls; four notes need four.
    llm = StubLLM()
    summary = run(backfill_mod, llm, monkeypatch, apply=True)
    assert summary["written"] == 0 and summary["errors"] == 1
    assert llm.calls == 0

    raised = run(backfill_mod, llm, monkeypatch, apply=True, max_calls=10)
    assert raised["written"] == 4


def test_lectures_only_leaves_everything_else_alone(cfg, backfill_mod, monkeypatch):
    seed_legacy_note(cfg, tid="t1", name="2026-07-01 chat.md")
    summary = run(backfill_mod, StubLLM(), monkeypatch, apply=True, lectures_only=True)
    assert summary["planned"] == 0
    assert summary["skipped"]["not_lecture"] == 1


def test_a_note_with_no_transcript_is_skipped_not_summarized(cfg, backfill_mod, monkeypatch):
    """Without the transcript there is nothing to re-summarize from."""
    path = cfg.vault.insights_path / "2026-07-03 empty.md"
    path.write_text(
        "---\nsource: pocket\ndate: 2026-07-03\ntranscript_id: t9\n---\n\n"
        "## Summary\nSomething.\n",
        encoding="utf-8",
    )
    index_note(cfg, path)
    summary = run(backfill_mod, StubLLM(), monkeypatch, apply=True)
    assert summary["written"] == 0
    assert summary["skipped"]["no_transcript"] == 1


def test_one_unparseable_response_does_not_end_a_whole_vault_backfill(
    cfg, backfill_mod, monkeypatch
):
    """A truncated response is one NOTE's problem, not the run's.

    It arrives as an LLMError subclass, so the run-level stop swallowed it and
    broke the loop — worst under --batch, where every remaining payload has
    already been submitted, waited for and BILLED.
    """
    from transcript_analyzer.pipeline.llm import LLMResponseError

    good = seed_legacy_note(cfg, tid="t1", name="2026-07-01 first.md")
    bad = seed_legacy_note(cfg, tid="t2", name="2026-07-02 second.md")
    last = seed_legacy_note(cfg, tid="t3", name="2026-07-03 third.md")

    class OneBadResponse(StubLLM):
        def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
            self.calls += 1
            if "second" in user or self.calls == 2:
                raise LLMResponseError("Structured output truncated at max_tokens")
            return self.payload

    summary = run(backfill_mod, OneBadResponse(), monkeypatch, apply=True)

    assert summary["errors"] == 1
    assert summary["written"] == 2, "the run stopped at the first bad response"
    rewritten = "A long summary that a reader can read straight through."
    assert rewritten in good.read_text() and rewritten in last.read_text()
    assert rewritten not in bad.read_text()


def test_a_budget_stop_still_ends_the_run(cfg, backfill_mod, monkeypatch):
    """Narrowing the stop must not let a real run-level error through."""
    from transcript_analyzer.pipeline.llm import LLMBudgetError

    seed_legacy_note(cfg, tid="t1", name="2026-07-01 first.md")
    seed_legacy_note(cfg, tid="t2", name="2026-07-02 second.md")

    class OutOfBudget(StubLLM):
        def chat_json(self, system, user, schema, *, max_tokens=None, stage="", stream=False):
            self.calls += 1
            raise LLMBudgetError("Monthly spend ceiling reached")

    llm = OutOfBudget()
    summary = run(backfill_mod, llm, monkeypatch, apply=True)

    assert summary["written"] == 0 and summary["errors"] == 1
    assert llm.calls == 1, "the run kept paying after the ceiling was reached"
