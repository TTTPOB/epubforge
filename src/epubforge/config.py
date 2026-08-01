"""Epubforge configuration — pydantic-settings nested submodels with explicit env mapping."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Submodels — extra="forbid" so unknown TOML keys fail fast
# ---------------------------------------------------------------------------


class ProviderSettings(BaseModel):
    """Settings for the text LLM provider endpoint."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str | None = None
    model: str = "anthropic/claude-haiku-4.5"
    timeout_seconds: float = 300.0
    max_tokens: int | None = None
    prompt_caching: bool = True
    extra_body: dict[str, Any] = Field(default_factory=dict)


class MineruSettings(BaseModel):
    """Settings for the MinerU official cloud API."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    base_url: str = "https://mineru.net/api/v4"
    model_version: Literal["pipeline", "vlm"] = "vlm"
    timeout_seconds: float = Field(default=300.0, gt=0)
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    max_polls: int = Field(default=300, gt=0)
    max_download_bytes: int = Field(default=2 * 1024**3, gt=0)
    max_uncompressed_bytes: int = Field(default=8 * 1024**3, gt=0)
    is_ocr: bool = False
    enable_formula: bool = True
    enable_table: bool = True
    language: Literal["ch", "en", "korean", "japan"] = "ch"


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concurrency: int = 4
    cache_dir: Path = Path("work/.cache")
    work_dir: Path = Path("work")
    out_dir: Path = Path("out")
    log_level: Literal["DEBUG", "INFO", "WARNING"] = "INFO"


class EditorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compact_threshold: int = 50
    max_loops: int = 50


class ExtractSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_book_memory: bool = True


# ---------------------------------------------------------------------------
# Top-level Config — extra="ignore" so unknown env vars don't raise
# ---------------------------------------------------------------------------


class Config(BaseSettings):
    """Top-level application configuration assembled from defaults + TOML + env."""

    model_config = SettingsConfigDict(extra="ignore")

    llm: ProviderSettings = Field(default_factory=ProviderSettings)
    mineru: MineruSettings = Field(default_factory=MineruSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    editor: EditorSettings = Field(default_factory=EditorSettings)
    extract: ExtractSettings = Field(default_factory=ExtractSettings)

    def require_llm(self) -> None:
        if not self.llm.api_key:
            raise SystemExit(
                "LLM API key is required (set [llm].api_key or EPUBFORGE_LLM_API_KEY)"
            )

    def require_mineru(self) -> None:
        if not self.mineru.api_key:
            raise SystemExit(
                "MinerU API key is required "
                "(set [mineru].api_key or EPUBFORGE_MINERU_API_KEY)"
            )

    def book_work_dir(self, pdf_path: Path) -> Path:
        return self.runtime.work_dir / pdf_path.stem

    def book_out_path(self, pdf_path: Path) -> Path:
        return self.runtime.out_dir / f"{pdf_path.stem}.epub"


# ---------------------------------------------------------------------------
# Explicit env whitelist — maps env name → (section, field, cast_fn)
# No env_nested_delimiter; each entry is a deliberate leaf-level override.
# ---------------------------------------------------------------------------


def _bool_env(v: str) -> bool:
    return v.lower() in {"1", "true", "yes", "on"}


_ENV_MAP: list[tuple[str, str, str, Any]] = [
    # (env_name, section, field, cast)
    ("EPUBFORGE_LLM_BASE_URL", "llm", "base_url", str),
    ("EPUBFORGE_LLM_API_KEY", "llm", "api_key", str),
    ("EPUBFORGE_LLM_MODEL", "llm", "model", str),
    ("EPUBFORGE_LLM_TIMEOUT", "llm", "timeout_seconds", float),
    (
        "EPUBFORGE_LLM_MAX_TOKENS",
        "llm",
        "max_tokens",
        lambda v: None if v == "" else int(v),
    ),
    ("EPUBFORGE_LLM_PROMPT_CACHING", "llm", "prompt_caching", _bool_env),
    ("EPUBFORGE_MINERU_API_KEY", "mineru", "api_key", str),
    ("EPUBFORGE_MINERU_BASE_URL", "mineru", "base_url", str),
    ("EPUBFORGE_MINERU_MODEL_VERSION", "mineru", "model_version", str),
    ("EPUBFORGE_MINERU_TIMEOUT", "mineru", "timeout_seconds", float),
    (
        "EPUBFORGE_MINERU_POLL_INTERVAL_SECONDS",
        "mineru",
        "poll_interval_seconds",
        float,
    ),
    ("EPUBFORGE_MINERU_MAX_POLLS", "mineru", "max_polls", int),
    ("EPUBFORGE_MINERU_MAX_DOWNLOAD_BYTES", "mineru", "max_download_bytes", int),
    (
        "EPUBFORGE_MINERU_MAX_UNCOMPRESSED_BYTES",
        "mineru",
        "max_uncompressed_bytes",
        int,
    ),
    ("EPUBFORGE_MINERU_IS_OCR", "mineru", "is_ocr", _bool_env),
    ("EPUBFORGE_MINERU_ENABLE_FORMULA", "mineru", "enable_formula", _bool_env),
    ("EPUBFORGE_MINERU_ENABLE_TABLE", "mineru", "enable_table", _bool_env),
    ("EPUBFORGE_MINERU_LANGUAGE", "mineru", "language", str),
    ("EPUBFORGE_RUNTIME_CONCURRENCY", "runtime", "concurrency", int),
    ("EPUBFORGE_RUNTIME_CACHE_DIR", "runtime", "cache_dir", Path),
    ("EPUBFORGE_RUNTIME_WORK_DIR", "runtime", "work_dir", Path),
    ("EPUBFORGE_RUNTIME_OUT_DIR", "runtime", "out_dir", Path),
    ("EPUBFORGE_RUNTIME_LOG_LEVEL", "runtime", "log_level", str),
    ("EPUBFORGE_EDITOR_COMPACT_THRESHOLD", "editor", "compact_threshold", int),
    ("EPUBFORGE_EDITOR_MAX_LOOPS", "editor", "max_loops", int),
    ("EPUBFORGE_ENABLE_BOOK_MEMORY", "extract", "enable_book_memory", _bool_env),
]


def _apply_env_overrides(base: dict[str, Any]) -> dict[str, Any]:
    """Apply env vars as leaf-level overrides onto a nested dict scaffold.

    Leaf-merge: only the touched field changes; sibling fields are untouched.
    """
    for env_name, section, field, cast in _ENV_MAP:
        v = os.environ.get(env_name)
        if v is None:
            continue
        parts = section.split(".")
        section_data = base
        for part in parts:
            section_data = section_data.setdefault(part, {})
        section_data[field] = cast(v)
    return base


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from defaults + optional explicit TOML + env overrides.

    Args:
        config_path: If provided, read this TOML file (must exist).
                     If None: defaults + env only — does NOT scan cwd for
                     config.toml / config.local.toml.
    """
    base: dict[str, Any] = {}

    if config_path is not None:
        if not config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}. "
                "Specify a valid path via --config or omit the flag to use defaults + env only."
            )
        with config_path.open("rb") as fh:
            toml_data = tomllib.load(fh)
        # Accept only known top-level sections; unknown keys at the top level are silently
        # ignored (Config.model_config extra="ignore" handles this at parse time too).
        for key in ("llm", "mineru", "runtime", "editor", "extract"):
            if key in toml_data:
                base[key] = dict(toml_data[key])

    _apply_env_overrides(base)

    # Rebuild each section as the appropriate submodel so unknown keys raise early.
    # Top-level Config with extra="ignore" is intentional for env robustness.
    return Config(**base)
