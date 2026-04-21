"""OpenAI-SDK client with typed structured outputs and disk-level request caching."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, TypeVar, cast

from openai import BadRequestError, OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel

from epubforge.config import Config

# Re-export so existing `from epubforge.llm.client import Message` imports stay valid.
Message = ChatCompletionMessageParam

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)


class LLMClient:
    """Thin wrapper around an OpenAI-compatible chat endpoint with Pydantic parsing."""

    def __init__(self, cfg: Config, *, use_vlm: bool = False) -> None:
        self.base_url = (cfg.vlm_base_url if use_vlm else cfg.llm_base_url).rstrip("/")
        self.model = cfg.vlm_model if use_vlm else cfg.llm_model
        self.timeout = cfg.vlm_timeout if use_vlm else cfg.llm_timeout
        self.max_tokens = cfg.vlm_max_tokens if use_vlm else cfg.llm_max_tokens
        if use_vlm and self.max_tokens is None:
            self.max_tokens = 16384
        self.cache_dir = cfg.cache_dir
        api_key = cfg.vlm_api_key if use_vlm else cfg.llm_api_key
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=api_key,
            timeout=self.timeout,
            max_retries=2,
        )

    def chat_parsed(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        response_format: type[T],
        temperature: float | None = 0.0,
    ) -> T:
        cache_key = self._cache_key(messages, response_format, temperature)
        cache_path = self._cache_path(cache_key)
        if cache_path.exists():
            raw = json.loads(cache_path.read_text(encoding="utf-8"))["content"]
            return response_format.model_validate_json(raw)

        parsed = self._call_parsed(messages, response_format, temperature)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"content": parsed.model_dump_json()}),
            encoding="utf-8",
        )
        return parsed

    def _call_parsed(
        self,
        messages: list[ChatCompletionMessageParam],
        response_format: type[T],
        temperature: float | None,
    ) -> T:
        """Try OpenAI structured outputs; fall back to json_object mode on 400.

        Automatically retries with doubled max_tokens if the response is truncated
        (finish_reason == "length"), up to 3 attempts total (max 65536 tokens).
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": response_format,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature

        budget = self.max_tokens
        if budget is not None:
            kwargs["max_tokens"] = budget

        for _attempt in range(3):
            try:
                completion = self._client.chat.completions.parse(**kwargs)
            except BadRequestError as exc:
                if exc.status_code != 400 or "response_format" not in str(exc):
                    raise
                log.warning(
                    "endpoint rejected json_schema response_format (400); "
                    "falling back to json_object mode (no strict schema)"
                )
                return self._call_json_object_fallback(messages, response_format, temperature)

            finish_reason = completion.choices[0].finish_reason
            message = completion.choices[0].message
            parsed = message.parsed

            if finish_reason == "length":
                new_budget = min((budget or 8192) * 2, 65536)
                log.warning(
                    "structured output truncated; retrying with max_tokens=%d", new_budget
                )
                budget = new_budget
                kwargs["max_tokens"] = new_budget
                continue

            if parsed is None:
                raise RuntimeError(
                    f"LLM returned no parsed content for {response_format.__name__}: "
                    f"refusal={message.refusal!r}"
                )
            return parsed

        raise RuntimeError(
            f"Structured output for {response_format.__name__} was truncated after 3 attempts"
        )

    def _call_json_object_fallback(
        self,
        messages: list[ChatCompletionMessageParam],
        response_format: type[T],
        temperature: float | None,
    ) -> T:
        """Fallback: json_object mode — model must emit valid JSON, parsed manually.

        Retries with doubled max_tokens if the response is truncated (finish_reason==length
        or ValidationError due to EOF), up to 3 attempts total.
        """
        from pydantic import ValidationError as PydanticValidationError

        budget = self.max_tokens or 8192
        fallback_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": budget,
        }
        if temperature is not None:
            fallback_kwargs["temperature"] = temperature

        for _attempt in range(3):
            completion = cast(
                ChatCompletion, self._client.chat.completions.create(**fallback_kwargs)
            )
            finish_reason = completion.choices[0].finish_reason
            content = completion.choices[0].message.content or ""

            if finish_reason == "length":
                budget = min(budget * 2, 65536)
                log.warning(
                    "json_object response truncated; retrying with max_tokens=%d", budget
                )
                fallback_kwargs["max_tokens"] = budget
                continue

            # Strip markdown fences the model may add
            content = re.sub(r"^```(?:json)?\s*", "", content.strip())
            content = re.sub(r"\s*```$", "", content)

            try:
                return response_format.model_validate_json(content)
            except PydanticValidationError as exc:
                if "EOF" in str(exc) and _attempt < 2:
                    budget = min(budget * 2, 65536)
                    log.warning(
                        "json_object response appears truncated (EOF); retrying with max_tokens=%d",
                        budget,
                    )
                    fallback_kwargs["max_tokens"] = budget
                    continue
                raise

        raise RuntimeError(
            f"json_object response for {response_format.__name__} was truncated after 3 attempts"
        )

    def _cache_key(
        self,
        messages: list[ChatCompletionMessageParam],
        response_format: type[BaseModel],
        temperature: float | None,
    ) -> str:
        key_obj: dict[str, Any] = {
            "base_url": self.base_url,
            "model": self.model,
            "messages": messages,
            "response_format": {
                "name": response_format.__name__,
                "schema": response_format.model_json_schema(),
            },
        }
        if temperature is not None:
            key_obj["temperature"] = temperature
        blob = json.dumps(key_obj, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"
