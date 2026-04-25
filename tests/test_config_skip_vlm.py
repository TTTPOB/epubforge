"""Tests for ExtractSettings after skip_vlm / max_vlm_batch_pages removal."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from epubforge.config import ExtractSettings, load_config


class TestExtractSettingsDefaults:
    def test_removed_fields_absent(self) -> None:
        s = ExtractSettings()
        assert not hasattr(s, "skip_vlm")
        assert not hasattr(s, "max_vlm_batch_pages")
        assert not hasattr(s, "vlm_dpi")
        assert not hasattr(s, "max_simple_batch_pages")
        assert not hasattr(s, "max_complex_batch_pages")

    def test_enable_book_memory_default(self) -> None:
        s = ExtractSettings()
        assert s.enable_book_memory is True


class TestTomlLoading:
    def test_removed_fields_in_toml_raise(self, tmp_path: Path) -> None:
        """ExtractSettings has extra='forbid', so removed field names in TOML must raise."""
        for field in ("skip_vlm", "vlm_dpi", "max_vlm_batch_pages"):
            cfg_file = tmp_path / f"config_{field}.toml"
            cfg_file.write_text(
                textwrap.dedent(f"""\
                    [extract]
                    {field} = true
                """),
                encoding="utf-8",
            )
            with pytest.raises(Exception):
                load_config(cfg_file)


class TestEnvOverrides:
    def test_removed_env_skip_vlm_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Setting removed env var must not affect config and must not raise
        monkeypatch.setenv("EPUBFORGE_EXTRACT_SKIP_VLM", "1")
        cfg = load_config(None)
        assert not hasattr(cfg.extract, "skip_vlm")

    def test_removed_env_max_vlm_batch_pages_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_MAX_VLM_BATCH_PAGES", "8")
        cfg = load_config(None)
        assert not hasattr(cfg.extract, "max_vlm_batch_pages")

    def test_removed_env_vlm_dpi_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EPUBFORGE_EXTRACT_VLM_DPI", "300")
        cfg = load_config(None)
        assert not hasattr(cfg.extract, "vlm_dpi")
