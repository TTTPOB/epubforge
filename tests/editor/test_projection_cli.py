from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from epubforge.cli import app
from epubforge.editor.state import resolve_editor_paths
from epubforge.io import load_book
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


runner = CliRunner()


def _invoke(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


def _source_book(prov: Callable[..., Provenance], *, title: str = "Projection Book") -> Book:
    return Book(
        title=title,
        authors=["Projection Author"],
        chapters=[
            Chapter(
                title="Chapter Alpha",
                blocks=[Paragraph(text="Alpha from edit state.", provenance=prov(1))],
            ),
            Chapter(
                title="Chapter Beta",
                blocks=[Paragraph(text="Beta from edit state.", provenance=prov(2))],
            ),
        ],
    )


def _write_semantic_source(work_dir: Path, book: Book) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(
        book.model_dump_json(indent=2), encoding="utf-8"
    )


def _init_work_dir(prov: Callable[..., Provenance], tmp_path: Path, name: str) -> Path:
    work_dir = tmp_path / name
    _write_semantic_source(work_dir, _source_book(prov))
    result = _invoke(["editor", "init", str(work_dir)])
    assert result.exit_code == 0, result.output
    return work_dir


def test_projection_full_export_writes_index_and_all_chapters(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "full-export")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)

    result = _invoke(["editor", "projection", "export", str(work_dir)])

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert isinstance(summary["exported_at"], str)
    assert summary["exported_at"]
    assert summary["projection_dir"] == str(paths.edit_state_dir / "projections")
    assert summary["index_path"] == str(paths.edit_state_dir / "projections" / "index.md")
    assert summary["chapters_written"] == 2
    assert summary["blocks_written"] == 2
    assert len(summary["chapter_paths"]) == 2

    index_path = paths.edit_state_dir / "projections" / "index.md"
    assert index_path.exists()
    index_text = index_path.read_text(encoding="utf-8")
    assert "Projection Book" in index_text

    for chapter in book.chapters:
        assert chapter.uid is not None
        chapter_path = paths.edit_state_dir / "projections" / "chapters" / f"{chapter.uid}.md"
        assert chapter_path.exists()
        chapter_text = chapter_path.read_text(encoding="utf-8")
        assert f"[[chapter {chapter.uid}]]" in chapter_text


def test_projection_chapter_export_writes_selected_chapter_and_refreshes_index(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "chapter-export")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    target = book.chapters[1]
    other = book.chapters[0]
    assert target.uid is not None
    assert other.uid is not None

    result = _invoke(
        ["editor", "projection", "export", str(work_dir), "--chapter", target.uid]
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["chapters_written"] == 1
    assert summary["blocks_written"] == 1
    assert summary["chapter_paths"] == [
        str(paths.edit_state_dir / "projections" / "chapters" / f"{target.uid}.md")
    ]

    assert (paths.edit_state_dir / "projections" / "chapters" / f"{target.uid}.md").exists()
    assert not (paths.edit_state_dir / "projections" / "chapters" / f"{other.uid}.md").exists()
    index_text = (paths.edit_state_dir / "projections" / "index.md").read_text(
        encoding="utf-8"
    )
    assert target.uid in index_text
    assert other.uid not in index_text


def test_projection_chapter_export_after_full_export_cleans_stale_chapter_files(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "chapter-export-cleans-stale")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    target = book.chapters[1]
    other = book.chapters[0]
    assert target.uid is not None
    assert other.uid is not None

    full = _invoke(["editor", "projection", "export", str(work_dir)])
    assert full.exit_code == 0, full.output
    other_path = paths.edit_state_dir / "projections" / "chapters" / f"{other.uid}.md"
    target_path = paths.edit_state_dir / "projections" / "chapters" / f"{target.uid}.md"
    assert other_path.exists()
    assert target_path.exists()

    single = _invoke(
        ["editor", "projection", "export", str(work_dir), "--chapter", target.uid]
    )

    assert single.exit_code == 0, single.output
    assert target_path.exists()
    assert not other_path.exists()
    chapter_files = sorted(
        path.name for path in (paths.edit_state_dir / "projections" / "chapters").glob("*.md")
    )
    assert chapter_files == [f"{target.uid}.md"]


def test_projection_invalid_chapter_returns_json_error(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "invalid-chapter")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    expected_uids = [chapter.uid for chapter in book.chapters]

    result = _invoke(
        ["editor", "projection", "export", str(work_dir), "--chapter", "missing-ch"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "chapter not found: missing-ch"
    assert payload["available_chapters"] == expected_uids


def test_projection_rejects_unsafe_chapter_uid_from_book_json(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "unsafe-chapter-uid")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    book.chapters[0].uid = "../escape"
    paths.book_path.write_text(book.model_dump_json(indent=2), encoding="utf-8")

    result = _invoke(["editor", "projection", "export", str(work_dir)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "unsafe chapter uid for projection path: ../escape"
    assert not (paths.edit_state_dir / "projections" / "escape.md").exists()
    assert not (paths.edit_state_dir / "projections" / "chapters" / ".." / "escape.md").exists()


def test_projection_uninitialized_workdir_errors_without_semantic_fallback(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = tmp_path / "uninitialized"
    _write_semantic_source(work_dir, _source_book(prov, title="Should Not Export"))

    result = _invoke(["editor", "projection", "export", str(work_dir)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "editor state is not initialized" in payload["error"]
    assert not (work_dir / "edit_state" / "projections" / "index.md").exists()


def test_projection_repeat_export_overwrites_existing_files(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "repeat-export")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    chapter_uid = book.chapters[0].uid
    assert chapter_uid is not None

    first = _invoke(["editor", "projection", "export", str(work_dir)])
    assert first.exit_code == 0, first.output

    index_path = paths.edit_state_dir / "projections" / "index.md"
    chapter_path = paths.edit_state_dir / "projections" / "chapters" / f"{chapter_uid}.md"
    index_path.write_text("STALE INDEX", encoding="utf-8")
    chapter_path.write_text("STALE CHAPTER", encoding="utf-8")

    second = _invoke(["editor", "projection", "export", str(work_dir)])
    assert second.exit_code == 0, second.output
    assert "STALE" not in index_path.read_text(encoding="utf-8")
    assert "STALE" not in chapter_path.read_text(encoding="utf-8")


def test_projection_export_uses_edit_state_book_json_not_05_semantic(
    prov: Callable[..., Provenance], tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "edit-state-source")
    paths = resolve_editor_paths(work_dir)

    edited_book = load_book(paths.book_path)
    edited_book.title = "Edited Projection Book"
    edited_block = edited_book.chapters[0].blocks[0]
    assert isinstance(edited_block, Paragraph)
    edited_block.text = "Edited alpha from book.json."
    paths.book_path.write_text(edited_book.model_dump_json(indent=2), encoding="utf-8")

    _write_semantic_source(
        work_dir,
        Book(
            title="Stale Semantic Source",
            chapters=[
                Chapter(
                    title="Stale Chapter",
                    blocks=[Paragraph(text="Stale paragraph.", provenance=prov(9))],
                )
            ],
        ),
    )

    result = _invoke(["editor", "projection", "export", str(work_dir)])

    assert result.exit_code == 0, result.output
    index_text = (paths.edit_state_dir / "projections" / "index.md").read_text(
        encoding="utf-8"
    )
    projection_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (paths.edit_state_dir / "projections" / "chapters").glob("*.md")
    )
    assert "Edited Projection Book" in index_text
    assert "Edited alpha from book.json." in projection_text
    assert "Stale Semantic Source" not in index_text
    assert "Stale paragraph." not in projection_text


def test_projection_export_help_does_not_expose_stdout_mode() -> None:
    result = _invoke(["editor", "projection", "export", "--help"])

    assert result.exit_code == 0, result.output
    assert "--stdout" not in result.output
