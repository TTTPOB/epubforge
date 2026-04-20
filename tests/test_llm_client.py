"""Unit tests for LLMClient — truncation retry and json_object fallback."""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest
from openai import BadRequestError
from pydantic import BaseModel

from epubforge.config import Config
from epubforge.llm.client import LLMClient


class _DummyOutput(BaseModel):
    value: str = ""


def _make_client(tmp_path, *, use_vlm: bool = False) -> LLMClient:
    cfg = Config(
        llm_base_url="https://example.com/v1",
        llm_api_key="test-key",
        cache_dir=tmp_path / ".cache",
    )
    return LLMClient(cfg, use_vlm=use_vlm)


def _make_completion(parsed: Any, finish_reason: str = "stop") -> MagicMock:
    msg = MagicMock()
    msg.parsed = parsed
    msg.refusal = None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    completion = MagicMock()
    completion.choices = [choice]
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
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0
            )

        assert result.value == "done"
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
                    [{"role": "user", "content": "hi"}], _DummyOutput, 0.0
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
                    [{"role": "user", "content": "hi"}], _DummyOutput, 0.0
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
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0
            )

        assert result.value == "fallback"

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
                [{"role": "user", "content": "hi"}], _DummyOutput, 0.0
            )

        assert any("json_object" in r.message.lower() for r in caplog.records)


class TestVlmDefaultMaxTokens:
    def test_vlm_max_tokens_defaults_to_16384(self, tmp_path) -> None:
        cfg = Config(
            vlm_base_url="https://example.com/v1",
            vlm_api_key="test-key",
            vlm_max_tokens=None,
            cache_dir=tmp_path / ".cache",
        )
        client = LLMClient(cfg, use_vlm=True)
        assert client.max_tokens == 16384

    def test_vlm_max_tokens_respects_explicit_config(self, tmp_path) -> None:
        cfg = Config(
            vlm_base_url="https://example.com/v1",
            vlm_api_key="test-key",
            vlm_max_tokens=8192,
            cache_dir=tmp_path / ".cache",
        )
        client = LLMClient(cfg, use_vlm=True)
        assert client.max_tokens == 8192

    def test_llm_max_tokens_unchanged(self, tmp_path) -> None:
        cfg = Config(
            llm_api_key="test-key",
            llm_max_tokens=None,
            cache_dir=tmp_path / ".cache",
        )
        client = LLMClient(cfg, use_vlm=False)
        assert client.max_tokens is None
