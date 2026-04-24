"""OpenAI-SDK client with typed structured outputs and disk-level request caching."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from openai import BadRequestError, OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel

from epubforge.config import Config
from epubforge.observability import get_tracker

# Re-export so existing `from epubforge.llm.client import Message` imports stay valid.
Message = ChatCompletionMessageParam

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)


@dataclass
class _CallResult:
    parsed: Any
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    cached_tokens: int = 0


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base; override wins on conflicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _apply_cache_control(
    messages: list[ChatCompletionMessageParam],
    *,
    enabled: bool,
) -> list[ChatCompletionMessageParam]:
    """Attach cache_control: ephemeral to the system message (Anthropic-style).

    Called AFTER cache-key computation so on-disk cache keys stay stable
    when the flag is toggled.
    """
    if not enabled:
        return messages
    out: list[ChatCompletionMessageParam] = []
    for msg in messages:
        if msg.get("role") != "system":
            out.append(msg)
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            blocks: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        elif isinstance(content, list) and content:
            blocks = [dict(b) for b in content]
            for b in reversed(blocks):
                if b.get("type") == "text":
                    b["cache_control"] = {"type": "ephemeral"}
                    break
            else:
                out.append(msg)
                continue
        else:
            out.append(msg)
            continue
        out.append(cast(ChatCompletionMessageParam, {**msg, "content": blocks}))
    return out


def _count_chars(messages: list[ChatCompletionMessageParam]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(part.get("text", ""))
    return total


def _count_images(messages: list[ChatCompletionMessageParam]) -> int:
    count = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    count += 1
    return count


class LLMClient:
    """Thin wrapper around an OpenAI-compatible chat endpoint with Pydantic parsing."""

    def __init__(self, cfg: Config, *, use_vlm: bool = False) -> None:
        provider = cfg.resolved_vlm() if use_vlm else cfg.llm
        self.base_url = provider.base_url.rstrip("/")
        self.model = provider.model
        self._kind = "VLM" if use_vlm else "LLM"
        self.timeout = provider.timeout_seconds
        self.max_tokens = provider.max_tokens
        self.extra_body: dict[str, Any] = provider.extra_body
        self.prompt_caching = provider.prompt_caching
        self.cache_dir = cfg.runtime.cache_dir
        api_key = provider.api_key
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=api_key or "",
            timeout=self.timeout,
            max_retries=2,
        )

    def chat_parsed(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        response_format: type[T],
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> T:
        merged_extra = _deep_merge(self.extra_body, extra_body or {})
        cache_key = self._cache_key(
            messages, response_format, temperature, merged_extra
        )
        cache_path = self._cache_path(cache_key)
        req_id = cache_key[:8]
        chars = _count_chars(messages)
        images = _count_images(messages)

        log.info(
            "%s req=%s model=%s fmt=%s msgs=%d chars=%d images=%d",
            self._kind,
            req_id,
            self.model,
            response_format.__name__,
            len(messages),
            chars,
            images,
        )

        if cache_path.exists():
            raw = json.loads(cache_path.read_text(encoding="utf-8"))["content"]
            log.info("%s req=%s cache HIT", self._kind, req_id)
            get_tracker().record_hit()
            return response_format.model_validate_json(raw)

        t0 = time.perf_counter()
        sdk_messages = _apply_cache_control(messages, enabled=self.prompt_caching)
        result = self._call_parsed(
            sdk_messages, response_format, temperature, merged_extra, req_id=req_id
        )
        elapsed = time.perf_counter() - t0

        log.info(
            "%s req=%s cache MISS elapsed=%.2fs finish=%s usage=%dp+%dc cached=%d",
            self._kind,
            req_id,
            elapsed,
            result.finish_reason,
            result.prompt_tokens,
            result.completion_tokens,
            result.cached_tokens,
        )
        get_tracker().record_miss(
            prompt=result.prompt_tokens,
            completion=result.completion_tokens,
            elapsed=elapsed,
            cached=result.cached_tokens,
        )

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        parsed = result.parsed
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
        merged_extra: dict[str, Any],
        req_id: str = "????????",
    ) -> _CallResult:
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
        if merged_extra:
            kwargs["extra_body"] = merged_extra

        budget = self.max_tokens
        if budget is not None:
            kwargs["max_tokens"] = budget

        for attempt in range(3):
            try:
                completion = self._client.chat.completions.parse(**kwargs)
            except BadRequestError as exc:
                if exc.status_code != 400 or "response_format" not in str(exc):
                    raise
                log.warning(
                    "req=%s attempt=%d/3 endpoint rejected json_schema response_format (400); "
                    "falling back to json_object mode (no strict schema)",
                    req_id,
                    attempt + 1,
                )
                return self._call_json_object_fallback(
                    messages, response_format, temperature, merged_extra, req_id=req_id
                )

            finish_reason = completion.choices[0].finish_reason
            message = completion.choices[0].message
            parsed = message.parsed
            usage = completion.usage

            if finish_reason == "length":
                new_budget = min((budget or 8192) * 2, 65536)
                log.warning(
                    "req=%s attempt=%d/3 structured output truncated; retrying with max_tokens=%d",
                    req_id,
                    attempt + 1,
                    new_budget,
                )
                budget = new_budget
                kwargs["max_tokens"] = new_budget
                continue

            if parsed is None:
                raise RuntimeError(
                    f"LLM returned no parsed content for {response_format.__name__}: "
                    f"refusal={message.refusal!r}"
                )

            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0
            details = getattr(usage, "prompt_tokens_details", None) if usage else None
            cached_tokens = (
                int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            )
            return _CallResult(
                parsed=parsed,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                finish_reason=finish_reason or "stop",
                cached_tokens=cached_tokens,
            )

        raise RuntimeError(
            f"Structured output for {response_format.__name__} was truncated after 3 attempts"
        )

    def _call_json_object_fallback(
        self,
        messages: list[ChatCompletionMessageParam],
        response_format: type[T],
        temperature: float | None,
        merged_extra: dict[str, Any],
        req_id: str = "????????",
    ) -> _CallResult:
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
        if merged_extra:
            fallback_kwargs["extra_body"] = merged_extra

        for attempt in range(3):
            completion = cast(
                ChatCompletion, self._client.chat.completions.create(**fallback_kwargs)
            )
            finish_reason = completion.choices[0].finish_reason
            content = completion.choices[0].message.content or ""
            usage = completion.usage

            if finish_reason == "length":
                budget = min(budget * 2, 65536)
                log.warning(
                    "req=%s attempt=%d/3 json_object response truncated; retrying with max_tokens=%d",
                    req_id,
                    attempt + 1,
                    budget,
                )
                fallback_kwargs["max_tokens"] = budget
                continue

            # Strip markdown fences the model may add
            content = re.sub(r"^```(?:json)?\s*", "", content.strip())
            content = re.sub(r"\s*```$", "", content)

            try:
                parsed = response_format.model_validate_json(content)
                prompt_tokens = usage.prompt_tokens if usage else 0
                completion_tokens = usage.completion_tokens if usage else 0
                details = (
                    getattr(usage, "prompt_tokens_details", None) if usage else None
                )
                cached_tokens = (
                    int(getattr(details, "cached_tokens", 0) or 0) if details else 0
                )
                return _CallResult(
                    parsed=parsed,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    finish_reason=finish_reason or "stop",
                    cached_tokens=cached_tokens,
                )
            except PydanticValidationError as exc:
                if "EOF" in str(exc) and attempt < 2:
                    budget = min(budget * 2, 65536)
                    log.warning(
                        "req=%s attempt=%d/3 json_object response appears truncated (EOF); "
                        "retrying with max_tokens=%d",
                        req_id,
                        attempt + 1,
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
        merged_extra: dict[str, Any],
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
        if merged_extra:
            key_obj["extra_body"] = merged_extra
        blob = json.dumps(key_obj, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"
