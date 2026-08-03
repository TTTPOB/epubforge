"""Architecture checks for the isolated OpenCode book-editor workflow."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import cast

from click import Group
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from epubforge import pipeline
from epubforge.agent_runner import book_editor_identity
from epubforge.cli import app
from epubforge.config import ChaptersSettings, Config, load_config


REPO_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "epubforge"
COMMANDS = {"run", "parse", "normalize", "segment", "prepare", "revise"}
RETAINED_MODULES = (
    "agent_runner",
    "annotation",
    "chapter_revision",
    "chapter_segmentation",
    "chapter_workspace",
    "config",
    "mineru",
    "mineru_content",
    "observability",
    "page_geometry",
    "pipeline",
    "strict_json",
)
DELETED_MODULES = (
    "assembler",
    "audit.structure",
    "classifier",
    "editor.doctor",
    "editor.agent_output",
    "editor.patches",
    "epub_builder",
    "extract_skip_vlm",
    "fields",
    "io",
    "ir.semantic",
    "markers",
    "query",
    "stage3_artifacts",
    "text_utils",
    "llm.client",
)


def test_run_all_uses_the_five_stage_chapter_workflow(monkeypatch) -> None:
    calls: list[str] = []

    def record(name: str):
        def inner(*args, **kwargs) -> None:
            calls.append(name)

        return inner

    monkeypatch.setattr(pipeline, "run_parse", record("parse"))
    monkeypatch.setattr(pipeline, "run_normalize", record("normalize"))
    monkeypatch.setattr(pipeline, "run_segment", record("segment"))
    monkeypatch.setattr(pipeline, "run_prepare", record("prepare"))
    monkeypatch.setattr(pipeline, "run_revise", record("revise"))

    pipeline.run_all(Path("book.pdf"), Config())

    assert calls == ["parse", "normalize", "segment", "prepare", "revise"]


@pytest.mark.parametrize("module_name", RETAINED_MODULES)
def test_retained_modules_import(module_name: str) -> None:
    importlib.import_module(f"epubforge.{module_name}")


@pytest.mark.parametrize("module_name", DELETED_MODULES)
def test_deleted_modules_have_no_source_or_import(module_name: str) -> None:
    module_path = SOURCE_ROOT.joinpath(*module_name.split("."))
    assert not module_path.with_suffix(".py").exists()

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"epubforge.{module_name}")


def test_cli_exposes_only_the_six_current_commands() -> None:
    click_app = cast(Group, get_command(app))

    assert set(click_app.commands) == COMMANDS

    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


def test_run_command_accepts_stage_5(monkeypatch) -> None:
    calls: list[int] = []

    def fake_run_all(*args, **kwargs) -> None:
        calls.append(kwargs["from_stage"])

    monkeypatch.setattr(pipeline, "run_all", fake_run_all)

    result = CliRunner().invoke(app, ["run", "book.pdf", "--from", "5"])

    assert result.exit_code == 0
    assert calls == [5]


def test_run_command_rejects_stage_above_5() -> None:
    result = CliRunner().invoke(app, ["run", "book.pdf", "--from", "6"])

    assert result.exit_code != 0
    assert "1<=x<=5" in result.output


def test_load_config_reads_chapter_rendering_section_and_env_override(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[chapters]
render_dpi = 180
jpeg_quality = 88
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EPUBFORGE_CHAPTERS_RENDER_DPI", "200")

    cfg = load_config(config_path)

    assert cfg.chapters.render_dpi == 200
    assert cfg.chapters.jpeg_quality == 88
    assert ChaptersSettings().render_dpi == 150
    assert not hasattr(cfg, "editor")
    assert not hasattr(cfg, "extract")
    assert not hasattr(cfg, "llm")
    assert not hasattr(cfg.runtime, "cache_dir")


def test_packaged_agent_selects_luna_medium() -> None:
    identity = book_editor_identity()

    assert identity.name == "book-editor"
    assert identity.model == "openai/gpt-5.6-luna"
    assert identity.variant == "medium"
    assert len(identity.fingerprint) == 64

    markdown = (SOURCE_ROOT / "agents/book-editor.md").read_text(encoding="utf-8")
    assert "permission:" in markdown
    assert '  "*": deny' in markdown
    assert "  bash: deny" in markdown
    assert "  external_directory: deny" in markdown


def test_production_has_no_direct_openai_or_llm_client_architecture() -> None:
    python_sources = list(SOURCE_ROOT.rglob("*.py"))

    assert not any((SOURCE_ROOT / "llm").rglob("*.py"))
    for path in python_sources:
        source = path.read_text(encoding="utf-8")
        assert "from openai" not in source
        assert "import openai" not in source
        assert "LLMClient" not in source
        assert "EPUBFORGE_LLM_" not in source

    project = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"openai>=' not in project


def test_old_llm_environment_values_are_not_loaded(monkeypatch) -> None:
    monkeypatch.setenv("EPUBFORGE_LLM_API_KEY", "must-not-enter-config")
    monkeypatch.setenv("EPUBFORGE_RUNTIME_CACHE_DIR", "must-not-enter-config")

    cfg = load_config()

    assert not hasattr(cfg, "llm")
    assert not hasattr(cfg.runtime, "cache_dir")


def test_cli_startup_banner_has_no_model_or_cache(caplog) -> None:
    from epubforge import cli

    caplog.set_level(logging.INFO, logger="epubforge.cli")
    cli._log_startup_banner(Config(), None)

    assert "epubforge startup" in caplog.text
    assert "model=" not in caplog.text
    assert "cache" not in caplog.text.lower()
