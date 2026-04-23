"""Unit tests for LLMClient — truncation retry and json_object fallback."""

from __future__ import annotations

import json
import logging
from typing import Any, cast
from unittest.mock import MagicMock, patch, call

import pytest
from openai import BadRequestError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from epubforge.config import Config, ProviderSettings, RuntimeSettings
from epubforge.llm.client import LLMClient, _apply_cache_control
from epubforge.observability import get_tracker


class _DummyOutput(BaseModel):
    value: str = ""


def _make_client(tmp_path, *, use_vlm: bool = False) -> LLMClient:
    cfg = Config(
        llm=ProviderSettings(base_url="https://example.com/v1", api_key="test-key"),
        runtime=RuntimeSettings(cache_dir=tmp_path / ".cache"),
    )
    return LLMClient(cfg, use_vlm=use_vlm)


def _make_usage(prompt: int = 10, completion: int = 5) -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    return usage


def _make_completion(parsed: Any, finish_reason: str = "stop") -> MagicMock:
    msg = MagicMock()
    msg.parsed = parsed
    msg.refusal = None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    completion = MagicMock()
    completion.choices = [choice]
    completion.usage = _make_usage()
    return completion


class TestTruncationRetry:
    def test_retries_with_doubled_max_tokens_on_length(self, tmp_path) -> None:
        client = _make_client(tmp_path)
        client.max_tokens = 4096

        truncated = _make_completion(None, finish_reason="length")
        ok_result = _DummyOutput(value="done")
        ok_completion = _make_completion(ok_result, finish_reason="stop")

        with patch.object(client._client.chat.completions, "parse",
                          side_effect=[truncated, ok_completion]) as mock_parse:
            result = client._call_parsed(
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0, {}
            )

        assert result.parsed.value == "done"
        calls = mock_parse.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["max_tokens"] == 4096
        assert calls[1].kwargs["max_tokens"] == 8192  # doubled

    def test_raises_after_three_truncations(self, tmp_path) -> None:
        client = _make_client(tmp_path)
        client.max_tokens = 4096

        truncated = _make_completion(None, finish_reason="length")
        with patch.object(client._client.chat.completions, "parse",
                          return_value=truncated):
            with pytest.raises(RuntimeError, match="truncated after 3 attempts"):
                client._call_parsed(
                    [{"role": "user", "content": "hi"}], _DummyOutput, 0.0, {}
                )

    def test_truncation_warning_logged(self, tmp_path, caplog) -> None:
        client = _make_client(tmp_path)
        client.max_tokens = 4096

        truncated = _make_completion(None, finish_reason="length")
        ok = _make_completion(_DummyOutput(value="ok"), finish_reason="stop")

        with patch.object(client._client.chat.completions, "parse",
                          side_effect=[truncated, ok]):
            with caplog.at_level(logging.WARNING, logger="epubforge.llm.client"):
                client._call_parsed(
                    [{"role": "user", "content": "hi"}], _DummyOutput, 0.0, {}
                )

        assert any("truncated" in r.message.lower() for r in caplog.records)


class TestJsonObjectFallback:
    def _make_bad_request(self) -> BadRequestError:
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"error": {"message": "response_format not supported"}}
        response.text = "response_format not supported"
        response.headers = {}
        response.request = MagicMock()
        return BadRequestError(
            message="response_format not supported",
            response=response,
            body={"error": {"message": "response_format not supported"}},
        )

    def test_falls_back_on_400_response_format(self, tmp_path) -> None:
        client = _make_client(tmp_path)

        fallback_content = json.dumps({"value": "fallback"})
        fallback_msg = MagicMock()
        fallback_msg.content = fallback_content
        fallback_choice = MagicMock()
        fallback_choice.message = fallback_msg
        fallback_completion = MagicMock()
        fallback_completion.choices = [fallback_choice]

        with (
            patch.object(client._client.chat.completions, "parse",
                         side_effect=self._make_bad_request()),
            patch.object(client._client.chat.completions, "create",
                         return_value=fallback_completion),
        ):
            result = client._call_parsed(
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0, {}
            )

        assert result.parsed.value == "fallback"

    def test_fallback_logs_warning(self, tmp_path, caplog) -> None:
        client = _make_client(tmp_path)

        fallback_msg = MagicMock()
        fallback_msg.content = json.dumps({"value": "x"})
        fallback_choice = MagicMock()
        fallback_choice.message = fallback_msg
        fallback_completion = MagicMock()
        fallback_completion.choices = [fallback_choice]

        with (
            patch.object(client._client.chat.completions, "parse",
                         side_effect=self._make_bad_request()),
            patch.object(client._client.chat.completions, "create",
                         return_value=fallback_completion),
            caplog.at_level(logging.WARNING, logger="epubforge.llm.client"),
        ):
            client._call_parsed(
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0, {}
            )

        assert any("json_object" in r.message.lower() for r in caplog.records)

    def _make_fallback_completion(self, content: str, finish_reason: str = "stop") -> MagicMock:
        msg = MagicMock()
        msg.content = content
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = finish_reason
        comp = MagicMock()
        comp.choices = [choice]
        comp.usage = _make_usage()
        return comp

    def test_fallback_retries_on_finish_reason_length(self, tmp_path) -> None:
        client = _make_client(tmp_path)
        client.max_tokens = 4096

        truncated = self._make_fallback_completion('{"value": "tr', finish_reason="length")
        ok = self._make_fallback_completion(json.dumps({"value": "ok"}), finish_reason="stop")

        with (
            patch.object(client._client.chat.completions, "parse",
                         side_effect=self._make_bad_request()),
            patch.object(client._client.chat.completions, "create",
                         side_effect=[truncated, ok]),
        ):
            result = client._call_parsed(
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0, {}
            )
        assert result.parsed.value == "ok"

    def test_fallback_retries_on_eof_validation_error(self, tmp_path) -> None:
        client = _make_client(tmp_path)

        truncated_json = '{"value": "incomplete'
        ok_json = json.dumps({"value": "complete"})

        truncated = self._make_fallback_completion(truncated_json, finish_reason="stop")
        ok = self._make_fallback_completion(ok_json, finish_reason="stop")

        with (
            patch.object(client._client.chat.completions, "parse",
                         side_effect=self._make_bad_request()),
            patch.object(client._client.chat.completions, "create",
                         side_effect=[truncated, ok]),
        ):
            result = client._call_parsed(
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0, {}
            )
        assert result.parsed.value == "complete"


class TestVlmDefaultMaxTokens:
    def test_vlm_max_tokens_defaults_to_16384(self, tmp_path) -> None:
        # The VLM default_factory sets max_tokens=16384; not overriding here means default applies
        cfg = Config(
            runtime=RuntimeSettings(cache_dir=tmp_path / ".cache"),
        )
        client = LLMClient(cfg, use_vlm=True)
        assert client.max_tokens == 16384

    def test_vlm_max_tokens_respects_explicit_config(self, tmp_path) -> None:
        cfg = Config(
            vlm=ProviderSettings(base_url="https://example.com/v1", api_key="test-key", max_tokens=8192),
            runtime=RuntimeSettings(cache_dir=tmp_path / ".cache"),
        )
        client = LLMClient(cfg, use_vlm=True)
        assert client.max_tokens == 8192

    def test_llm_max_tokens_unchanged(self, tmp_path) -> None:
        cfg = Config(
            llm=ProviderSettings(api_key="test-key", max_tokens=None),
            runtime=RuntimeSettings(cache_dir=tmp_path / ".cache"),
        )
        client = LLMClient(cfg, use_vlm=False)
        assert client.max_tokens is None


def _make_usage_with_cached(prompt: int = 100, completion: int = 20, cached: int = 0) -> MagicMock:
    details = MagicMock()
    details.cached_tokens = cached
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.prompt_tokens_details = details
    return usage


class TestCachedTokensExtraction:
    def test_cached_tokens_extracted_from_usage(self, tmp_path, caplog) -> None:
        tracker = get_tracker()
        before = tracker.cached_tokens

        client = _make_client(tmp_path)
        completion = _make_completion(_DummyOutput(value="ok"), finish_reason="stop")
        completion.usage = _make_usage_with_cached(prompt=200, completion=30, cached=1500)

        with (
            patch.object(client._client.chat.completions, "parse", return_value=completion),
            caplog.at_level(logging.INFO, logger="epubforge.llm.client"),
        ):
            client.chat_parsed(
                [{"role": "user", "content": "hi"}],
                response_format=_DummyOutput,
            )

        assert tracker.cached_tokens - before >= 1500
        assert any("cached=1500" in r.message for r in caplog.records)

    def test_missing_prompt_tokens_details_is_zero(self, tmp_path) -> None:
        tracker = get_tracker()
        before = tracker.cached_tokens

        client = _make_client(tmp_path)
        completion = _make_completion(_DummyOutput(value="ok"), finish_reason="stop")
        usage = MagicMock()
        usage.prompt_tokens = 50
        usage.completion_tokens = 10
        # No prompt_tokens_details attribute at all
        del usage.prompt_tokens_details
        completion.usage = usage

        with patch.object(client._client.chat.completions, "parse", return_value=completion):
            client.chat_parsed(
                [{"role": "user", "content": "no-cache-details"}],
                response_format=_DummyOutput,
            )

        assert tracker.cached_tokens - before == 0


def _sys_user_msgs(sys_content: Any) -> list[ChatCompletionMessageParam]:
    return cast(list[ChatCompletionMessageParam], [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": "hi"},
    ])


class TestPromptCaching:
    def test_system_string_wrapped_with_cache_control(self) -> None:
        msgs = _sys_user_msgs("You are helpful.")
        out = _apply_cache_control(msgs, enabled=True)
        sys_msg = next(m for m in out if m["role"] == "system")
        content = sys_msg["content"]
        assert isinstance(content, list)
        blocks = cast(list[dict[str, Any]], content)
        assert len(blocks) == 1
        block = blocks[0]
        assert block["type"] == "text"
        assert block["text"] == "You are helpful."
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_system_list_attaches_cache_control_to_last_text_block(self) -> None:
        msgs = _sys_user_msgs([
            {"type": "text", "text": "First block."},
            {"type": "text", "text": "Second block."},
        ])
        out = _apply_cache_control(msgs, enabled=True)
        sys_msg = next(m for m in out if m["role"] == "system")
        blocks = cast(list[dict[str, Any]], sys_msg["content"])
        assert "cache_control" not in blocks[0]
        assert blocks[1]["cache_control"] == {"type": "ephemeral"}

    def test_flag_disabled_passthrough(self) -> None:
        msgs = _sys_user_msgs("You are helpful.")
        out = _apply_cache_control(msgs, enabled=False)
        assert out is msgs

    def test_cache_key_stable_across_flag_toggle(self, tmp_path) -> None:
        msgs = _sys_user_msgs("You are helpful.")
        cfg_on = Config(
            llm=ProviderSettings(api_key="k", prompt_caching=True),
            runtime=RuntimeSettings(cache_dir=tmp_path / ".cache"),
        )
        cfg_off = Config(
            llm=ProviderSettings(api_key="k", prompt_caching=False),
            runtime=RuntimeSettings(cache_dir=tmp_path / ".cache"),
        )
        client_on = LLMClient(cfg_on)
        client_off = LLMClient(cfg_off)
        key_on = client_on._cache_key(msgs, _DummyOutput, 0.0, {})
        key_off = client_off._cache_key(msgs, _DummyOutput, 0.0, {})
        assert key_on == key_off
