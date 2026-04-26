"""Tests for GraniteSettings config submodel (Phase 1 / I1)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from epubforge.config import Config, ExtractSettings, GraniteSettings, load_config


class TestGraniteSettingsDefaults:
    def test_enabled_default_false(self) -> None:
        s = GraniteSettings()
        assert s.enabled is False

    def test_api_url_default(self) -> None:
        s = GraniteSettings()
        assert s.api_url == "http://localhost:8080/v1/chat/completions"

    def test_api_model_default(self) -> None:
        s = GraniteSettings()
        assert s.api_model == "granite-docling"

    def test_prompt_default(self) -> None:
        s = GraniteSettings()
        assert s.prompt == "Convert this page to docling."

    def test_scale_default(self) -> None:
        s = GraniteSettings()
        assert s.scale == 2.0

    def test_timeout_seconds_default(self) -> None:
        s = GraniteSettings()
        assert s.timeout_seconds == 180

    def test_max_tokens_default(self) -> None:
        s = GraniteSettings()
        assert s.max_tokens == 4096

    def test_health_check_default(self) -> None:
        s = GraniteSettings()
        assert s.health_check is True

    def test_concurrency_default(self) -> None:
        s = GraniteSettings()
        assert s.concurrency == 1


class TestExtractSettingsGraniteField:
    def test_granite_field_present(self) -> None:
        s = ExtractSettings()
        assert hasattr(s, "granite")
        assert isinstance(s.granite, GraniteSettings)

    def test_granite_disabled_by_default(self) -> None:
        s = ExtractSettings()
        assert s.granite.enabled is False

    def test_config_granite_accessible(self) -> None:
        c = Config()
        assert c.extract.granite.enabled is False
        assert c.extract.granite.api_url == "http://localhost:8080/v1/chat/completions"


class TestTomlLoading:
    def test_granite_section_enabled(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            textwrap.dedent("""\
                [extract.granite]
                enabled = true
                api_url = "http://192.168.1.10:8080/v1/chat/completions"
            """),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.extract.granite.enabled is True
        assert cfg.extract.granite.api_url == "http://192.168.1.10:8080/v1/chat/completions"

    def test_granite_section_partial(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            textwrap.dedent("""\
                [extract.granite]
                concurrency = 2
            """),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.extract.granite.concurrency == 2
        assert cfg.extract.granite.enabled is False  # default preserved

    def test_granite_unknown_key_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            textwrap.dedent("""\
                [extract.granite]
                nonexistent_field = "oops"
            """),
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            load_config(cfg_file)


class TestEnvOverrides:
    def test_granite_enabled_env_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "1")
        cfg = load_config(None)
        assert cfg.extract.granite.enabled is True

    def test_granite_enabled_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "true")
        cfg = load_config(None)
        assert cfg.extract.granite.enabled is True

    def test_granite_enabled_env_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "yes")
        cfg = load_config(None)
        assert cfg.extract.granite.enabled is True

    def test_granite_enabled_env_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "on")
        cfg = load_config(None)
        assert cfg.extract.granite.enabled is True

    def test_granite_enabled_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "false")
        cfg = load_config(None)
        assert cfg.extract.granite.enabled is False

    def test_granite_enabled_env_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "no")
        cfg = load_config(None)
        assert cfg.extract.granite.enabled is False

    def test_granite_enabled_env_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_ENABLED", "0")
        cfg = load_config(None)
        assert cfg.extract.granite.enabled is False

    def test_granite_api_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "EPUBFORGE_EXTRACT_GRANITE_API_URL",
            "http://10.0.0.1:9090/v1/chat/completions",
        )
        cfg = load_config(None)
        assert cfg.extract.granite.api_url == "http://10.0.0.1:9090/v1/chat/completions"

    def test_granite_api_model_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_API_MODEL", "my-granite-model")
        cfg = load_config(None)
        assert cfg.extract.granite.api_model == "my-granite-model"

    def test_granite_timeout_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_GRANITE_TIMEOUT", "300")
        cfg = load_config(None)
        assert cfg.extract.granite.timeout_seconds == 300
