"""Common semantic Book query helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

from epubforge.fields import iter_block_text_fields
from epubforge.ir.semantic import Block, Book, Chapter, Footnote, Heading
from epubforge.markers import FN_MARKER_FULL_RE


@dataclass(frozen=True)
class BlockRef:
    chapter_idx: int
    block_idx: int
    chapter: Chapter
    block: Block


@dataclass(frozen=True)
class MarkerRef:
    chapter_idx: int
    block_idx: int
    block: Block
    field: str
    page: int
    callout: str
    marker: str
    span: tuple[int, int]


def iter_blocks(book: Book) -> Iterator[BlockRef]:
    for ch_idx, chapter in enumerate(book.chapters):
        for b_idx, block in enumerate(chapter.blocks):
            yield BlockRef(
                chapter_idx=ch_idx,
                block_idx=b_idx,
                chapter=chapter,
                block=block,
            )


def find_blocks(
    book: Book,
    *,
    kinds: set[str] | None = None,
    chapter_uid: str | None = None,
    predicate: Callable[[BlockRef], bool] | None = None,
) -> list[BlockRef]:
    matches: list[BlockRef] = []
    for ref in iter_blocks(book):
        if chapter_uid is not None and getattr(ref.chapter, "uid", None) != chapter_uid:
            continue
        if kinds is not None and ref.block.kind not in kinds:
            continue
        if predicate is not None and not predicate(ref):
            continue
        matches.append(ref)
    return matches


def find_block_by_uid(book: Book, uid: str) -> BlockRef | None:
    for ref in iter_blocks(book):
        if ref.block.uid == uid:
            return ref
    return None


def find_headings(book: Book, *, level: int | None = None) -> list[BlockRef]:
    refs = find_blocks(book, kinds={"heading"})
    if level is None:
        return refs
    return [ref for ref in refs if isinstance(ref.block, Heading) and ref.block.level == level]


def find_footnotes(
    book: Book,
    *,
    paired: bool | None = None,
    orphan: bool | None = None,
    callout: str | None = None,
) -> list[BlockRef]:
    refs = find_blocks(book, kinds={"footnote"})
    matches: list[BlockRef] = []
    for ref in refs:
        block = ref.block
        assert isinstance(block, Footnote)
        if paired is not None and block.paired != paired:
            continue
        if orphan is not None and block.orphan != orphan:
            continue
        if callout is not None and block.callout != callout:
            continue
        matches.append(ref)
    return matches


def find_markers(
    book: Book,
    *,
    page: int | None = None,
    callout: str | None = None,
) -> list[MarkerRef]:
    matches: list[MarkerRef] = []
    for ref in iter_blocks(book):
        for field, value in iter_block_text_fields(ref.block):
            for match in FN_MARKER_FULL_RE.finditer(value):
                marker_page = int(match.group(1))
                marker_callout = match.group(2)
                if page is not None and marker_page != page:
                    continue
                if callout is not None and marker_callout != callout:
                    continue
                matches.append(
                    MarkerRef(
                        chapter_idx=ref.chapter_idx,
                        block_idx=ref.block_idx,
                        block=ref.block,
                        field=field,
                        page=marker_page,
                        callout=marker_callout,
                        marker=match.group(0),
                        span=match.span(),
                    )
                )
    return matches


def find_marker_source(book: Book, footnote: Footnote) -> MarkerRef | None:
    matches = find_markers(book, page=footnote.provenance.page, callout=footnote.callout)
    return matches[0] if matches else None
