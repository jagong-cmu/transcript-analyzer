import importlib
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
def app_mod(cfg, monkeypatch):
    """The dashboard bound to the tmp-vault `cfg` above — vault AND database.

    web/app.py resolves its config once, at module scope, so the module is
    dropped from sys.modules and re-imported per test. Pointing
    TRANSCRIPT_ANALYZER_CONFIG at a scratch toml is not enough on its own:
    Config.data_dir is a dataclass default (REPO_ROOT/"data") that no toml key
    can move, so a config loaded from a file still binds — and creates — the
    developer's real index.db. The isolation therefore comes from handing the
    module the Config object itself.
    """
    from transcript_analyzer import config as config_module

    real_load_config = config_module.load_config
    real_load_config.cache_clear()
    monkeypatch.setattr(config_module, "load_config", lambda: cfg)

    sys.modules.pop(APP_MODULE, None)
    app_module = importlib.import_module(APP_MODULE)
    assert app_module.cfg is cfg, "the dashboard did not bind the scratch config"

    yield app_module

    sys.modules.pop(APP_MODULE, None)
    real_load_config.cache_clear()
