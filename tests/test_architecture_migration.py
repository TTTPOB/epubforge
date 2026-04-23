from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from epubforge import pipeline
from epubforge.cli import app
from epubforge.config import Config, load_config
from epubforge.epub_builder import build_epub, resolve_build_source
from epubforge.io import EDITABLE_BOOK_PATH
from epubforge.io import save_book
from epubforge.ir.semantic import Book, Chapter, Figure, Footnote, Paragraph, Provenance, Table
from epubforge.ir.style_registry import StyleDefinition, StyleRegistry


def _synthetic_verified_book(prov: Callable[..., Provenance]) -> Book:
    return Book(
        title="Synthetic Build",
        chapters=[
            Chapter(
                title="Chapter 1",
                blocks=[
                    Paragraph(
                        text="Intro \x02fn-1-①\x03 text.",
                        style_class="intro",
                        provenance=prov(1),
                    ),
                    Footnote(callout="①", text="Synthetic note.", paired=True, provenance=prov(1)),
                    Figure(caption="Synthetic figure", provenance=prov(1)),
                    Table(
                        html="<table><tbody><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></tbody></table>",
                        table_title="Table 1",
                        caption="Synthetic table",
                        provenance=prov(1),
                    ),
                ],
            )
        ],
    )


def _write_synthetic_build_assets(prov: Callable[..., Provenance], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    save_book(_synthetic_verified_book(prov), destination / "07_footnote_verified.json", allow_legacy=True)
    registry = StyleRegistry(
        book="synthetic-build",
        styles=[
            StyleDefinition(
                id="intro",
                parent_role="body",
                description="Synthetic intro style",
                css_class="intro",
                css_rules={"text-indent": "0", "font-weight": "bold"},
            )
        ],
    )
    (destination / "style_registry.json").write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    images_dir = destination / "images"
    images_dir.mkdir()
    (images_dir / "p0001_synthetic.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-image")


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


def test_resolve_build_source_falls_back_only_to_05_semantic(tmp_path: Path) -> None:
    legacy = tmp_path / "05_semantic.json"
    legacy.write_text("{}", encoding="utf-8")
    (tmp_path / "06_proofread.json").write_text("{}", encoding="utf-8")
    (tmp_path / "07_footnote_verified.json").write_text("{}", encoding="utf-8")

    assert resolve_build_source(tmp_path) == legacy


def test_resolve_build_source_rejects_hidden_06_07_fallback(tmp_path: Path) -> None:
    (tmp_path / "06_proofread.json").write_text("{}", encoding="utf-8")
    (tmp_path / "07_footnote_verified.json").write_text("{}", encoding="utf-8")

    try:
        resolve_build_source(tmp_path)
    except FileNotFoundError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected FileNotFoundError when only 06/07 artifacts exist")

    assert "05_semantic.json" in message
    assert "edit_state/book.json" in message


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


def test_load_config_reads_editor_section_and_env_overrides(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[editor]
lease_ttl_seconds = 900
compact_threshold = 12
max_loops = 7
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EPUBFORGE_EDITOR_COMPACT_THRESHOLD", "33")

    cfg = load_config(config_path)

    assert cfg.editor.lease_ttl_seconds == 900
    assert cfg.editor.compact_threshold == 33
    assert cfg.editor.max_loops == 7


def test_synthetic_build_is_byte_equivalent_before_and_after_import_legacy_migration(prov, tmp_path: Path) -> None:
    work_dir = tmp_path / "synthetic-build"
    _write_synthetic_build_assets(prov, work_dir)

    legacy_epub = work_dir / "legacy.epub"
    migrated_epub = work_dir / "migrated.epub"

    build_epub(
        work_dir / "07_footnote_verified.json",
        legacy_epub,
        images_dir=work_dir / "images",
        registry_path=work_dir / "style_registry.json",
    )

    cli_runner = CliRunner()
    import_result = cli_runner.invoke(
        app,
        ["editor", "import-legacy", str(work_dir), "--from", "07_footnote_verified.json", "--assume-verified"],
        catch_exceptions=False,
    )
    assert import_result.exit_code == 0, import_result.output
    assert resolve_build_source(work_dir) == work_dir / EDITABLE_BOOK_PATH

    build_epub(
        resolve_build_source(work_dir),
        migrated_epub,
        images_dir=work_dir / "images",
        registry_path=work_dir / "style_registry.json",
    )

    assert legacy_epub.read_bytes() == migrated_epub.read_bytes()
