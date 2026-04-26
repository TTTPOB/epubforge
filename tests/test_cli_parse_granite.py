"""Tests for parse CLI granite flags (Phase 1 / I5).

Covers --with-granite / --no-granite / --force-granite behavior.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from epubforge.cli import app
from epubforge.config import Config


runner = CliRunner()


def _make_cfg(granite_enabled: bool) -> Config:
    """Return a Config with granite.enabled set as requested."""
    cfg = Config()
    return cfg.model_copy(
        update={
            "extract": cfg.extract.model_copy(
                update={
                    "granite": cfg.extract.granite.model_copy(
                        update={"enabled": granite_enabled}
                    )
                }
            )
        }
    )


def _invoke_parse(
    tmp_path: Path,
    extra_args: list[str],
    granite_enabled_in_config: bool,
) -> tuple[MagicMock, Result]:
    """Invoke 'parse' CLI with a fake PDF and mocked run_parse.

    Returns (mock_run_parse, result).
    run_parse is called as: pipeline.run_parse(pdf_path, cfg, force=force)
    so cfg is args[1] and force is kwargs["force"].
    """
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    cfg = _make_cfg(granite_enabled_in_config)

    with (
        patch("epubforge.cli.load_config", return_value=cfg),
        patch("epubforge.cli._init_logging", return_value=None),
        patch("epubforge.cli._log_startup_banner"),
        patch("epubforge.cli.pipeline.run_parse") as mock_run_parse,
    ):
        args = ["--config", str(tmp_path / "fake.toml"), "parse", str(pdf)] + extra_args
        result = runner.invoke(app, args)

    return mock_run_parse, result


def _get_run_parse_cfg(mock_run_parse: MagicMock) -> Config:
    """Extract the cfg positional arg from a run_parse mock call."""
    pos_args, _ = mock_run_parse.call_args
    # Signature: run_parse(pdf_path, cfg, force=force)
    return pos_args[1]


def _get_run_parse_force(mock_run_parse: MagicMock) -> bool:
    """Extract the force keyword arg from a run_parse mock call."""
    _, kw = mock_run_parse.call_args
    return kw["force"]


class TestGraniteDefaultBehavior:
    """Scenario 1: default flags + config disabled → run_parse called with granite disabled."""

    def test_default_no_granite_flag_config_disabled(self, tmp_path: Path) -> None:
        mock_run_parse, result = _invoke_parse(
            tmp_path, extra_args=[], granite_enabled_in_config=False
        )
        assert result.exit_code == 0, result.output
        mock_run_parse.assert_called_once()
        assert _get_run_parse_cfg(mock_run_parse).extract.granite.enabled is False


class TestWithGraniteFlag:
    """Scenario 2: --with-granite + config disabled → granite enabled."""

    def test_with_granite_overrides_disabled_config(self, tmp_path: Path) -> None:
        mock_run_parse, result = _invoke_parse(
            tmp_path, extra_args=["--with-granite"], granite_enabled_in_config=False
        )
        assert result.exit_code == 0, result.output
        mock_run_parse.assert_called_once()
        assert _get_run_parse_cfg(mock_run_parse).extract.granite.enabled is True

    def test_with_granite_short_flag(self, tmp_path: Path) -> None:
        mock_run_parse, result = _invoke_parse(
            tmp_path, extra_args=["-g"], granite_enabled_in_config=False
        )
        assert result.exit_code == 0, result.output
        assert _get_run_parse_cfg(mock_run_parse).extract.granite.enabled is True


class TestNoGraniteFlag:
    """Scenario 3: --no-granite + config enabled → granite disabled."""

    def test_no_granite_overrides_enabled_config(self, tmp_path: Path) -> None:
        mock_run_parse, result = _invoke_parse(
            tmp_path, extra_args=["--no-granite"], granite_enabled_in_config=True
        )
        assert result.exit_code == 0, result.output
        mock_run_parse.assert_called_once()
        assert _get_run_parse_cfg(mock_run_parse).extract.granite.enabled is False


class TestMutualExclusion:
    """Scenario 4: --with-granite + --no-granite → error exit."""

    def test_mutual_exclusion_raises(self, tmp_path: Path) -> None:
        _, result = _invoke_parse(
            tmp_path,
            extra_args=["--with-granite", "--no-granite"],
            granite_enabled_in_config=False,
        )
        assert result.exit_code != 0


class TestForceGraniteFlag:
    """Scenario 5: --force-granite equivalent to -f (re-runs all of stage 1)."""

    def test_force_granite_implies_force(self, tmp_path: Path) -> None:
        mock_run_parse, result = _invoke_parse(
            tmp_path, extra_args=["--force-granite"], granite_enabled_in_config=False
        )
        assert result.exit_code == 0, result.output
        mock_run_parse.assert_called_once()
        # Simplified: --force-granite sets force=True (re-runs all of stage 1)
        assert _get_run_parse_force(mock_run_parse) is True
        # --force-granite also implies with_granite → granite enabled
        assert _get_run_parse_cfg(mock_run_parse).extract.granite.enabled is True

    def test_force_flag_sets_force_true(self, tmp_path: Path) -> None:
        mock_run_parse, result = _invoke_parse(
            tmp_path, extra_args=["-f"], granite_enabled_in_config=False
        )
        assert result.exit_code == 0, result.output
        assert _get_run_parse_force(mock_run_parse) is True

    def test_no_force_sets_force_false(self, tmp_path: Path) -> None:
        mock_run_parse, result = _invoke_parse(
            tmp_path, extra_args=[], granite_enabled_in_config=False
        )
        assert result.exit_code == 0, result.output
        assert _get_run_parse_force(mock_run_parse) is False
