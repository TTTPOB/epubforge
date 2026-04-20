"""OpenAI-compatible httpx client with disk-level request caching."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from epubforge.config import Config


class LLMClient:
    """Thin wrapper around an OpenAI-compatible chat endpoint."""

    def __init__(self, cfg: Config, *, use_vlm: bool = False) -> None:
        self.base_url = (cfg.vlm_base_url if use_vlm else cfg.llm_base_url).rstrip("/")
        self.api_key = cfg.vlm_api_key if use_vlm else cfg.llm_api_key
        self.model = cfg.vlm_model if use_vlm else cfg.llm_model
        self.cache_dir = cfg.cache_dir

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = 0.0,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format:
            payload["response_format"] = response_format

        cache_key = self._cache_key({"base_url": self.base_url, "payload": payload})
        cache_path = self._cache_path(cache_key)

        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))["content"]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=300.0,
        )
        resp.raise_for_status()
        content: str = resp.json()["choices"][0]["message"]["content"]

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"content": content}), encoding="utf-8")

        return content

    def _cache_key(self, payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"
