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


# Per-stage model and effort defaults. A stage names a job, not a model, so
# the expensive study-notes pass and the cheap bulk extraction can be retuned
# (or repointed at a new model) without touching call sites. Empty string =
# "use [anthropic] model" for a model, and "send no effort at all" for effort
# — which is what pre-4.6 models require, since they reject the parameter.
DEFAULT_STAGE_MODELS: dict[str, str] = {
    "lecture": "claude-opus-5",   # study notes + diagram specs: the quality bet
    "backfill": "claude-sonnet-5",  # bulk re-summarization of the whole vault
}
# Thinking is ON BY DEFAULT on Opus 5 and billed as output tokens, so the
# stages that only fill in a schema are pinned low rather than paying for
# reasoning they do not need.
DEFAULT_STAGE_EFFORT: dict[str, str] = {
    "extract": "low",
    "backfill": "low",
    "lecture": "medium",
}


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
    # Per-stage overrides; see DEFAULT_STAGE_MODELS / DEFAULT_STAGE_EFFORT.
    stage_models: dict = field(default_factory=lambda: dict(DEFAULT_STAGE_MODELS))
    stage_effort: dict = field(default_factory=lambda: dict(DEFAULT_STAGE_EFFORT))
    # Output cap for the study-notes pass, which writes far more than a
    # summary does. Streamed, so it is not bounded by the HTTP timeout.
    lecture_max_tokens: int = 32000


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
class LectureConfig:
    """The lecture profile: study notes + a rendered PDF for recordings the
    extraction pass classifies as `kind: lecture`.

    Detection is the model's `kind` field alone — the captain declined a course
    registry, so nothing here names a course.
    """

    enabled: bool = True
    # Render the PDF. Needs the Playwright Chromium cache; when it is missing
    # the markdown study note is still written and the PDF is skipped.
    pdf: bool = True
    # Diagram budget. Under the minimum the prompt is not satisfied, but a
    # lecture that genuinely has nothing to draw still gets its notes.
    min_visuals: int = 2
    max_visuals: int = 5


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
    lecture: LectureConfig = field(default_factory=LectureConfig)
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


def _stage_overrides(defaults: dict[str, str], raw) -> dict[str, str]:
    """Config values layered over the built-in stage defaults.

    A stage the user does not mention keeps its default; one they set to ""
    explicitly falls back to [anthropic] model (or sends no effort), which is
    the escape hatch for a model that rejects the parameter.
    """
    out = dict(defaults)
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = str(v)
    return out


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
        stage_models=_stage_overrides(
            DEFAULT_STAGE_MODELS, anthropic_raw.get("stage_models", {})
        ),
        stage_effort=_stage_overrides(
            DEFAULT_STAGE_EFFORT, anthropic_raw.get("stage_effort", {})
        ),
        lecture_max_tokens=int(anthropic_raw.get("lecture_max_tokens", 32000)),
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
    lecture_raw = raw.get("lecture", {})
    lecture = LectureConfig(
        enabled=bool(lecture_raw.get("enabled", True)),
        pdf=bool(lecture_raw.get("pdf", True)),
        min_visuals=int(lecture_raw.get("min_visuals", 2)),
        max_visuals=int(lecture_raw.get("max_visuals", 5)),
    )
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
        lecture=lecture,
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@lru_cache(maxsize=1)
def load_config() -> Config:
    return _load(_config_file())
