"""Single source of truth for editable text fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Literal

from epubforge.ir.semantic import Block, Book, Chapter

TextFieldName = Literal["text", "html", "table_title", "caption", "latex"]

FIELD_MAP: dict[str, tuple[TextFieldName, ...]] = {
    "paragraph": ("text",),
    "heading": ("text",),
    "footnote": ("text",),
    "figure": ("caption",),
    "table": ("html", "table_title", "caption"),
    "equation": ("latex",),
}


@dataclass(frozen=True)
class TextFieldRef:
    chapter_idx: int
    block_idx: int
    chapter: Chapter
    block: Block
    field: TextFieldName
    value: str


def block_text_fields(block: Block) -> tuple[TextFieldName, ...]:
    return FIELD_MAP.get(block.kind, ())


def iter_block_text_fields(block: Block) -> Iterator[tuple[TextFieldName, str]]:
    for field in block_text_fields(block):
        yield field, getattr(block, field)


def set_text_field(block: Block, field: TextFieldName, value: str) -> Block:
    if field not in block_text_fields(block):
        raise ValueError(
            f"Field {field!r} is not editable for block kind {block.kind!r}"
        )
    return block.model_copy(update={field: value})


def iter_text_fields(book: Book) -> Iterator[TextFieldRef]:
    for ch_idx, chapter in enumerate(book.chapters):
        for b_idx, block in enumerate(chapter.blocks):
            for field, value in iter_block_text_fields(block):
                yield TextFieldRef(
                    chapter_idx=ch_idx,
                    block_idx=b_idx,
                    chapter=chapter,
                    block=block,
                    field=field,
                    value=value,
                )
