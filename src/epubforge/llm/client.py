"""OpenAI-compatible httpx client with disk-level request caching.

Implement in epubforge-rmp.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from epubforge.config import Config


class LLMClient:
    """Thin wrapper around an OpenAI-compatible chat endpoint."""

    def __init__(self, cfg: Config, *, use_vlm: bool = False) -> None:
        self.base_url = cfg.vlm_base_url if use_vlm else cfg.llm_base_url
        self.api_key = cfg.vlm_api_key if use_vlm else cfg.llm_api_key
        self.model = cfg.vlm_model if use_vlm else cfg.llm_model
        self.cache_dir = cfg.cache_dir

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> str:
        raise NotImplementedError("TODO: implement in epubforge-rmp")

    def _cache_key(self, payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"
