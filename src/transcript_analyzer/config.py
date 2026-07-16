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
class OllamaConfig:
    host: str
    chat_model: str
    embed_model: str
    timeout: int


@dataclass(frozen=True)
class SyncConfig:
    interval_seconds: int


@dataclass(frozen=True)
class TaxonomyConfig:
    seed: list[str]
    merge_threshold: float


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    vault: VaultConfig
    pocket: PocketConfig
    granola: GranolaConfig
    ollama: OllamaConfig
    sync: SyncConfig
    taxonomy: TaxonomyConfig
    web: WebConfig
    data_dir: Path = field(default=DATA_DIR)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def taxonomy_path(self) -> Path:
        return self.data_dir / "taxonomy.json"

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


@lru_cache(maxsize=1)
def load_config() -> Config:
    path = _config_file()
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
        api_base=granola_raw.get("api_base", "https://api.granola.ai"),
    )
    ollama_raw = raw["ollama"]
    ollama = OllamaConfig(
        host=ollama_raw.get("host", "http://localhost:11434"),
        chat_model=ollama_raw["chat_model"],
        embed_model=ollama_raw["embed_model"],
        timeout=int(ollama_raw.get("timeout", 600)),
    )
    sync = SyncConfig(interval_seconds=int(raw.get("sync", {}).get("interval_seconds", 1200)))
    tax_raw = raw.get("taxonomy", {})
    taxonomy = TaxonomyConfig(
        seed=list(tax_raw.get("seed", [])),
        merge_threshold=float(tax_raw.get("merge_threshold", 0.82)),
    )
    web_raw = raw.get("web", {})
    web = WebConfig(host=web_raw.get("host", "127.0.0.1"), port=int(web_raw.get("port", 8787)))

    cfg = Config(
        vault=vault,
        pocket=pocket,
        granola=granola,
        ollama=ollama,
        sync=sync,
        taxonomy=taxonomy,
        web=web,
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg
