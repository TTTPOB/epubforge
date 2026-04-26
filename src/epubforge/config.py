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
    """Settings for a single LLM/VLM provider endpoint."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str | None = None
    model: str = "anthropic/claude-haiku-4.5"
    timeout_seconds: float = 300.0
    max_tokens: int | None = None
    prompt_caching: bool = True
    extra_body: dict[str, Any] = Field(default_factory=dict)


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


class OcrSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    force_full_page_ocr: bool = True
    ocr_version: str = "PP-OCRv5"
    model_type: str = "mobile"
    backend: str = "onnxruntime"
    text_score: float = 0.5
    bitmap_area_threshold: float = 0.05


class GraniteSettings(BaseModel):
    """Granite-Docling-258M VLM via llama-server (OpenAI-compatible API).

    Off by default. When `enabled=True`, parse stage runs Granite as a
    secondary pipeline alongside the standard Docling+OCR primary; the
    output is persisted as 01_raw_granite.json next to 01_raw.json.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_url: str = "http://localhost:8080/v1/chat/completions"
    api_model: str = "granite-docling"
    prompt: str = "Convert this page to docling."
    scale: float = 2.0
    timeout_seconds: int = 180
    max_tokens: int = 4096
    health_check: bool = True
    # Concurrency MUST be 1 for default llama-server -np 1 config on 8GB WSL2.
    # Increase only if llama-server -np N is configured to match.
    concurrency: int = 1


class ExtractSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_book_memory: bool = True
    # Stage 1 traditional pipeline batches the PDF into chunks of this many
    # pages before calling DocumentConverter.convert. With OCR enabled, a
    # single 50-page convert peaks above 5 GiB RSS on 8 GiB WSL2; batching
    # keeps peak memory bounded by per-batch cost. Default 20 pages.
    page_batch_size: int = 20
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    granite: GraniteSettings = Field(default_factory=GraniteSettings)


# ---------------------------------------------------------------------------
# Top-level Config — extra="ignore" so unknown env vars don't raise
# ---------------------------------------------------------------------------


class Config(BaseSettings):
    """Top-level application configuration assembled from defaults + TOML + env."""

    model_config = SettingsConfigDict(extra="ignore")

    llm: ProviderSettings = Field(default_factory=ProviderSettings)
    vlm: ProviderSettings = Field(
        default_factory=lambda: ProviderSettings(
            model="google/gemini-flash-3", max_tokens=16384
        )
    )
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    editor: EditorSettings = Field(default_factory=EditorSettings)
    extract: ExtractSettings = Field(default_factory=ExtractSettings)

    def require_llm(self) -> None:
        if not self.llm.api_key:
            raise SystemExit(
                "LLM API key is required (set [llm].api_key or EPUBFORGE_LLM_API_KEY)"
            )

    def require_vlm(self) -> None:
        resolved = self.resolved_vlm()
        if not resolved.api_key:
            raise SystemExit(
                "VLM API key is required (set [vlm].api_key or EPUBFORGE_VLM_API_KEY)"
            )

    def resolved_vlm(self) -> ProviderSettings:
        """Return effective VLM settings, falling back to LLM for api_key.

        api_key: vlm.api_key if not None, else llm.api_key
        base_url: vlm.base_url (defaults to same as llm default; override via [vlm] or env)
        All other fields: taken directly from vlm.
        """
        return ProviderSettings(
            base_url=self.vlm.base_url,
            api_key=self.vlm.api_key
            if self.vlm.api_key is not None
            else self.llm.api_key,
            model=self.vlm.model,
            timeout_seconds=self.vlm.timeout_seconds,
            max_tokens=self.vlm.max_tokens,
            prompt_caching=self.vlm.prompt_caching,
            extra_body=self.vlm.extra_body,
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
    ("EPUBFORGE_VLM_BASE_URL", "vlm", "base_url", str),
    ("EPUBFORGE_VLM_API_KEY", "vlm", "api_key", str),
    ("EPUBFORGE_VLM_MODEL", "vlm", "model", str),
    ("EPUBFORGE_VLM_TIMEOUT", "vlm", "timeout_seconds", float),
    (
        "EPUBFORGE_VLM_MAX_TOKENS",
        "vlm",
        "max_tokens",
        lambda v: None if v == "" else int(v),
    ),
    ("EPUBFORGE_VLM_PROMPT_CACHING", "vlm", "prompt_caching", _bool_env),
    ("EPUBFORGE_RUNTIME_CONCURRENCY", "runtime", "concurrency", int),
    ("EPUBFORGE_RUNTIME_CACHE_DIR", "runtime", "cache_dir", Path),
    ("EPUBFORGE_RUNTIME_WORK_DIR", "runtime", "work_dir", Path),
    ("EPUBFORGE_RUNTIME_OUT_DIR", "runtime", "out_dir", Path),
    ("EPUBFORGE_RUNTIME_LOG_LEVEL", "runtime", "log_level", str),
    ("EPUBFORGE_EDITOR_COMPACT_THRESHOLD", "editor", "compact_threshold", int),
    ("EPUBFORGE_EDITOR_MAX_LOOPS", "editor", "max_loops", int),
    ("EPUBFORGE_ENABLE_BOOK_MEMORY", "extract", "enable_book_memory", _bool_env),
    ("EPUBFORGE_EXTRACT_PAGE_BATCH_SIZE", "extract", "page_batch_size", int),
    ("EPUBFORGE_EXTRACT_OCR_ENABLED", "extract.ocr", "enabled", _bool_env),
    ("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "extract.granite", "enabled", _bool_env),
    ("EPUBFORGE_EXTRACT_GRANITE_API_URL", "extract.granite", "api_url", str),
    ("EPUBFORGE_EXTRACT_GRANITE_API_MODEL", "extract.granite", "api_model", str),
    ("EPUBFORGE_EXTRACT_GRANITE_TIMEOUT", "extract.granite", "timeout_seconds", int),
]

_SECTION_MODELS = {
    "llm": ProviderSettings,
    "vlm": ProviderSettings,
    "runtime": RuntimeSettings,
    "editor": EditorSettings,
    "extract": ExtractSettings,
}


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
        for key in ("llm", "vlm", "runtime", "editor", "extract"):
            if key in toml_data:
                base[key] = dict(toml_data[key])

    _apply_env_overrides(base)

    # Rebuild each section as the appropriate submodel so unknown keys raise early.
    # Top-level Config with extra="ignore" is intentional for env robustness.
    return Config(**base)
