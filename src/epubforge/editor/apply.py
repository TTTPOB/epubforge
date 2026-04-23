"""Deterministic editor apply and replay helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Iterable, Literal, cast
from uuid import uuid4

from epubforge.editor.ops import (
    CompactMarker,
    DeleteBlock,
    EditOp,
    FootnoteOp,
    InsertBlock,
    MergeBlocks,
    MergeChapters,
    NoopOp,
    OpEnvelope,
    Precondition,
    RelocateBlock,
    RevertOp,
    SetFootnoteFlag,
    SetHeadingId,
    SetHeadingLevel,
    SetRole,
    SetStyleClass,
    SetText,
    SplitBlock,
    SplitChapter,
    SplitMergedTable,
)
from epubforge.editor.leases import LeaseState
from epubforge.editor.memory import ChapterStatus, EditMemory
from epubforge.ir.semantic import (
    Block,
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Table,
)
from epubforge.markers import FN_MARKER_FULL_RE, has_raw_callout, make_fn_marker, replace_first_raw, replace_nth_raw


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


class ApplyError(RuntimeError):
    """Raised when an envelope cannot be applied safely."""

    def __init__(self, reason: str, op_id: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.op_id = op_id


@dataclass(frozen=True)
class RevertBackref:
    target_op_id: str
    revert_op_id: str
    inverse_op_id: str
    ts: str


@dataclass(frozen=True)
class ApplyResult:
    book: Book
    accepted_envelopes: tuple[OpEnvelope, ...]
    revert_backref: RevertBackref | None = None
    memory: EditMemory | None = None


@dataclass(frozen=True)
class BlockRef:
    chapter_idx: int
    block_idx: int


@dataclass(frozen=True)
class FootnoteMutation:
    op_name: Literal["pair_footnote", "unpair_footnote", "relink_footnote", "mark_orphan"]
    fn_ref: BlockRef
    source_ref: BlockRef | None = None
    new_source_ref: BlockRef | None = None
    occurrence_index: int = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_envelopes(log_path: Path) -> list[OpEnvelope]:
    if not log_path.exists():
        return []
    envelopes: list[OpEnvelope] = []
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            envelopes.append(OpEnvelope.model_validate_json(line))
    return envelopes


def _chapter_index(book: Book) -> dict[str, int]:
    index: dict[str, int] = {}
    for ch_idx, chapter in enumerate(book.chapters):
        if chapter.uid is not None:
            index[chapter.uid] = ch_idx
    return index


def _chapter_uid_for_index(book: Book, chapter_idx: int, *, op_id: str) -> str:
    chapter_uid = book.chapters[chapter_idx].uid
    if chapter_uid is None:
        raise ApplyError("chapter is missing uid", op_id)
    return chapter_uid


def _block_index(book: Book) -> dict[str, BlockRef]:
    index: dict[str, BlockRef] = {}
    for ch_idx, chapter in enumerate(book.chapters):
        for b_idx, block in enumerate(chapter.blocks):
            if block.uid is not None:
                index[block.uid] = BlockRef(ch_idx, b_idx)
    return index


def _text_fields(block: Block) -> list[tuple[str, str]]:
    if isinstance(block, Paragraph):
        return [("text", block.text)]
    if isinstance(block, Heading):
        return [("text", block.text)]
    if isinstance(block, Footnote):
        return [("text", block.text)]
    if isinstance(block, Table):
        fields: list[tuple[str, str]] = [("html", block.html)]
        if block.table_title:
            fields.append(("table_title", block.table_title))
        if block.caption:
            fields.append(("caption", block.caption))
        return fields
    if isinstance(block, Figure):
        return [("caption", block.caption)]
    if isinstance(block, Equation):
        return []
    return []


def _set_text_field(block: Block, field: str, value: str) -> Block:
    return block.model_copy(update={field: value})


def _get_block(book: Book, block_uid: str) -> tuple[BlockRef, Block]:
    ref = _block_index(book).get(block_uid)
    if ref is None:
        raise ApplyError(f"missing block {block_uid}", block_uid)
    return ref, book.chapters[ref.chapter_idx].blocks[ref.block_idx]


def _get_chapter(book: Book, chapter_uid: str) -> tuple[int, Chapter]:
    ch_idx = _chapter_index(book).get(chapter_uid)
    if ch_idx is None:
        raise ApplyError(f"missing chapter {chapter_uid}", chapter_uid)
    return ch_idx, book.chapters[ch_idx]


def _get_field_value(block: Block, field: str) -> object:
    if not hasattr(block, field):
        raise ApplyError(f"block {block.uid} does not expose field {field}", block.uid or "<unknown>")
    return getattr(block, field)


def _require_same_chapter(book: Book, block_uids: Iterable[str], *, op_id: str) -> tuple[int, list[BlockRef]]:
    refs: list[BlockRef] = []
    chapter_idx: int | None = None
    block_map = _block_index(book)
    for block_uid in block_uids:
        ref = block_map.get(block_uid)
        if ref is None:
            raise ApplyError(f"missing block {block_uid}", op_id)
        if chapter_idx is None:
            chapter_idx = ref.chapter_idx
        elif ref.chapter_idx != chapter_idx:
            raise ApplyError("all blocks must belong to the same chapter", op_id)
        refs.append(ref)
    assert chapter_idx is not None
    return chapter_idx, refs


def _join_text(parts: list[str], join: Literal["concat", "cjk", "newline"]) -> str:
    if join == "newline":
        return "\n".join(parts)
    if join == "concat":
        return "".join(parts)
    return "".join(parts)


def _split_text(block: Block, op: SplitBlock, *, op_id: str) -> list[str]:
    if not hasattr(block, "text"):
        raise ApplyError(f"split_block only supports text-bearing blocks; got {block.kind}", op_id)

    text = getattr(block, "text")
    if not isinstance(text, str):
        raise ApplyError("split_block text field must be a string", op_id)

    if op.strategy == "at_text_match":
        assert op.text_match is not None
        idx = text.find(op.text_match)
        if idx <= 0:
            raise ApplyError(f"text_match {op.text_match!r} not found for split", op_id)
        return [text[:idx], text[idx:]]

    if op.strategy == "at_marker":
        assert op.marker_occurrence is not None
        matches = list(FN_MARKER_FULL_RE.finditer(text))
        if len(matches) < op.marker_occurrence:
            raise ApplyError("marker occurrence for split_block not found", op_id)
        cut = matches[op.marker_occurrence - 1].end()
        return [text[:cut], text[cut:]]

    if op.strategy == "at_line_index":
        if not isinstance(block, Paragraph) or block.display_lines is None:
            raise ApplyError("at_line_index requires paragraph.display_lines", op_id)
        assert op.line_index is not None
        if op.line_index >= len(block.display_lines) - 1:
            raise ApplyError("line_index must leave content on both sides of split", op_id)
        left = "\n".join(block.display_lines[: op.line_index + 1])
        right = "\n".join(block.display_lines[op.line_index + 1 :])
        return [left, right]

    sentence_breaks = [match.end() for match in _SENTENCE_SPLIT_RE.finditer(text)]
    if len(sentence_breaks) < op.max_splits:
        raise ApplyError("at_sentence could not produce enough segments", op_id)
    cut_positions = sentence_breaks[: op.max_splits]
    segments: list[str] = []
    start = 0
    for cut in cut_positions:
        segments.append(text[start:cut])
        start = cut
    segments.append(text[start:])
    if any(segment == "" for segment in segments):
        raise ApplyError("at_sentence produced an empty split segment", op_id)
    return segments


def _make_block(kind: str, uid: str, payload: dict[str, object]) -> Block:
    data = dict(payload)
    data["uid"] = uid
    if kind == "paragraph":
        return Paragraph.model_validate(data)
    if kind == "heading":
        return Heading.model_validate(data)
    if kind == "footnote":
        return Footnote.model_validate(data)
    if kind == "figure":
        return Figure.model_validate(data)
    if kind == "table":
        return Table.model_validate(data)
    if kind == "equation":
        return Equation.model_validate(data)
    raise AssertionError(f"unsupported block kind {kind}")


def _find_marker_source(book: Book, marker: str) -> BlockRef | None:
    for ch_idx, chapter in enumerate(book.chapters):
        for b_idx, block in enumerate(chapter.blocks):
            for _, text in _text_fields(block):
                if marker in text:
                    return BlockRef(ch_idx, b_idx)
    return None


def _resolve_marker_field(block: Block, marker: str, callout: str) -> tuple[str, str] | None:
    for field, text in _text_fields(block):
        if marker in text:
            return field, text
        if has_raw_callout(text, callout):
            return field, text
    return None


def apply_footnote_mutation(book: Book, mutation: FootnoteMutation, *, op_id: str) -> bool:
    fn_block = book.chapters[mutation.fn_ref.chapter_idx].blocks[mutation.fn_ref.block_idx]
    if not isinstance(fn_block, Footnote):
        raise ApplyError("footnote op target is not a Footnote block", op_id)

    marker = make_fn_marker(fn_block.provenance.page, fn_block.callout)

    if mutation.op_name == "pair_footnote":
        if mutation.source_ref is None:
            raise ApplyError("pair_footnote requires source block", op_id)
        source_block = book.chapters[mutation.source_ref.chapter_idx].blocks[mutation.source_ref.block_idx]
        for field, text in _text_fields(source_block):
            if has_raw_callout(text, fn_block.callout):
                new_text = replace_nth_raw(text, fn_block.callout, marker, mutation.occurrence_index)
                book.chapters[mutation.source_ref.chapter_idx].blocks[mutation.source_ref.block_idx] = _set_text_field(
                    source_block,
                    field,
                    new_text,
                )
                book.chapters[mutation.fn_ref.chapter_idx].blocks[mutation.fn_ref.block_idx] = fn_block.model_copy(
                    update={"paired": True}
                )
                return True
        raise ApplyError("pair_footnote source block does not contain raw callout", op_id)

    if mutation.op_name == "unpair_footnote":
        source_ref = _find_marker_source(book, marker)
        if source_ref is None:
            raise ApplyError("unpair_footnote could not locate existing marker", op_id)
        source_block = book.chapters[source_ref.chapter_idx].blocks[source_ref.block_idx]
        for field, text in _text_fields(source_block):
            if marker in text:
                book.chapters[source_ref.chapter_idx].blocks[source_ref.block_idx] = _set_text_field(
                    source_block,
                    field,
                    text.replace(marker, fn_block.callout),
                )
                book.chapters[mutation.fn_ref.chapter_idx].blocks[mutation.fn_ref.block_idx] = fn_block.model_copy(
                    update={"paired": False}
                )
                return True
        raise ApplyError("unpair_footnote could not remove marker from source block", op_id)

    if mutation.op_name == "relink_footnote":
        if mutation.source_ref is None or mutation.new_source_ref is None:
            raise ApplyError("relink_footnote requires source and new_source blocks", op_id)
        old_block = book.chapters[mutation.source_ref.chapter_idx].blocks[mutation.source_ref.block_idx]
        new_block = book.chapters[mutation.new_source_ref.chapter_idx].blocks[mutation.new_source_ref.block_idx]

        new_has_raw = any(has_raw_callout(text, fn_block.callout) for _, text in _text_fields(new_block))
        if not new_has_raw:
            raise ApplyError("relink_footnote new source does not contain raw callout", op_id)

        old_removed = False
        for field, text in _text_fields(old_block):
            if marker in text:
                book.chapters[mutation.source_ref.chapter_idx].blocks[mutation.source_ref.block_idx] = _set_text_field(
                    old_block,
                    field,
                    text.replace(marker, fn_block.callout),
                )
                old_removed = True
                break

        new_block = book.chapters[mutation.new_source_ref.chapter_idx].blocks[mutation.new_source_ref.block_idx]
        for field, text in _text_fields(new_block):
            if has_raw_callout(text, fn_block.callout):
                new_text = replace_nth_raw(text, fn_block.callout, marker, mutation.occurrence_index)
                book.chapters[mutation.new_source_ref.chapter_idx].blocks[mutation.new_source_ref.block_idx] = _set_text_field(
                    new_block,
                    field,
                    new_text,
                )
                book.chapters[mutation.fn_ref.chapter_idx].blocks[mutation.fn_ref.block_idx] = fn_block.model_copy(
                    update={"paired": True}
                )
                return True

        if old_removed:
            restored_old = book.chapters[mutation.source_ref.chapter_idx].blocks[mutation.source_ref.block_idx]
            for field, text in _text_fields(restored_old):
                if has_raw_callout(text, fn_block.callout):
                    book.chapters[mutation.source_ref.chapter_idx].blocks[mutation.source_ref.block_idx] = _set_text_field(
                        restored_old,
                        field,
                        replace_first_raw(text, fn_block.callout, marker),
                    )
                    break
        raise ApplyError("relink_footnote could not embed marker in new source", op_id)

    source_ref = _find_marker_source(book, marker)
    if source_ref is not None:
        source_block = book.chapters[source_ref.chapter_idx].blocks[source_ref.block_idx]
        for field, text in _text_fields(source_block):
            if marker in text:
                book.chapters[source_ref.chapter_idx].blocks[source_ref.block_idx] = _set_text_field(
                    source_block,
                    field,
                    text.replace(marker, fn_block.callout),
                )
                break
    book.chapters[mutation.fn_ref.chapter_idx].blocks[mutation.fn_ref.block_idx] = fn_block.model_copy(
        update={"paired": False, "orphan": True}
    )
    return True


def _check_preconditions(book: Book, preconditions: Iterable[Precondition], *, op_id: str) -> None:
    block_map = _block_index(book)
    chapter_map = _chapter_index(book)
    for precondition in preconditions:
        if precondition.kind == "block_exists":
            assert precondition.block_uid is not None
            if precondition.block_uid not in block_map:
                raise ApplyError(f"precondition failed: block {precondition.block_uid} does not exist", op_id)
            continue

        if precondition.kind == "chapter_exists":
            assert precondition.chapter_uid is not None
            if precondition.chapter_uid not in chapter_map:
                raise ApplyError(f"precondition failed: chapter {precondition.chapter_uid} does not exist", op_id)
            continue

        if precondition.kind == "version_at_least":
            assert precondition.min_version is not None
            if book.version < precondition.min_version:
                raise ApplyError(
                    f"precondition failed: book.version={book.version} < {precondition.min_version}",
                    op_id,
                )
            continue

        assert precondition.block_uid is not None
        ref = block_map.get(precondition.block_uid)
        if ref is None:
            raise ApplyError(f"precondition failed: block {precondition.block_uid} does not exist", op_id)
        block = book.chapters[ref.chapter_idx].blocks[ref.block_idx]

        if precondition.kind == "field_equals":
            assert precondition.field is not None
            actual = _get_field_value(block, precondition.field)
            if actual != precondition.expected:
                raise ApplyError(
                    f"precondition failed: {precondition.block_uid}.{precondition.field}={actual!r} != {precondition.expected!r}",
                    op_id,
                )
            continue

        if not isinstance(block, Footnote):
            raise ApplyError("footnote_paired_state applies only to Footnote blocks", op_id)
        if precondition.paired is not None and block.paired != precondition.paired:
            raise ApplyError(
                f"precondition failed: footnote {precondition.block_uid} paired={block.paired!r} != {precondition.paired!r}",
                op_id,
            )
        if precondition.orphan is not None and block.orphan != precondition.orphan:
            raise ApplyError(
                f"precondition failed: footnote {precondition.block_uid} orphan={block.orphan!r} != {precondition.orphan!r}",
                op_id,
            )


def _check_new_uid_collisions(book: Book, op: EditOp, *, op_id: str) -> None:
    chapter_map = _chapter_index(book)
    block_map = _block_index(book)

    def ensure_block_uid(uid: str) -> None:
        if uid in block_map:
            raise ApplyError(f"new block uid collision: {uid}", op_id)

    def ensure_chapter_uid(uid: str) -> None:
        if uid in chapter_map:
            raise ApplyError(f"new chapter uid collision: {uid}", op_id)

    if isinstance(op, InsertBlock):
        ensure_block_uid(op.new_block_uid)
        return
    if isinstance(op, SplitBlock):
        for uid in op.new_block_uids:
            ensure_block_uid(uid)
        return
    if isinstance(op, MergeChapters):
        ensure_chapter_uid(op.new_chapter_uid)
        for section in op.sections:
            ensure_block_uid(section.new_block_uid)
        return
    if isinstance(op, SplitChapter):
        ensure_chapter_uid(op.new_chapter_uid)


def _apply_op(book: Book, op: EditOp, *, op_id: str) -> Book:
    if isinstance(op, NoopOp | CompactMarker | RevertOp):
        return book

    if isinstance(op, SetRole):
        ref, block = _get_block(book, op.block_uid)
        book.chapters[ref.chapter_idx].blocks[ref.block_idx] = block.model_copy(update={"role": op.value})
        return book

    if isinstance(op, SetStyleClass):
        ref, block = _get_block(book, op.block_uid)
        book.chapters[ref.chapter_idx].blocks[ref.block_idx] = block.model_copy(update={"style_class": op.value})
        return book

    if isinstance(op, SetText):
        ref, block = _get_block(book, op.block_uid)
        book.chapters[ref.chapter_idx].blocks[ref.block_idx] = block.model_copy(update={op.field: op.value})
        return book

    if isinstance(op, SetHeadingLevel):
        ref, block = _get_block(book, op.block_uid)
        if not isinstance(block, Heading):
            raise ApplyError("set_heading_level requires a Heading block", op_id)
        book.chapters[ref.chapter_idx].blocks[ref.block_idx] = block.model_copy(update={"level": op.value})
        return book

    if isinstance(op, SetHeadingId):
        ref, block = _get_block(book, op.block_uid)
        if not isinstance(block, Heading):
            raise ApplyError("set_heading_id requires a Heading block", op_id)
        book.chapters[ref.chapter_idx].blocks[ref.block_idx] = block.model_copy(update={"id": op.value})
        return book

    if isinstance(op, SetFootnoteFlag):
        ref, block = _get_block(book, op.block_uid)
        if not isinstance(block, Footnote):
            raise ApplyError("set_footnote_flag requires a Footnote block", op_id)
        update: dict[str, object] = {}
        if op.paired is not None:
            update["paired"] = op.paired
        if op.orphan is not None:
            update["orphan"] = op.orphan
        book.chapters[ref.chapter_idx].blocks[ref.block_idx] = block.model_copy(update=update)
        return book

    if isinstance(op, MergeBlocks):
        chapter_idx, refs = _require_same_chapter(book, op.block_uids, op_id=op_id)
        blocks = [book.chapters[ref.chapter_idx].blocks[ref.block_idx] for ref in refs]
        if not all(hasattr(block, "text") for block in blocks):
            raise ApplyError("merge_blocks currently only supports text-bearing blocks", op_id)
        merged_text = _join_text([getattr(block, "text") for block in blocks], op.join)
        first_block = blocks[0].model_copy(update={"text": merged_text})
        chapter = book.chapters[chapter_idx]
        kept_idx = refs[0].block_idx
        delete_indexes = {ref.block_idx for ref in refs[1:]}
        next_blocks: list[Block] = []
        for b_idx, block in enumerate(chapter.blocks):
            if b_idx == kept_idx:
                next_blocks.append(first_block)
            elif b_idx not in delete_indexes:
                next_blocks.append(block)
        chapter.blocks = next_blocks
        return book

    if isinstance(op, SplitBlock):
        ref, block = _get_block(book, op.block_uid)
        segments = _split_text(block, op, op_id=op_id)
        if len(segments) != len(op.new_block_uids) + 1:
            raise ApplyError("split_block produced unexpected segment count", op_id)
        chapter = book.chapters[ref.chapter_idx]
        next_blocks: list[Block] = []
        for b_idx, current in enumerate(chapter.blocks):
            if b_idx != ref.block_idx:
                next_blocks.append(current)
                continue
            next_blocks.append(current.model_copy(update={"text": segments[0]}))
            for new_uid, segment in zip(op.new_block_uids, segments[1:], strict=True):
                next_blocks.append(current.model_copy(update={"uid": new_uid, "text": segment}))
        chapter.blocks = next_blocks
        return book

    if isinstance(op, DeleteBlock):
        ref, _ = _get_block(book, op.block_uid)
        chapter = book.chapters[ref.chapter_idx]
        chapter.blocks = [block for b_idx, block in enumerate(chapter.blocks) if b_idx != ref.block_idx]
        return book

    if isinstance(op, InsertBlock):
        ch_idx, chapter = _get_chapter(book, op.chapter_uid)
        insert_at = 0
        if op.after_uid is not None:
            ref = _block_index(book).get(op.after_uid)
            if ref is None or ref.chapter_idx != ch_idx:
                raise ApplyError("insert_block after_uid must belong to target chapter", op_id)
            insert_at = ref.block_idx + 1
        new_block = _make_block(op.block_kind, op.new_block_uid, op.block_data)
        chapter.blocks = chapter.blocks[:insert_at] + [new_block] + chapter.blocks[insert_at:]
        return book

    if isinstance(op, FootnoteOp):
        block_map = _block_index(book)
        fn_ref = block_map.get(op.fn_block_uid)
        if fn_ref is None:
            raise ApplyError(f"missing footnote block {op.fn_block_uid}", op_id)
        source_ref = block_map.get(op.source_block_uid) if op.source_block_uid is not None else None
        new_source_ref = block_map.get(op.new_source_block_uid) if op.new_source_block_uid is not None else None
        apply_footnote_mutation(
            book,
            FootnoteMutation(
                op_name=op.op,
                fn_ref=fn_ref,
                source_ref=source_ref,
                new_source_ref=new_source_ref,
                occurrence_index=op.occurrence_index,
            ),
            op_id=op_id,
        )
        return book

    if isinstance(op, MergeChapters):
        chapter_map = _chapter_index(book)
        source_indexes = [chapter_map.get(uid) for uid in op.source_chapter_uids]
        if any(index is None for index in source_indexes):
            raise ApplyError("merge_chapters source chapter missing", op_id)
        positions = [index for index in source_indexes if index is not None]
        insert_at = min(positions)
        new_blocks: list[Block] = []
        for source_uid, section in zip(op.source_chapter_uids, op.sections, strict=True):
            source_chapter = book.chapters[chapter_map[source_uid]]
            new_blocks.append(
                Heading(
                    uid=section.new_block_uid,
                    level=2,
                    text=section.text,
                    id=section.id,
                    style_class=section.style_class,
                    provenance=source_chapter.blocks[0].provenance if source_chapter.blocks else {"page": 0, "source": "passthrough"},
                )
            )
            new_blocks.extend(block.model_copy(deep=True) for block in source_chapter.blocks)
        new_chapter = Chapter(uid=op.new_chapter_uid, title=op.new_title, blocks=new_blocks)
        remaining = [chapter for idx, chapter in enumerate(book.chapters) if idx not in positions]
        book.chapters = remaining[:insert_at] + [new_chapter] + remaining[insert_at:]
        return book

    if isinstance(op, SplitChapter):
        ch_idx, chapter = _get_chapter(book, op.chapter_uid)
        split_ref = _block_index(book).get(op.split_at_block_uid)
        if split_ref is None or split_ref.chapter_idx != ch_idx:
            raise ApplyError("split_chapter split_at_block_uid must belong to chapter_uid", op_id)
        if split_ref.block_idx == 0:
            raise ApplyError("split_chapter requires at least one block in the original chapter", op_id)
        head = [block.model_copy(deep=True) for block in chapter.blocks[: split_ref.block_idx]]
        tail = [block.model_copy(deep=True) for block in chapter.blocks[split_ref.block_idx :]]
        chapter.blocks = head
        new_chapter = Chapter(uid=op.new_chapter_uid, title=op.new_chapter_title, blocks=tail)
        book.chapters = book.chapters[: ch_idx + 1] + [new_chapter] + book.chapters[ch_idx + 1 :]
        return book

    if isinstance(op, RelocateBlock):
        source_ref, block = _get_block(book, op.block_uid)
        target_idx, target_chapter = _get_chapter(book, op.target_chapter_uid)
        if source_ref.chapter_idx == target_idx and op.after_uid == op.block_uid:
            raise ApplyError("relocate_block after_uid cannot point to the moved block", op_id)
        source_chapter = book.chapters[source_ref.chapter_idx]
        source_chapter.blocks = [item for idx, item in enumerate(source_chapter.blocks) if idx != source_ref.block_idx]
        insert_at = 0
        if op.after_uid is not None:
            target_ref = _block_index(book).get(op.after_uid)
            if target_ref is None or target_ref.chapter_idx != target_idx:
                raise ApplyError("relocate_block after_uid must belong to target chapter", op_id)
            insert_at = target_ref.block_idx + 1
        target_chapter.blocks = target_chapter.blocks[:insert_at] + [block] + target_chapter.blocks[insert_at:]
        return book

    if isinstance(op, SplitMergedTable):
        ref, block = _get_block(book, op.block_uid)
        if not isinstance(block, Table):
            raise ApplyError("split_merged_table requires a Table block", op_id)
        if not block.multi_page:
            raise ApplyError("split_merged_table target block is not a multi_page Table", op_id)
        chapter = book.chapters[ref.chapter_idx]
        # Build one new Table per segment; each gets a fresh runtime uid.
        # Provenance page is taken from segment_pages; all other fields are
        # inherited from the merged table (title, caption, etc.).
        new_blocks: list[Block] = []
        for seg_idx, (seg_html, seg_page) in enumerate(
            zip(op.segment_html, op.segment_pages, strict=True)
        ):
            new_uid = str(uuid4())
            seg_table = Table(
                uid=new_uid,
                html=seg_html,
                table_title=block.table_title,
                caption=block.caption if seg_idx == len(op.segment_html) - 1 else "",
                continuation=(seg_idx > 0),
                multi_page=False,
                bbox=block.bbox,
                provenance=block.provenance.model_copy(update={"page": seg_page}),
                merge_record=None,
            )
            new_blocks.append(seg_table)
        # Replace the original merged block with the split sequence in-place.
        chapter.blocks = (
            chapter.blocks[: ref.block_idx]
            + new_blocks
            + chapter.blocks[ref.block_idx + 1 :]
        )
        return book

    raise AssertionError(f"unsupported op type {type(op)!r}")


def _is_topology_op(op: EditOp) -> bool:
    return isinstance(op, (MergeChapters, SplitChapter, RelocateBlock))


def _resolve_intra_chapter_uid(book: Book, op: EditOp, *, op_id: str) -> str | None:
    if isinstance(op, (NoopOp, CompactMarker, RevertOp)):
        return None

    if isinstance(op, InsertBlock):
        ch_idx, _ = _get_chapter(book, op.chapter_uid)
        return _chapter_uid_for_index(book, ch_idx, op_id=op_id)

    if isinstance(op, MergeBlocks):
        ch_idx, _ = _require_same_chapter(book, op.block_uids, op_id=op_id)
        return _chapter_uid_for_index(book, ch_idx, op_id=op_id)

    if isinstance(op, FootnoteOp):
        related_uids = [op.fn_block_uid]
        if op.source_block_uid is not None:
            related_uids.append(op.source_block_uid)
        if op.new_source_block_uid is not None:
            related_uids.append(op.new_source_block_uid)
        ch_idx, _ = _require_same_chapter(book, related_uids, op_id=op_id)
        return _chapter_uid_for_index(book, ch_idx, op_id=op_id)

    if isinstance(
        op,
        (
            SetRole,
            SetStyleClass,
            SetText,
            SetHeadingLevel,
            SetHeadingId,
            SetFootnoteFlag,
            SplitBlock,
            DeleteBlock,
            SplitMergedTable,
        ),
    ):
        ref, _ = _get_block(book, op.block_uid)
        return _chapter_uid_for_index(book, ref.chapter_idx, op_id=op_id)

    return None


def _ensure_lease_access(
    book: Book,
    op: EditOp,
    *,
    op_id: str,
    lease_state: LeaseState | None,
    holder: str,
    now_ts: str,
) -> None:
    if lease_state is None:
        return

    lease_state.expire_stale(now=now_ts)
    if isinstance(op, (NoopOp, CompactMarker, RevertOp)):
        return

    if _is_topology_op(op):
        active = lease_state.book_exclusive
        if active is None or active.holder != holder:
            raise ApplyError(f"topology op requires book-exclusive lease held by {holder}", op_id)
        return

    if lease_state.book_exclusive is not None:
        raise ApplyError("book-exclusive lease is active; chapter ops are paused", op_id)

    chapter_uid = _resolve_intra_chapter_uid(book, op, op_id=op_id)
    if chapter_uid is None:
        return
    active = lease_state.chapter_lease(chapter_uid)
    if active is None or active.holder != holder:
        raise ApplyError(f"intra-chapter op requires chapter lease for {chapter_uid} held by {holder}", op_id)


def _chapter_status_or_default(memory: EditMemory, chapter_uid: str) -> ChapterStatus:
    return memory.chapter_status.get(chapter_uid, ChapterStatus(chapter_uid=chapter_uid))


def _concat_notes(notes: Iterable[str]) -> str:
    values = [note.strip() for note in notes if note.strip()]
    return "\n".join(values)


def _migrate_merge_chapter_status(memory: EditMemory, op: MergeChapters) -> EditMemory:
    merged_sources = [_chapter_status_or_default(memory, chapter_uid) for chapter_uid in op.source_chapter_uids]
    merged = ChapterStatus(
        chapter_uid=op.new_chapter_uid,
        read_passes=max((item.read_passes for item in merged_sources), default=0),
        last_reader=next((item.last_reader for item in reversed(merged_sources) if item.last_reader), None),
        issues_found=sum(item.issues_found for item in merged_sources),
        issues_fixed=sum(item.issues_fixed for item in merged_sources),
        notes=_concat_notes(item.notes for item in merged_sources),
    )
    chapter_status = {
        chapter_uid: status
        for chapter_uid, status in memory.chapter_status.items()
        if chapter_uid not in set(op.source_chapter_uids)
    }
    chapter_status[op.new_chapter_uid] = merged
    return memory.model_copy(update={"chapter_status": chapter_status})


def _migrate_split_chapter_status(memory: EditMemory, op: SplitChapter) -> EditMemory:
    chapter_status = dict(memory.chapter_status)
    chapter_status.setdefault(op.chapter_uid, ChapterStatus(chapter_uid=op.chapter_uid))
    chapter_status[op.new_chapter_uid] = ChapterStatus(
        chapter_uid=op.new_chapter_uid,
        notes=f"split from {op.chapter_uid}",
    )
    return memory.model_copy(update={"chapter_status": chapter_status})


def _migrate_topology_memory(memory: EditMemory, op: EditOp, *, updated_at: str, updated_by: str) -> EditMemory:
    if isinstance(op, MergeChapters):
        migrated = _migrate_merge_chapter_status(memory, op)
    elif isinstance(op, SplitChapter):
        migrated = _migrate_split_chapter_status(memory, op)
    elif isinstance(op, RelocateBlock):
        migrated = memory.model_copy(deep=True)
    else:
        return memory.model_copy(deep=True)
    return migrated.model_copy(update={"updated_at": updated_at, "updated_by": updated_by})


def _target_effect_preconditions(target: OpEnvelope) -> list[Precondition]:
    op = target.op
    if isinstance(op, InsertBlock):
        return [Precondition(kind="block_exists", block_uid=op.new_block_uid)]
    if isinstance(op, SetRole):
        return [Precondition(kind="field_equals", block_uid=op.block_uid, field="role", expected=op.value)]
    if isinstance(op, SetStyleClass):
        return [Precondition(kind="field_equals", block_uid=op.block_uid, field="style_class", expected=op.value)]
    if isinstance(op, SetText):
        return [Precondition(kind="field_equals", block_uid=op.block_uid, field=op.field, expected=op.value)]
    if isinstance(op, SetHeadingLevel):
        return [Precondition(kind="field_equals", block_uid=op.block_uid, field="level", expected=op.value)]
    if isinstance(op, SetHeadingId):
        return [Precondition(kind="field_equals", block_uid=op.block_uid, field="id", expected=op.value)]
    if isinstance(op, SetFootnoteFlag):
        conditions: list[Precondition] = []
        if op.paired is not None:
            conditions.append(Precondition(kind="field_equals", block_uid=op.block_uid, field="paired", expected=op.paired))
        if op.orphan is not None:
            conditions.append(Precondition(kind="field_equals", block_uid=op.block_uid, field="orphan", expected=op.orphan))
        return conditions
    if isinstance(op, FootnoteOp):
        conditions = [Precondition(kind="block_exists", block_uid=op.fn_block_uid)]
        if op.op == "pair_footnote":
            conditions.append(Precondition(kind="field_equals", block_uid=op.fn_block_uid, field="paired", expected=True))
        elif op.op == "unpair_footnote":
            conditions.append(Precondition(kind="field_equals", block_uid=op.fn_block_uid, field="paired", expected=False))
        elif op.op == "mark_orphan":
            conditions.append(Precondition(kind="field_equals", block_uid=op.fn_block_uid, field="orphan", expected=True))
        else:
            conditions.append(Precondition(kind="field_equals", block_uid=op.fn_block_uid, field="paired", expected=True))
        return conditions
    if isinstance(op, NoopOp):
        return []
    raise ApplyError("target op is not safely reversible from logged payload alone", target.op_id)


def _find_precondition_expected_value(target: OpEnvelope, *, block_uid: str, field: str) -> object:
    for precondition in target.preconditions:
        if precondition.kind == "field_equals" and precondition.block_uid == block_uid and precondition.field == field:
            return precondition.expected

    if field in {"paired", "orphan"}:
        for precondition in target.preconditions:
            if precondition.kind != "footnote_paired_state" or precondition.block_uid != block_uid:
                continue
            if field == "paired" and precondition.paired is not None:
                return precondition.paired
            if field == "orphan" and precondition.orphan is not None:
                return precondition.orphan

    raise ApplyError(f"cannot revert without prior {field} precondition for block {block_uid}", target.op_id)


def _split_effect_preconditions(book: Book, target: OpEnvelope, op: SplitBlock) -> list[Precondition]:
    original_text = _find_precondition_expected_value(target, block_uid=op.block_uid, field="text")
    if not isinstance(original_text, str):
        raise ApplyError("split_block revert requires original text precondition", target.op_id)

    ref, current_block = _get_block(book, op.block_uid)
    simulated_source = current_block.model_copy(update={"text": original_text})
    segments = _split_text(simulated_source, op, op_id=target.op_id)
    if len(segments) != len(op.new_block_uids) + 1:
        raise ApplyError("split_block revert could not reconstruct target segments", target.op_id)

    conditions = [
        Precondition(kind="block_exists", block_uid=op.block_uid),
        Precondition(kind="field_equals", block_uid=op.block_uid, field="text", expected=segments[0]),
    ]
    for new_uid, segment in zip(op.new_block_uids, segments[1:], strict=True):
        conditions.append(Precondition(kind="block_exists", block_uid=new_uid))
        conditions.append(Precondition(kind="field_equals", block_uid=new_uid, field="text", expected=segment))
    if ref.chapter_idx < 0:
        raise ApplyError("split_block revert resolved an invalid chapter index", target.op_id)
    return conditions


def _build_inverse_op(book: Book, target: OpEnvelope) -> EditOp:
    op = target.op
    if isinstance(op, InsertBlock):
        return DeleteBlock(op="delete_block", block_uid=op.new_block_uid)
    if isinstance(op, SetRole):
        previous_value = _find_precondition_expected_value(target, block_uid=op.block_uid, field="role")
        if not isinstance(previous_value, str):
            raise ApplyError("cannot revert set_role without prior string value", target.op_id)
        return SetRole(
            op="set_role",
            block_uid=op.block_uid,
            value=previous_value,
        )
    if isinstance(op, SetStyleClass):
        previous_value = _find_precondition_expected_value(target, block_uid=op.block_uid, field="style_class")
        if previous_value is not None and not isinstance(previous_value, str):
            raise ApplyError("cannot revert set_style_class without prior string-or-none value", target.op_id)
        return SetStyleClass(
            op="set_style_class",
            block_uid=op.block_uid,
            value=previous_value,
        )
    if isinstance(op, SetText):
        previous_value = _find_precondition_expected_value(target, block_uid=op.block_uid, field=op.field)
        if not isinstance(previous_value, str):
            raise ApplyError(f"cannot revert set_text without prior string value for {op.field}", target.op_id)
        return SetText(op="set_text", block_uid=op.block_uid, field=op.field, value=previous_value)
    if isinstance(op, SetHeadingLevel):
        previous_value = _find_precondition_expected_value(target, block_uid=op.block_uid, field="level")
        if not isinstance(previous_value, int) or previous_value not in (1, 2, 3):
            raise ApplyError("cannot revert set_heading_level without prior integer value", target.op_id)
        return SetHeadingLevel(
            op="set_heading_level",
            block_uid=op.block_uid,
            value=cast(Literal[1, 2, 3], previous_value),
        )
    if isinstance(op, SetHeadingId):
        previous_value = _find_precondition_expected_value(target, block_uid=op.block_uid, field="id")
        if previous_value is not None and not isinstance(previous_value, str):
            raise ApplyError("cannot revert set_heading_id without prior string-or-none value", target.op_id)
        return SetHeadingId(op="set_heading_id", block_uid=op.block_uid, value=previous_value)
    if isinstance(op, SetFootnoteFlag):
        paired = (
            _find_precondition_expected_value(target, block_uid=op.block_uid, field="paired")
            if op.paired is not None
            else None
        )
        orphan = (
            _find_precondition_expected_value(target, block_uid=op.block_uid, field="orphan")
            if op.orphan is not None
            else None
        )
        if paired is not None and not isinstance(paired, bool):
            raise ApplyError("cannot revert set_footnote_flag without prior boolean paired value", target.op_id)
        if orphan is not None and not isinstance(orphan, bool):
            raise ApplyError("cannot revert set_footnote_flag without prior boolean orphan value", target.op_id)
        return SetFootnoteFlag(op="set_footnote_flag", block_uid=op.block_uid, paired=paired, orphan=orphan)
    if isinstance(op, SplitBlock):
        original_block, _ = _get_block(book, op.block_uid)
        snapshot_uids = [op.block_uid, *op.new_block_uids]
        current_block_map = _block_index(book)
        original_blocks: list[dict[str, object]] = []
        for block_uid in snapshot_uids:
            ref = current_block_map.get(block_uid)
            if ref is None:
                raise ApplyError(f"split_block revert missing split output block {block_uid}", target.op_id)
            original_blocks.append(book.chapters[ref.chapter_idx].blocks[ref.block_idx].model_dump(mode="json"))
        if original_block.chapter_idx < 0:
            raise ApplyError("split_block revert resolved an invalid block reference", target.op_id)
        return MergeBlocks(op="merge_blocks", block_uids=snapshot_uids, join="concat", original_blocks=original_blocks)
    if isinstance(op, FootnoteOp):
        if op.op == "pair_footnote":
            return FootnoteOp(op="unpair_footnote", fn_block_uid=op.fn_block_uid, occurrence_index=op.occurrence_index)
        if op.op == "unpair_footnote":
            if op.source_block_uid is None:
                raise ApplyError("cannot revert unpair_footnote without source_block_uid", target.op_id)
            return FootnoteOp(
                op="pair_footnote",
                fn_block_uid=op.fn_block_uid,
                source_block_uid=op.source_block_uid,
                occurrence_index=op.occurrence_index,
            )
        if op.op == "relink_footnote":
            if op.source_block_uid is None or op.new_source_block_uid is None:
                raise ApplyError("cannot revert relink_footnote without source metadata", target.op_id)
            return FootnoteOp(
                op="relink_footnote",
                fn_block_uid=op.fn_block_uid,
                source_block_uid=op.new_source_block_uid,
                new_source_block_uid=op.source_block_uid,
                occurrence_index=op.occurrence_index,
            )
        source_block_uid = op.source_block_uid
        if source_block_uid is None:
            raise ApplyError("cannot revert mark_orphan without source_block_uid", target.op_id)
        return FootnoteOp(
            op="pair_footnote",
            fn_block_uid=op.fn_block_uid,
            source_block_uid=source_block_uid,
            occurrence_index=op.occurrence_index,
        )
    if isinstance(op, NoopOp):
        return NoopOp(op="noop", purpose="milestone")
    raise ApplyError("target op is not safely reversible from logged payload alone", target.op_id)


def _build_inverse_envelope(
    book: Book,
    target: OpEnvelope,
    revert_request: OpEnvelope,
    *,
    existing_op_ids: Collection[str],
    now: Callable[[], str],
) -> tuple[OpEnvelope, RevertBackref]:
    preconditions = (
        _split_effect_preconditions(book, target, target.op)
        if isinstance(target.op, SplitBlock)
        else _target_effect_preconditions(target)
    )
    inverse_op_id = str(uuid4())
    while inverse_op_id in existing_op_ids or inverse_op_id == revert_request.op_id:
        inverse_op_id = str(uuid4())

    ts = now()
    inverse = OpEnvelope(
        op_id=inverse_op_id,
        ts=ts,
        agent_id="supervisor-revert",
        base_version=book.version,
        preconditions=preconditions,
        op=_build_inverse_op(book, target),
        rationale=f"inverse of op {target.op_id} (requested by revert op {revert_request.op_id})",
        irreversible=False,
    )
    backref = RevertBackref(
        target_op_id=target.op_id,
        revert_op_id=revert_request.op_id,
        inverse_op_id=inverse.op_id,
        ts=ts,
    )
    return inverse, backref


def apply_envelope(
    book: Book,
    env: OpEnvelope,
    *,
    mode: Literal["strict"] = "strict",
    existing_op_ids: Collection[str] = (),
    reverted_target_op_ids: Collection[str] = (),
    resolve_target: Callable[[str], OpEnvelope | None] | None = None,
    now: Callable[[], str] = _utc_now,
    replay: bool = False,
    lease_state: LeaseState | None = None,
    lease_holder: str | None = None,
    memory: EditMemory | None = None,
) -> ApplyResult:
    """Apply a single envelope to a Book snapshot."""

    if mode != "strict":
        raise ValueError(f"unsupported apply mode {mode}")
    if env.op_id in existing_op_ids:
        raise ApplyError("duplicate op_id", env.op_id)
    if env.base_version > book.version:
        raise ApplyError(
            f"future-version rejection: base_version={env.base_version} > book.version={book.version}",
            env.op_id,
        )

    working = book.model_copy(deep=True)
    working_memory = memory.model_copy(deep=True) if memory is not None else None
    holder = lease_holder or env.agent_id
    default_applied_at = env.applied_at or now()

    if isinstance(env.op, CompactMarker):
        if env.applied_version is not None and env.applied_version != working.version:
            raise ApplyError("compact_marker applied_version does not match current book.version", env.op_id)
        applied_at = default_applied_at
        applied = env.model_copy(update={"applied_version": working.version, "applied_at": applied_at})
        return ApplyResult(book=working, accepted_envelopes=(applied,), memory=working_memory)

    if isinstance(env.op, RevertOp):
        if replay:
            applied_at = default_applied_at
            applied = env.model_copy(update={"applied_version": working.version, "applied_at": applied_at})
            return ApplyResult(book=working, accepted_envelopes=(applied,), memory=working_memory)

        if resolve_target is None:
            raise ApplyError("revert requires target lookup", env.op_id)
        if env.op.target_op_id in reverted_target_op_ids:
            raise ApplyError(f"target op {env.op.target_op_id} has already been reverted", env.op_id)

        target = resolve_target(env.op.target_op_id)
        if target is None:
            raise ApplyError(f"target op {env.op.target_op_id} not found", env.op_id)
        if target.irreversible:
            raise ApplyError(f"target op {target.op_id} is irreversible", env.op_id)

        _check_preconditions(working, env.preconditions, op_id=env.op_id)
        revert_applied_at = default_applied_at
        applied_revert = env.model_copy(update={"applied_version": working.version, "applied_at": revert_applied_at})
        inverse, backref = _build_inverse_envelope(
            working,
            target,
            applied_revert,
            existing_op_ids=set(existing_op_ids) | {env.op_id},
            now=now,
        )
        inverse_result = apply_envelope(
            working,
            inverse,
            mode=mode,
            existing_op_ids=set(existing_op_ids) | {env.op_id},
            reverted_target_op_ids=reverted_target_op_ids,
            resolve_target=resolve_target,
            now=now,
            replay=False,
            lease_state=lease_state,
            lease_holder=lease_holder,
            memory=working_memory,
        )
        return ApplyResult(
            book=inverse_result.book,
            accepted_envelopes=(applied_revert, *inverse_result.accepted_envelopes),
            revert_backref=backref,
            memory=inverse_result.memory,
        )

    _ensure_lease_access(
        working,
        env.op,
        op_id=env.op_id,
        lease_state=lease_state,
        holder=holder,
        now_ts=default_applied_at,
    )
    _check_preconditions(working, env.preconditions, op_id=env.op_id)
    _check_new_uid_collisions(working, env.op, op_id=env.op_id)
    updated = _apply_op(working, env.op, op_id=env.op_id)
    updated.version += 1
    applied_at = default_applied_at
    applied = env.model_copy(update={"applied_version": updated.version, "applied_at": applied_at})
    if env.applied_version is not None and env.applied_version != applied.applied_version:
        raise ApplyError("envelope applied_version does not match replay result", env.op_id)
    if working_memory is not None and _is_topology_op(env.op):
        working_memory = _migrate_topology_memory(working_memory, env.op, updated_at=applied_at, updated_by=holder)
    return ApplyResult(book=updated, accepted_envelopes=(applied,), memory=working_memory)


def apply_log(book: Book, log_path: Path, *, from_version: int = 0) -> Book:
    """Replay an accepted edit log against a Book snapshot."""

    current = book.model_copy(deep=True)
    for env in _load_envelopes(log_path):
        if env.applied_version is None:
            raise ApplyError("accepted log entry missing applied_version", env.op_id)
        if env.applied_version <= from_version:
            continue
        result = apply_envelope(current, env, replay=True, now=lambda: env.applied_at or _utc_now())
        current = result.book
    return current


__all__ = [
    "ApplyError",
    "ApplyResult",
    "BlockRef",
    "FootnoteMutation",
    "RevertBackref",
    "apply_footnote_mutation",
    "apply_envelope",
    "apply_log",
]
