from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    llm_max_tokens: int | None = None
    vlm_max_tokens: int | None = None
    llm_extra_body: dict[str, Any] = field(default_factory=dict)
    vlm_extra_body: dict[str, Any] = field(default_factory=dict)
    llm_prompt_caching: bool = True
    vlm_prompt_caching: bool = True
    concurrency: int = 4
    cache_dir: Path = field(default_factory=lambda: Path("work/.cache"))
    work_dir: Path = field(default_factory=lambda: Path("work"))
    out_dir: Path = field(default_factory=lambda: Path("out"))
    proofread_phase1_thinking_budget_tokens: int = 2000
    proofread_phase2_thinking_budget_tokens: int = 2000
    proofread_max_chunk_tokens: int = 100_000
    proofread_chars_per_token: float = 3.0
    footnote_verify_thinking_budget_tokens: int = 2000
    footnote_verify_max_chapter_tokens: int = 100_000
    footnote_verify_chars_per_token: float = 3.0
    footnote_verify_model: str = ""  # empty = use cfg.llm_model
    footnote_verify_providers: list[str] = field(default_factory=list)  # OpenRouter provider order
    vlm_dpi: int = 200
    max_simple_batch_pages: int = 8
    max_complex_batch_pages: int = 12
    enable_book_memory: bool = True

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


def load_config(config_path: Path | None = None) -> Config:
    # Layer 1: built-in defaults (via dataclass defaults)
    cfg = Config()

    # Layer 2+3: explicit path OR config.toml then config.local.toml
    toml_paths = (config_path,) if config_path else (Path("config.toml"), Path("config.local.toml"))
    for toml_path in toml_paths:
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
            if "max_tokens" in llm:
                cfg.llm_max_tokens = int(llm["max_tokens"])  # type: ignore[arg-type]
            if "extra_body" in llm and isinstance(llm["extra_body"], dict):
                cfg.llm_extra_body = dict(llm["extra_body"])  # type: ignore[arg-type]
            if "prompt_caching" in llm:
                cfg.llm_prompt_caching = bool(llm["prompt_caching"])

        if isinstance(vlm, dict):
            if "base_url" in vlm:
                cfg.vlm_base_url = str(vlm["base_url"])
            if "api_key" in vlm:
                cfg.vlm_api_key = str(vlm["api_key"])
            if "model" in vlm:
                cfg.vlm_model = str(vlm["model"])
            if "timeout_seconds" in vlm:
                cfg.vlm_timeout = float(vlm["timeout_seconds"])  # type: ignore[arg-type]
            if "max_tokens" in vlm:
                cfg.vlm_max_tokens = int(vlm["max_tokens"])  # type: ignore[arg-type]
            if "extra_body" in vlm and isinstance(vlm["extra_body"], dict):
                cfg.vlm_extra_body = dict(vlm["extra_body"])  # type: ignore[arg-type]
            if "prompt_caching" in vlm:
                cfg.vlm_prompt_caching = bool(vlm["prompt_caching"])

        if isinstance(rt, dict):
            if "concurrency" in rt:
                cfg.concurrency = int(rt["concurrency"])  # type: ignore[arg-type]
            if "cache_dir" in rt:
                cfg.cache_dir = Path(str(rt["cache_dir"]))
            if "work_dir" in rt:
                cfg.work_dir = Path(str(rt["work_dir"]))
            if "out_dir" in rt:
                cfg.out_dir = Path(str(rt["out_dir"]))

        pr = data.get("proofread") or {}
        if isinstance(pr, dict):
            if "phase1_thinking_budget_tokens" in pr:
                cfg.proofread_phase1_thinking_budget_tokens = int(pr["phase1_thinking_budget_tokens"])  # type: ignore[arg-type]
            if "phase2_thinking_budget_tokens" in pr:
                cfg.proofread_phase2_thinking_budget_tokens = int(pr["phase2_thinking_budget_tokens"])  # type: ignore[arg-type]
            if "max_chunk_tokens" in pr:
                cfg.proofread_max_chunk_tokens = int(pr["max_chunk_tokens"])  # type: ignore[arg-type]
            if "chars_per_token" in pr:
                cfg.proofread_chars_per_token = float(pr["chars_per_token"])  # type: ignore[arg-type]

        fv = data.get("footnote_verify") or {}
        if isinstance(fv, dict):
            if "thinking_budget_tokens" in fv:
                cfg.footnote_verify_thinking_budget_tokens = int(fv["thinking_budget_tokens"])  # type: ignore[arg-type]
            if "max_chapter_tokens" in fv:
                cfg.footnote_verify_max_chapter_tokens = int(fv["max_chapter_tokens"])  # type: ignore[arg-type]
            if "chars_per_token" in fv:
                cfg.footnote_verify_chars_per_token = float(fv["chars_per_token"])  # type: ignore[arg-type]
            if "model" in fv:
                cfg.footnote_verify_model = str(fv["model"])
            if "providers" in fv:
                cfg.footnote_verify_providers = [str(p) for p in fv["providers"]]  # type: ignore[union-attr]

        ex = data.get("extract") or {}
        if isinstance(ex, dict):
            if "vlm_dpi" in ex:
                cfg.vlm_dpi = int(ex["vlm_dpi"])  # type: ignore[arg-type]
            if "max_simple_batch_pages" in ex:
                cfg.max_simple_batch_pages = int(ex["max_simple_batch_pages"])  # type: ignore[arg-type]
            if "max_complex_batch_pages" in ex:
                cfg.max_complex_batch_pages = int(ex["max_complex_batch_pages"])  # type: ignore[arg-type]
            if "enable_book_memory" in ex:
                cfg.enable_book_memory = bool(ex["enable_book_memory"])

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
    if v := os.environ.get("EPUBFORGE_LLM_MAX_TOKENS"):
        cfg.llm_max_tokens = int(v)
    if v := os.environ.get("EPUBFORGE_VLM_MAX_TOKENS"):
        cfg.vlm_max_tokens = int(v)
    if v := os.environ.get("EPUBFORGE_LLM_PROMPT_CACHING"):
        cfg.llm_prompt_caching = v.lower() not in {"0", "false", "no"}
    if v := os.environ.get("EPUBFORGE_VLM_PROMPT_CACHING"):
        cfg.vlm_prompt_caching = v.lower() not in {"0", "false", "no"}
    if v := os.environ.get("EPUBFORGE_CONCURRENCY"):
        cfg.concurrency = int(v)
    if v := os.environ.get("EPUBFORGE_CACHE_DIR"):
        cfg.cache_dir = Path(v)
    if v := os.environ.get("EPUBFORGE_VLM_DPI"):
        cfg.vlm_dpi = int(v)
    if v := os.environ.get("EPUBFORGE_MAX_SIMPLE_BATCH_PAGES"):
        cfg.max_simple_batch_pages = int(v)
    if v := os.environ.get("EPUBFORGE_MAX_COMPLEX_BATCH_PAGES"):
        cfg.max_complex_batch_pages = int(v)
    if v := os.environ.get("EPUBFORGE_ENABLE_BOOK_MEMORY"):
        cfg.enable_book_memory = v.lower() not in {"0", "false", "no"}

    # vlm falls back to llm when not explicitly set
    if not cfg.vlm_base_url:
        cfg.vlm_base_url = cfg.llm_base_url
    if not cfg.vlm_api_key:
        cfg.vlm_api_key = cfg.llm_api_key

    return cfg
