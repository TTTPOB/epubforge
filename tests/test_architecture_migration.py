from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from epubforge import pipeline
from epubforge.cli import app
from epubforge.config import Config, load_config
from epubforge.epub_builder import resolve_build_source


def test_run_all_stops_after_stage_4(monkeypatch) -> None:
    calls: list[str] = []

    def record(name: str):
        def inner(*args, **kwargs) -> None:
            calls.append(name)

        return inner

    monkeypatch.setattr(pipeline, "run_parse", record("parse"))
    monkeypatch.setattr(pipeline, "run_classify", record("classify"))
    monkeypatch.setattr(pipeline, "run_extract", record("extract"))
    monkeypatch.setattr(pipeline, "run_assemble", record("assemble"))
    monkeypatch.setattr(pipeline, "run_build", record("build"))

    pipeline.run_all(Path("book.pdf"), Config())

    assert calls == ["parse", "classify", "extract", "assemble"]


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


def test_run_command_limits_from_stage_to_4() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["run", "book.pdf", "--from", "5"])

    assert result.exit_code != 0
    assert "5" in result.output
    assert "1<=x<=4" in result.output


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
