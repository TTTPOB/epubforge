"""Tests for ExtractSettings skip_vlm / max_vlm_batch_pages fields and env overrides."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from epubforge.config import ExtractSettings, load_config


class TestExtractSettingsDefaults:
    def test_skip_vlm_default_false(self) -> None:
        s = ExtractSettings()
        assert s.skip_vlm is False

    def test_max_vlm_batch_pages_default(self) -> None:
        s = ExtractSettings()
        assert s.max_vlm_batch_pages == 4

    def test_old_fields_absent(self) -> None:
        # max_simple_batch_pages and max_complex_batch_pages must not exist
        s = ExtractSettings()
        assert not hasattr(s, "max_simple_batch_pages")
        assert not hasattr(s, "max_complex_batch_pages")


class TestTomlLoading:
    def test_skip_vlm_true_from_toml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            textwrap.dedent("""\
                [extract]
                skip_vlm = true
            """),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.extract.skip_vlm is True

    def test_max_vlm_batch_pages_from_toml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            textwrap.dedent("""\
                [extract]
                max_vlm_batch_pages = 8
            """),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.extract.max_vlm_batch_pages == 8

    def test_old_fields_in_toml_raise(self, tmp_path: Path) -> None:
        # ExtractSettings has extra="forbid", so old field names in TOML must raise
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            textwrap.dedent("""\
                [extract]
                max_simple_batch_pages = 8
            """),
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            load_config(cfg_file)


class TestEnvOverrides:
    def test_skip_vlm_env_truthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_SKIP_VLM", "1")
        cfg = load_config(None)
        assert cfg.extract.skip_vlm is True

    def test_skip_vlm_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_SKIP_VLM", "0")
        cfg = load_config(None)
        assert cfg.extract.skip_vlm is False

    def test_max_vlm_batch_pages_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_MAX_VLM_BATCH_PAGES", "8")
        cfg = load_config(None)
        assert cfg.extract.max_vlm_batch_pages == 8

    def test_old_env_max_simple_batch_pages_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Setting removed env var must not affect config and must not raise
        monkeypatch.setenv("EPUBFORGE_EXTRACT_MAX_SIMPLE_BATCH_PAGES", "99")
        cfg = load_config(None)
        # The field simply doesn't exist; the env var is not in _ENV_MAP so it's silently ignored
        assert not hasattr(cfg.extract, "max_simple_batch_pages")

    def test_old_env_max_complex_batch_pages_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_MAX_COMPLEX_BATCH_PAGES", "99")
        cfg = load_config(None)
        assert not hasattr(cfg.extract, "max_complex_batch_pages")


class TestPriority:
    def test_env_overrides_toml(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var takes precedence over TOML value."""
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            textwrap.dedent("""\
                [extract]
                skip_vlm = false
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("EPUBFORGE_EXTRACT_SKIP_VLM", "true")
        cfg = load_config(cfg_file)
        assert cfg.extract.skip_vlm is True
