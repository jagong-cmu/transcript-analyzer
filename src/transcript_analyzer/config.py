"""Load configuration from config.toml (falling back to config.example.toml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore

# Repo root = two levels up from this file (src/transcript_analyzer/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


@dataclass(frozen=True)
class VaultConfig:
    path: Path
    name: str
    insights_folder: str

    @property
    def insights_path(self) -> Path:
        return self.path / self.insights_folder


@dataclass(frozen=True)
class PocketConfig:
    folder: str
    api_key: str = ""
    api_base: str = "https://public.heypocketai.com/api/v1"
    download_audio: bool = True

    @property
    def api_enabled(self) -> bool:
        return bool(self.api_key.strip())


@dataclass(frozen=True)
class GranolaConfig:
    token: str
    api_base: str

    @property
    def enabled(self) -> bool:
        return bool(self.token.strip())


@dataclass(frozen=True)
class AnthropicConfig:
    """Claude API settings, including the hard cost guards.

    The API key may also come from the ANTHROPIC_API_KEY environment variable;
    the config value wins when both are set.
    """

    api_key: str = ""
    model: str = "claude-opus-4-8"
    max_tokens: int = 8192
    timeout: int = 300
    # Hard monthly spend ceiling (USD). Once the ledger reaches this, every
    # further call raises instead of billing. 0 disables the LLM entirely.
    monthly_budget_usd: float = 15.0
    # Hard cap on API calls in a single process run (one sync cycle, one
    # synthesis run, one dashboard process). Bounds runaway-loop damage.
    max_calls_per_run: int = 80


@dataclass(frozen=True)
class QualityConfig:
    """Ingest-time quality floor. Junk transcripts are skipped before any
    (now billable) LLM call and never written into the vault."""

    min_transcript_chars: int = 400
    junk_title_patterns: tuple[str, ...] = (
        "background noise",
        "hello testing",
        "testing a conversation",
        "getting started with pocket",
        "forwarded phone call",
        "your call has been forward",
        "asking to ask questions",
        "test recording",
    )


@dataclass(frozen=True)
class StudyConfig:
    name: str
    description: str


@dataclass(frozen=True)
class SynthesisConfig:
    enabled: bool = True
    # Days of conversations covered by the daily digest.
    digest_days: int = 7
    # A person needs at least this many conversations before a dossier is
    # written (85/105 people appear exactly once — dossiers for them are noise).
    dossier_min_conversations: int = 3
    # Names/emails identifying the vault owner, excluded from dossiers.
    self_names: tuple[str, ...] = ()
    self_emails: tuple[str, ...] = ()
    studies: tuple[StudyConfig, ...] = ()


@dataclass(frozen=True)
class CalendarConfig:
    """Optional read-only calendar feed (secret ICS URL) for meeting prep.
    Leave empty to disable prep notes."""

    ics_url: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.ics_url.strip())


@dataclass(frozen=True)
class SyncConfig:
    interval_seconds: int


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    vault: VaultConfig
    pocket: PocketConfig
    granola: GranolaConfig
    anthropic: AnthropicConfig
    quality: QualityConfig
    synthesis: SynthesisConfig
    calendar: CalendarConfig
    sync: SyncConfig
    web: WebConfig
    data_dir: Path = field(default=DATA_DIR)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def kill_switch_path(self) -> Path:
        """Touch this file to stop all Claude API calls immediately."""
        return self.data_dir / "llm.kill"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


def _config_file() -> Path:
    override = os.environ.get("TRANSCRIPT_ANALYZER_CONFIG")
    if override:
        return Path(override).expanduser()
    real = REPO_ROOT / "config.toml"
    if real.exists():
        return real
    return REPO_ROOT / "config.example.toml"


def _load(path: Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    vault_raw = raw["vault"]
    vault = VaultConfig(
        path=Path(vault_raw["path"]).expanduser(),
        name=vault_raw["name"],
        insights_folder=vault_raw.get("insights_folder", "Transcript Insights"),
    )
    pocket_raw = raw["pocket"]
    pocket = PocketConfig(
        folder=pocket_raw["folder"],
        api_key=pocket_raw.get("api_key", ""),
        api_base=pocket_raw.get("api_base", "https://public.heypocketai.com/api/v1"),
        download_audio=bool(pocket_raw.get("download_audio", True)),
    )
    granola_raw = raw.get("granola", {})
    granola = GranolaConfig(
        token=granola_raw.get("token", ""),
        api_base=granola_raw.get("api_base", "https://public-api.granola.ai/v1"),
    )
    anthropic_raw = raw.get("anthropic", {})
    anthropic = AnthropicConfig(
        api_key=anthropic_raw.get("api_key", ""),
        model=anthropic_raw.get("model", "claude-opus-4-8"),
        max_tokens=int(anthropic_raw.get("max_tokens", 8192)),
        timeout=int(anthropic_raw.get("timeout", 300)),
        monthly_budget_usd=float(anthropic_raw.get("monthly_budget_usd", 15.0)),
        max_calls_per_run=int(anthropic_raw.get("max_calls_per_run", 80)),
    )
    quality_raw = raw.get("quality", {})
    quality = QualityConfig(
        min_transcript_chars=int(quality_raw.get("min_transcript_chars", 400)),
        junk_title_patterns=tuple(
            quality_raw.get("junk_title_patterns", QualityConfig.junk_title_patterns)
        ),
    )
    synth_raw = raw.get("synthesis", {})
    synthesis = SynthesisConfig(
        enabled=bool(synth_raw.get("enabled", True)),
        digest_days=int(synth_raw.get("digest_days", 7)),
        dossier_min_conversations=int(synth_raw.get("dossier_min_conversations", 3)),
        self_names=tuple(synth_raw.get("self_names", [])),
        self_emails=tuple(e.lower() for e in synth_raw.get("self_emails", [])),
        studies=tuple(
            StudyConfig(name=s["name"], description=s.get("description", ""))
            for s in synth_raw.get("studies", [])
            if s.get("name")
        ),
    )
    calendar = CalendarConfig(ics_url=raw.get("calendar", {}).get("ics_url", ""))
    sync = SyncConfig(interval_seconds=int(raw.get("sync", {}).get("interval_seconds", 1200)))
    web_raw = raw.get("web", {})
    web = WebConfig(host=web_raw.get("host", "127.0.0.1"), port=int(web_raw.get("port", 8787)))

    cfg = Config(
        vault=vault,
        pocket=pocket,
        granola=granola,
        anthropic=anthropic,
        quality=quality,
        synthesis=synthesis,
        calendar=calendar,
        sync=sync,
        web=web,
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@lru_cache(maxsize=1)
def load_config() -> Config:
    return _load(_config_file())
