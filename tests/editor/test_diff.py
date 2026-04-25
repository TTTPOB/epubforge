"""Tests for the Phase 6B Book diff skeleton."""

from __future__ import annotations

import pytest

from epubforge.editor import DiffError as PublicDiffError
from epubforge.editor import diff_books as public_diff_books
from epubforge.editor.diff import DiffError, diff_books
from epubforge.editor.patches import apply_book_patch, validate_book_patch
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, bbox=None, source="passthrough")


def _paragraph(uid: str | None = "blk-1", text: str = "Hello") -> Paragraph:
    return Paragraph(uid=uid, text=text, role="body", provenance=_prov())


def _chapter(
    uid: str | None = "ch-1",
    *,
    blocks: list[Paragraph] | None = None,
) -> Chapter:
    return Chapter(uid=uid, title="Chapter 1", level=1, blocks=blocks or [_paragraph()])


def _book(*, chapters: list[Chapter] | None = None, title: str = "Test Book") -> Book:
    return Book(title=title, chapters=chapters or [_chapter()])


def test_identity_diff_returns_empty_patch_and_applies() -> None:
    base = _book()
    proposed = base.model_copy(deep=True)

    patch = diff_books(base, proposed)

    assert patch.agent_id == "diff-engine"
    assert patch.scope.chapter_uid is None
    assert patch.changes == []
    validate_book_patch(base, patch)
    result = apply_book_patch(base, patch)
    assert result.model_dump(mode="json") == proposed.model_dump(mode="json")


@pytest.mark.parametrize(
    ("book", "message"),
    [
        (_book(chapters=[_chapter(uid=None)]), "chapter.*uid=None"),
        (_book(chapters=[_chapter(blocks=[_paragraph(uid=None)])]), "block.*uid=None"),
        (
            _book(
                chapters=[
                    _chapter(uid="dup", blocks=[]),
                    _chapter(uid="dup", blocks=[]),
                ]
            ),
            "duplicate uid 'dup'.*chapter",
        ),
        (
            _book(
                chapters=[
                    _chapter(uid="ch-1", blocks=[_paragraph(uid="dup")]),
                    _chapter(uid="ch-2", blocks=[_paragraph(uid="dup")]),
                ]
            ),
            "duplicate uid 'dup'.*block",
        ),
        (
            _book(chapters=[_chapter(uid="collision", blocks=[_paragraph(uid="collision")])]),
            "duplicate uid 'collision'.*collides",
        ),
        (_book(chapters=[_chapter(uid="")]), "empty uid"),
    ],
)
def test_uid_validation_errors(book: Book, message: str) -> None:
    with pytest.raises(DiffError, match=message):
        diff_books(book, book.model_copy(deep=True))


def test_book_level_delta_is_unsupported() -> None:
    base = _book(title="Original")
    proposed = base.model_copy(update={"title": "Changed"}, deep=True)

    with pytest.raises(DiffError, match="Book-level.*title"):
        diff_books(base, proposed)


def test_provenance_only_delta_is_unsupported_immutable_delta() -> None:
    base = _book()
    proposed = base.model_copy(deep=True)
    proposed.chapters[0].blocks[0].provenance = _prov(page=2)

    with pytest.raises(DiffError, match="blk-1.*provenance"):
        diff_books(base, proposed)


def test_representable_field_delta_fails_closed_for_phase_6b() -> None:
    base = _book()
    proposed = base.model_copy(deep=True)
    block = proposed.chapters[0].blocks[0]
    assert isinstance(block, Paragraph)
    block.text = "Changed text"

    with pytest.raises(DiffError, match="representable deltas.*Phase 6B.*blk-1.*text"):
        diff_books(base, proposed)


def test_public_imports_work() -> None:
    assert public_diff_books is diff_books
    assert PublicDiffError is DiffError
