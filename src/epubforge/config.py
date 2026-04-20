from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _load_toml(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


@dataclass
class Config:
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "anthropic/claude-haiku-4.5"
    vlm_base_url: str = ""
    vlm_api_key: str = ""
    vlm_model: str = "google/gemini-flash-3"
    llm_timeout: float = 300.0
    vlm_timeout: float = 300.0
    concurrency: int = 4
    cache_dir: Path = field(default_factory=lambda: Path("work/.cache"))
    work_dir: Path = field(default_factory=lambda: Path("work"))
    out_dir: Path = field(default_factory=lambda: Path("out"))

    def require_llm(self) -> None:
        if not self.llm_api_key:
            raise SystemExit("LLM API key is required (set [llm].api_key or EPUBFORGE_LLM_API_KEY)")

    def require_vlm(self) -> None:
        if not self.vlm_api_key:
            raise SystemExit("VLM API key is required (set [vlm].api_key or EPUBFORGE_VLM_API_KEY)")

    def book_work_dir(self, pdf_path: Path) -> Path:
        return self.work_dir / pdf_path.stem

    def book_out_path(self, pdf_path: Path) -> Path:
        return self.out_dir / f"{pdf_path.stem}.epub"


def load_config() -> Config:
    # Layer 1: built-in defaults (via dataclass defaults)
    cfg = Config()

    # Layer 2+3: config.toml then config.local.toml
    for toml_path in (Path("config.toml"), Path("config.local.toml")):
        data = _load_toml(toml_path)
        llm = data.get("llm") or {}
        vlm = data.get("vlm") or {}
        rt = data.get("runtime") or {}

        if isinstance(llm, dict):
            if "base_url" in llm:
                cfg.llm_base_url = str(llm["base_url"])
            if "api_key" in llm:
                cfg.llm_api_key = str(llm["api_key"])
            if "model" in llm:
                cfg.llm_model = str(llm["model"])
            if "timeout_seconds" in llm:
                cfg.llm_timeout = float(llm["timeout_seconds"])  # type: ignore[arg-type]

        if isinstance(vlm, dict):
            if "base_url" in vlm:
                cfg.vlm_base_url = str(vlm["base_url"])
            if "api_key" in vlm:
                cfg.vlm_api_key = str(vlm["api_key"])
            if "model" in vlm:
                cfg.vlm_model = str(vlm["model"])
            if "timeout_seconds" in vlm:
                cfg.vlm_timeout = float(vlm["timeout_seconds"])  # type: ignore[arg-type]

        if isinstance(rt, dict):
            if "concurrency" in rt:
                cfg.concurrency = int(rt["concurrency"])  # type: ignore[arg-type]
            if "cache_dir" in rt:
                cfg.cache_dir = Path(str(rt["cache_dir"]))
            if "work_dir" in rt:
                cfg.work_dir = Path(str(rt["work_dir"]))
            if "out_dir" in rt:
                cfg.out_dir = Path(str(rt["out_dir"]))

    # Layer 4: environment variables (highest priority)
    if v := os.environ.get("EPUBFORGE_LLM_BASE_URL"):
        cfg.llm_base_url = v
    if v := os.environ.get("EPUBFORGE_LLM_API_KEY"):
        cfg.llm_api_key = v
    if v := os.environ.get("EPUBFORGE_LLM_MODEL"):
        cfg.llm_model = v
    if v := os.environ.get("EPUBFORGE_VLM_BASE_URL"):
        cfg.vlm_base_url = v
    if v := os.environ.get("EPUBFORGE_VLM_API_KEY"):
        cfg.vlm_api_key = v
    if v := os.environ.get("EPUBFORGE_VLM_MODEL"):
        cfg.vlm_model = v
    if v := os.environ.get("EPUBFORGE_LLM_TIMEOUT"):
        cfg.llm_timeout = float(v)
    if v := os.environ.get("EPUBFORGE_VLM_TIMEOUT"):
        cfg.vlm_timeout = float(v)
    if v := os.environ.get("EPUBFORGE_CONCURRENCY"):
        cfg.concurrency = int(v)
    if v := os.environ.get("EPUBFORGE_CACHE_DIR"):
        cfg.cache_dir = Path(v)

    # vlm falls back to llm when not explicitly set
    if not cfg.vlm_base_url:
        cfg.vlm_base_url = cfg.llm_base_url
    if not cfg.vlm_api_key:
        cfg.vlm_api_key = cfg.llm_api_key

    return cfg
