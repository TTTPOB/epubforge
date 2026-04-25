"""Tests for Sub-phase 7E: diff-books with Git ref resolution.

Covers:
- build_diff_books_result with --base-ref / --proposed-ref
- Mutual exclusivity validation (file + ref conflict)
- Mixed ref+file usage
- GitError → CommandError conversion
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from epubforge.editor.cli_support import CommandError
from epubforge.editor.tool_surface import build_diff_books_result
from epubforge.io import save_book
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, bbox=None, source="passthrough")


def _minimal_book(*, title: str = "Ref Book", block_text: str = "Hello world.") -> Book:
    return Book(
        initialized_at="2024-01-01T00:00:00",
        uid_seed="ref-seed",
        title=title,
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                level=1,
                blocks=[
                    Paragraph(
                        uid="blk-1",
                        text=block_text,
                        role="body",
                        provenance=_prov(),
                    )
                ],
            )
        ],
    )


@pytest.fixture
def book_git_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a Git repo with edit_state/book.json committed.

    Returns (repo_root, work_dir, work_dir_rel).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )

    work_dir_rel = "work/book"
    work_dir = repo / work_dir_rel
    work_dir.mkdir(parents=True)
    book = _minimal_book()
    save_book(book, work_dir)

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial book"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo, work_dir, work_dir_rel


# ---------------------------------------------------------------------------
# Tests: build_diff_books_result with refs
# ---------------------------------------------------------------------------


def test_build_diff_books_result_with_proposed_ref(
    book_git_repo: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """build_diff_books_result with proposed_ref should resolve Book from Git and succeed."""
    repo, work_dir, work_dir_rel = book_git_repo

    # Write a modified book as a file (base stays at HEAD via default)
    proposed_book = _minimal_book(block_text="Modified proposed text.")
    proposed_path = tmp_path / "proposed.json"
    proposed_path.write_text(proposed_book.model_dump_json(indent=2), encoding="utf-8")

    # Use HEAD as proposed ref (same as base → zero changes)
    result = build_diff_books_result(
        work_dir,
        proposed_ref="HEAD",
    )
    assert result.diff_applies is True
    assert result.proposed_ref == "HEAD"
    assert result.base_ref is None
    # HEAD book == default base (same file), so change_count should be 0
    assert result.change_count == 0


def test_build_diff_books_result_with_base_ref(
    book_git_repo: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """build_diff_books_result with base_ref and proposed_file should succeed."""
    repo, work_dir, work_dir_rel = book_git_repo

    proposed_book = _minimal_book(block_text="Updated text for proposed.")
    proposed_path = tmp_path / "proposed.json"
    proposed_path.write_text(proposed_book.model_dump_json(indent=2), encoding="utf-8")

    result = build_diff_books_result(
        work_dir,
        base_ref="HEAD",
        proposed_file=proposed_path,
    )
    assert result.diff_applies is True
    assert result.base_ref == "HEAD"
    assert result.proposed_ref is None
    assert result.change_count >= 1


def test_build_diff_books_result_both_refs(
    book_git_repo: tuple[Path, Path, str],
) -> None:
    """build_diff_books_result with both base_ref and proposed_ref should succeed."""
    _, work_dir, _ = book_git_repo

    result = build_diff_books_result(
        work_dir,
        base_ref="HEAD",
        proposed_ref="HEAD",
    )
    assert result.diff_applies is True
    assert result.base_ref == "HEAD"
    assert result.proposed_ref == "HEAD"
    assert result.change_count == 0


def test_build_diff_books_result_conflicting_base_args(
    book_git_repo: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """Providing both base_file and base_ref should raise CommandError."""
    _, work_dir, _ = book_git_repo

    base_path = tmp_path / "base.json"
    base_path.write_text(_minimal_book().model_dump_json(), encoding="utf-8")

    proposed_path = tmp_path / "proposed.json"
    proposed_path.write_text(_minimal_book().model_dump_json(), encoding="utf-8")

    with pytest.raises(CommandError) as exc_info:
        build_diff_books_result(
            work_dir,
            base_file=base_path,
            base_ref="HEAD",
            proposed_file=proposed_path,
        )
    err = exc_info.value
    assert err.exit_code == 2
    assert "mutually exclusive" in err.payload.get("error", "")


def test_build_diff_books_result_conflicting_proposed_args(
    book_git_repo: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """Providing both proposed_file and proposed_ref should raise CommandError."""
    _, work_dir, _ = book_git_repo

    proposed_path = tmp_path / "proposed.json"
    proposed_path.write_text(_minimal_book().model_dump_json(), encoding="utf-8")

    with pytest.raises(CommandError) as exc_info:
        build_diff_books_result(
            work_dir,
            proposed_file=proposed_path,
            proposed_ref="HEAD",
        )
    err = exc_info.value
    assert err.exit_code == 2
    assert "mutually exclusive" in err.payload.get("error", "")


def test_build_diff_books_result_no_proposed_raises(
    book_git_repo: tuple[Path, Path, str],
) -> None:
    """Providing neither proposed_file nor proposed_ref should raise CommandError."""
    _, work_dir, _ = book_git_repo

    with pytest.raises(CommandError) as exc_info:
        build_diff_books_result(work_dir)
    err = exc_info.value
    assert err.exit_code == 2
    assert "required" in err.payload.get("error", "")


def test_build_diff_books_result_bad_proposed_ref(
    book_git_repo: tuple[Path, Path, str],
) -> None:
    """An invalid proposed_ref should raise CommandError with kind git_ref_error."""
    _, work_dir, _ = book_git_repo

    with pytest.raises(CommandError) as exc_info:
        build_diff_books_result(
            work_dir,
            proposed_ref="nonexistent-ref-99999",
        )
    err = exc_info.value
    assert err.exit_code == 1
    assert err.payload.get("kind") == "git_ref_error"


def test_build_diff_books_result_bad_base_ref(
    book_git_repo: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """An invalid base_ref should raise CommandError with kind git_ref_error."""
    _, work_dir, _ = book_git_repo

    proposed_path = tmp_path / "proposed.json"
    proposed_path.write_text(_minimal_book().model_dump_json(), encoding="utf-8")

    with pytest.raises(CommandError) as exc_info:
        build_diff_books_result(
            work_dir,
            base_ref="nonexistent-base-ref-99999",
            proposed_file=proposed_path,
        )
    err = exc_info.value
    assert err.exit_code == 1
    assert err.payload.get("kind") == "git_ref_error"
