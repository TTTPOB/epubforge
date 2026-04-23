"""Canonical Book IO helpers for editable and legacy artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from epubforge.ir.semantic import Book

EDITABLE_BOOK_PATH = Path("edit_state/book.json")
LEGACY_BOOK_FILENAMES = (
    "05_semantic.json",
    "05_semantic_raw.json",
)


def resolve_book_path(path: str | Path, *, for_write: bool = False) -> Path:
    """Resolve a work dir or direct file path to the canonical book artifact path."""
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        if for_write:
            return candidate / EDITABLE_BOOK_PATH

        editable = candidate / EDITABLE_BOOK_PATH
        if editable.exists():
            return editable

        direct = candidate / "book.json"
        if direct.exists():
            return direct

        for name in LEGACY_BOOK_FILENAMES:
            legacy = candidate / name
            if legacy.exists():
                return legacy

        raise FileNotFoundError(f"No book artifact found under {candidate}")

    return candidate


def load_book(path: str | Path) -> Book:
    """Load a Book from an editable artifact or a legacy pipeline artifact."""
    book_path = resolve_book_path(path)
    payload = json.loads(book_path.read_text(encoding="utf-8"))
    _normalize_legacy_payload(payload)
    return Book.model_validate(payload)


def save_book(
    book: Book,
    path: str | Path,
    *,
    indent: int = 2,
    allow_legacy: bool = False,
) -> Path:
    """Save a Book to the canonical editable artifact path."""
    if not allow_legacy:
        _validate_editable_book(book)

    book_path = resolve_book_path(path, for_write=True)
    book_path.parent.mkdir(parents=True, exist_ok=True)
    book_path.write_text(book.model_dump_json(indent=indent), encoding="utf-8")
    return book_path


def _normalize_legacy_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise TypeError("Book artifact must decode to a JSON object")

    payload.setdefault("op_log_version", 0)
    payload.setdefault("initialized_at", "")
    payload.setdefault("uid_seed", "")

    chapters = payload.get("chapters")
    if not isinstance(chapters, list):
        return

    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        chapter.setdefault("uid", None)
        blocks = chapter.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict):
                block.setdefault("uid", None)


def _validate_editable_book(book: Book) -> None:
    missing: list[str] = []

    if not book.uid_seed:
        missing.append("Book.uid_seed")
    if not book.initialized_at:
        missing.append("Book.initialized_at")

    for ch_idx, chapter in enumerate(book.chapters):
        if not chapter.uid:
            missing.append(f"Chapter[{ch_idx}].uid")
        for b_idx, block in enumerate(chapter.blocks):
            if not block.uid:
                missing.append(f"Chapter[{ch_idx}].blocks[{b_idx}].uid")

    if missing:
        preview = ", ".join(missing[:8])
        if len(missing) > 8:
            preview += ", ..."
        raise ValueError(f"Editable book is missing required stable ids: {preview}")
