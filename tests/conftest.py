import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from transcript_analyzer.config import (
    AnthropicConfig,
    CalendarConfig,
    Config,
    GranolaConfig,
    PocketConfig,
    QualityConfig,
    SynthesisConfig,
    SyncConfig,
    VaultConfig,
    WebConfig,
)
from transcript_analyzer.models import Attendee, NoteRecord, Transcript


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    (vault / "Transcript Insights").mkdir(parents=True)
    return Config(
        vault=VaultConfig(path=vault, name="Test Vault", insights_folder="Transcript Insights"),
        pocket=PocketConfig(folder="Pocket AI Recordings"),
        granola=GranolaConfig(token="", api_base="https://example.invalid"),
        anthropic=AnthropicConfig(
            api_key="test-key", monthly_budget_usd=5.0, max_calls_per_run=3
        ),
        quality=QualityConfig(),
        synthesis=SynthesisConfig(),
        calendar=CalendarConfig(),
        sync=SyncConfig(interval_seconds=1200),
        web=WebConfig(host="127.0.0.1", port=0),
        data_dir=tmp_path / "data",
    )


def make_record(
    tid: str = "abc123",
    title: str = "2026-07-01 chat-with-angela",
    date_str: str = "2026-07-01",
    summary: str = "Angela agreed to review the pricing deck by Friday.",
    people: list[str] | None = None,
    attendees: list[Attendee] | None = None,
    open_items: list[str] | None = None,
    note_path: str = "",
) -> NoteRecord:
    return NoteRecord(
        transcript_id=tid,
        source="granola",
        title=title,
        date=date_str,
        category="",
        people=people if people is not None else ["Angela Jin"],
        topics=["pricing"],
        action_items=open_items or [],
        open_action_items=open_items or [],
        attendees=attendees or [],
        summary=summary,
        note_path=note_path or f"/vault/Transcript Insights/{title}.md",
        transcript_text="Angela: I will review the pricing deck by Friday.",
    )


def make_transcript(title: str = "Team sync", text: str = "x" * 1000) -> Transcript:
    return Transcript(
        id="t1",
        source="pocket",
        native_id="n1",
        title=title,
        date=date(2026, 7, 1),
        text=text,
    )


APP_MODULE = "transcript_analyzer.web.app"


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    """The dashboard imported against a scratch config (never the real vault).

    web/app.py binds `cfg = load_config()` at module scope, so the module has
    to be dropped from sys.modules and re-imported for each test to actually
    get its own config instead of the first test's (deleted) tmp_path.
    """
    config = tmp_path / "config.toml"
    config.write_text(
        "[vault]\n"
        f"path = {json.dumps(str(tmp_path / 'vault'))}\n"
        'name = "Test Vault"\n'
        'insights_folder = "Transcript Insights"\n'
        "\n[pocket]\n"
        'folder = "Pocket AI Recordings"\n'
        "\n[anthropic]\n"
        'api_key = "test-key"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TRANSCRIPT_ANALYZER_CONFIG", str(config))

    from transcript_analyzer.config import load_config

    load_config.cache_clear()
    sys.modules.pop(APP_MODULE, None)
    app_module = importlib.import_module(APP_MODULE)

    yield app_module

    sys.modules.pop(APP_MODULE, None)
    load_config.cache_clear()
