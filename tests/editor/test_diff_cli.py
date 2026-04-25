from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from epubforge.cli import app
from epubforge.editor.patches import BookPatch, validate_book_patch
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


runner = CliRunner()


def _invoke(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


def _book(*, text: str = "Base text", title: str = "Diff CLI Book") -> Book:
    return Book(
        title=title,
        authors=["Diff Author"],
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(
                        uid="p-1",
                        text=text,
                        provenance=Provenance(page=1, source="passthrough"),
                    )
                ],
            )
        ],
    )


def _write_book(path: Path, book: Book) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(book.model_dump_json(indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_diff_books_files(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    base_path = tmp_path / "base.json"
    proposed_path = tmp_path / "proposed.json"
    _write_book(base_path, _book())
    _write_book(proposed_path, _book(text="Proposed text"))

    result = _invoke(
        [
            "editor",
            "diff-books",
            str(work_dir),
            "--base-file",
            str(base_path),
            "--proposed-file",
            str(proposed_path),
        ]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["diff_applies"] is True
    assert payload["round_trip_verified"] is True
    assert payload["change_count"] == 1
    assert payload["base_sha256"] == _sha256(base_path)
    assert payload["proposed_sha256"] == _sha256(proposed_path)
    assert payload["unsupported_diffs"] == []
    assert payload["review_groups"] == []
    assert payload["patch"]["rationale"]
    assert payload["patch"]["evidence_refs"] == []


def test_cli_diff_current_book_default_base_and_does_not_write_book_json(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    base_path = work_dir / "edit_state" / "book.json"
    proposed_path = tmp_path / "proposed.json"
    _write_book(base_path, _book())
    _write_book(proposed_path, _book(text="Default base proposed text"))
    original_book_json = base_path.read_text(encoding="utf-8")

    result = _invoke(
        [
            "editor",
            "diff-books",
            str(work_dir),
            "--proposed-file",
            str(proposed_path),
        ]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["base_sha256"] == hashlib.sha256(
        original_book_json.encode("utf-8")
    ).hexdigest()
    assert payload["round_trip_verified"] is True
    assert base_path.read_text(encoding="utf-8") == original_book_json


def test_cli_invalid_book_json(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    base_path = tmp_path / "base.json"
    proposed_path = tmp_path / "proposed.json"
    _write_book(base_path, _book())
    proposed_path.write_text("{not json", encoding="utf-8")

    result = _invoke(
        [
            "editor",
            "diff-books",
            str(work_dir),
            "--base-file",
            str(base_path),
            "--proposed-file",
            str(proposed_path),
        ]
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["kind"] == "invalid_json"
    assert "invalid JSON" in payload["error"]
    assert payload["path"] == str(proposed_path)


def test_cli_invalid_book_schema(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    base_path = tmp_path / "base.json"
    proposed_path = tmp_path / "proposed.json"
    _write_book(base_path, _book())
    proposed_path.write_text(json.dumps({"chapters": []}), encoding="utf-8")

    result = _invoke(
        [
            "editor",
            "diff-books",
            str(work_dir),
            "--base-file",
            str(base_path),
            "--proposed-file",
            str(proposed_path),
        ]
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["kind"] == "invalid_book_schema"
    assert "invalid Book schema" in payload["error"]
    assert payload["path"] == str(proposed_path)


def test_cli_duplicate_uid_reports_diagnostic(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    base_path = tmp_path / "base.json"
    proposed_path = tmp_path / "proposed.json"
    _write_book(base_path, _book())
    proposed = _book(text="Duplicate UID text")
    proposed.chapters.append(
        Chapter(
            uid="ch-2",
            title="Chapter 2",
            blocks=[
                Paragraph(
                    uid="p-1",
                    text="Duplicate block uid",
                    provenance=Provenance(page=2, source="passthrough"),
                )
            ],
        )
    )
    _write_book(proposed_path, proposed)

    result = _invoke(
        [
            "editor",
            "diff-books",
            str(work_dir),
            "--base-file",
            str(base_path),
            "--proposed-file",
            str(proposed_path),
        ]
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["kind"] == "uid_error"
    assert "duplicate uid" in payload["error"]
    assert payload["base_sha256"] == _sha256(base_path)
    assert payload["proposed_sha256"] == _sha256(proposed_path)


def test_cli_round_trip_output_contains_schema_valid_patch(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    base_path = tmp_path / "base.json"
    proposed_path = tmp_path / "proposed.json"
    base = _book()
    proposed = _book(text="Schema-valid patch text")
    _write_book(base_path, base)
    _write_book(proposed_path, proposed)

    result = _invoke(
        [
            "editor",
            "diff-books",
            str(work_dir),
            "--base-file",
            str(base_path),
            "--proposed-file",
            str(proposed_path),
        ]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    patch = BookPatch.model_validate(payload["patch"])
    validate_book_patch(base, patch)
    assert payload["round_trip_verified"] is True
    assert payload["diff_applies"] is True
    assert patch.rationale
    assert isinstance(patch.evidence_refs, list)


def test_cli_unsupported_diff_reports_nonzero_diagnostic(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    base_path = tmp_path / "base.json"
    proposed_path = tmp_path / "proposed.json"
    _write_book(base_path, _book(title="Base Title"))
    _write_book(proposed_path, _book(title="Changed Title"))

    result = _invoke(
        [
            "editor",
            "diff-books",
            str(work_dir),
            "--base-file",
            str(base_path),
            "--proposed-file",
            str(proposed_path),
        ]
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["kind"] == "unsupported_diff"
    assert "Book-level" in payload["error"]
    assert payload["unsupported_diffs"] == [{"message": payload["error"]}]
