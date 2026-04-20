from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "anthropic/claude-haiku-4.5"
    vlm_base_url: str = ""
    vlm_api_key: str = ""
    vlm_model: str = "google/gemini-flash-3"
    concurrency: int = 4
    cache_dir: Path = field(default_factory=lambda: Path("work/.cache"))
    work_dir: Path = field(default_factory=lambda: Path("work"))
    out_dir: Path = field(default_factory=lambda: Path("out"))

    def require_llm(self) -> None:
        if not self.llm_api_key:
            raise SystemExit("EPUBFORGE_LLM_API_KEY is required for LLM stages")

    def require_vlm(self) -> None:
        if not self.vlm_api_key:
            raise SystemExit("EPUBFORGE_VLM_API_KEY is required for VLM stage")

    def book_work_dir(self, pdf_path: Path) -> Path:
        return self.work_dir / pdf_path.stem

    def book_out_path(self, pdf_path: Path) -> Path:
        return self.out_dir / f"{pdf_path.stem}.epub"


def load_config() -> Config:
    vlm_base_url = os.environ.get(
        "EPUBFORGE_VLM_BASE_URL",
        os.environ.get("EPUBFORGE_LLM_BASE_URL", "https://openrouter.ai/api/v1"),
    )
    return Config(
        llm_base_url=os.environ.get("EPUBFORGE_LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        llm_api_key=os.environ.get("EPUBFORGE_LLM_API_KEY", ""),
        llm_model=os.environ.get("EPUBFORGE_LLM_MODEL", "anthropic/claude-haiku-4.5"),
        vlm_base_url=vlm_base_url,
        vlm_api_key=os.environ.get("EPUBFORGE_VLM_API_KEY", os.environ.get("EPUBFORGE_LLM_API_KEY", "")),
        vlm_model=os.environ.get("EPUBFORGE_VLM_MODEL", "google/gemini-flash-3"),
        concurrency=int(os.environ.get("EPUBFORGE_CONCURRENCY", "4")),
        cache_dir=Path(os.environ.get("EPUBFORGE_CACHE_DIR", "work/.cache")),
        work_dir=Path("work"),
        out_dir=Path("out"),
    )
