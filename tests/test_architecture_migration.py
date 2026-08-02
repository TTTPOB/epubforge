"""Architecture checks for the direct MinerU-Luna workflow."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import cast

from click import Group
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from epubforge import pipeline
from epubforge.cli import app
from epubforge.config import ChaptersSettings, Config, load_config


REPO_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "epubforge"
COMMANDS = {"run", "parse", "normalize", "segment", "prepare", "revise"}
RETAINED_MODULES = (
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
    "llm.client",
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


def test_default_model_selects_luna_provider_id() -> None:
    assert Config().llm.model == "openai/gpt-5.6-luna"
