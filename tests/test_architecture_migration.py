from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from epubforge import pipeline
from epubforge.cli import app
from epubforge.config import ChaptersSettings, Config, load_config
from epubforge.epub_builder import resolve_build_source


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


def test_resolve_build_source_prefers_edit_state_book(tmp_path: Path) -> None:
    legacy = tmp_path / "05_semantic.json"
    legacy.write_text("{}", encoding="utf-8")
    (tmp_path / "06_proofread.json").write_text("{}", encoding="utf-8")
    (tmp_path / "07_footnote_verified.json").write_text("{}", encoding="utf-8")

    editable = tmp_path / "edit_state" / "book.json"
    editable.parent.mkdir()
    editable.write_text("{}", encoding="utf-8")

    assert resolve_build_source(tmp_path) == editable


def test_cli_help_omits_removed_stage_commands() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "refine-toc" not in result.output
    assert "proofread" not in result.output
    assert "footnote-verify" not in result.output
    assert "build" in result.output


def test_run_command_accepts_stage_5(monkeypatch) -> None:
    runner = CliRunner()
    calls: list[int] = []

    def fake_run_all(*args, **kwargs) -> None:
        calls.append(kwargs["from_stage"])

    monkeypatch.setattr(pipeline, "run_all", fake_run_all)

    result = runner.invoke(app, ["run", "book.pdf", "--from", "5"])

    assert result.exit_code == 0
    assert calls == [5]


def test_run_command_rejects_stage_above_5() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["run", "book.pdf", "--from", "6"])

    assert result.exit_code != 0
    assert "1<=x<=5" in result.output


def test_load_config_reads_editor_section_and_env_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[editor]
compact_threshold = 12
max_loops = 7
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EPUBFORGE_EDITOR_COMPACT_THRESHOLD", "33")

    cfg = load_config(config_path)

    assert cfg.editor.compact_threshold == 33
    assert cfg.editor.max_loops == 7


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


def test_default_model_selects_luna_medium_provider_id() -> None:
    assert Config().llm.model == "openai/gpt-5.6-luna"
