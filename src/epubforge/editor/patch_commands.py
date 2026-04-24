"""PatchCommand model — high-level ergonomic commands compiled to BookPatch in Phase 3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import Field, field_validator, model_validator

from epubforge.editor._validators import StrictModel, require_non_empty

# ---------------------------------------------------------------------------
# Op literal type
# ---------------------------------------------------------------------------

PatchCommandOp = Literal[
    "split_block",
    "merge_blocks",
    "split_chapter",
    "merge_chapters",
    "relocate_block",
    "pair_footnote",
    "unpair_footnote",
    "mark_orphan",
    "split_merged_table",
]

# ---------------------------------------------------------------------------
# Typed params models
# ---------------------------------------------------------------------------


class SplitBlockParams(StrictModel):
    """Params for split_block op."""

    block_uid: str
    strategy: Literal["at_marker", "at_line_index", "at_text_match", "at_sentence"]
    marker_occurrence: int = 1
    line_index: int | None = None
    text_match: str | None = None
    max_splits: int = 1
    new_block_uids: list[str]

    @field_validator("block_uid")
    @classmethod
    def _block_uid_non_empty(cls, v: str) -> str:
        return require_non_empty(v, field_name="block_uid")

    @field_validator("new_block_uids")
    @classmethod
    def _new_block_uids_non_empty(cls, v: list[str]) -> list[str]:
        for uid in v:
            require_non_empty(uid, field_name="new_block_uids item")
        return v

    @model_validator(mode="after")
    def _validate_lengths(self) -> "SplitBlockParams":
        if self.max_splits < 1:
            raise ValueError("max_splits must be >= 1")
        if len(self.new_block_uids) != self.max_splits:
            raise ValueError(
                f"new_block_uids length ({len(self.new_block_uids)}) must equal max_splits ({self.max_splits})"
            )
        return self


class MergeBlocksParams(StrictModel):
    """Params for merge_blocks op."""

    block_uids: list[str]
    join: Literal["concat", "cjk", "newline"] = "concat"
    target_field: str = "text"

    @field_validator("block_uids")
    @classmethod
    def _block_uids_valid(cls, v: list[str]) -> list[str]:
        if len(v) < 2:
            raise ValueError("block_uids must contain at least 2 items")
        for uid in v:
            require_non_empty(uid, field_name="block_uids item")
        return v


class RelocateBlockParams(StrictModel):
    """Params for relocate_block op."""

    block_uid: str
    target_chapter_uid: str
    after_uid: str | None = None

    @field_validator("block_uid", "target_chapter_uid")
    @classmethod
    def _non_empty(cls, v: str, info) -> str:
        return require_non_empty(v, field_name=info.field_name)


class SplitChapterParams(StrictModel):
    """Params for split_chapter op."""

    chapter_uid: str
    split_at_block_uid: str
    new_chapter_title: str
    new_chapter_uid: str

    @field_validator("chapter_uid", "split_at_block_uid", "new_chapter_title", "new_chapter_uid")
    @classmethod
    def _non_empty(cls, v: str, info) -> str:
        return require_non_empty(v, field_name=info.field_name)


class MergeChapterSection(StrictModel):
    """A single section entry within MergeChaptersParams."""

    text: str
    id: str | None = None
    style_class: str | None = None
    new_block_uid: str

    @field_validator("text", "new_block_uid")
    @classmethod
    def _non_empty(cls, v: str, info) -> str:
        return require_non_empty(v, field_name=info.field_name)


class MergeChaptersParams(StrictModel):
    """Params for merge_chapters op."""

    source_chapter_uids: list[str]
    new_title: str
    new_chapter_uid: str
    sections: list[MergeChapterSection]

    @field_validator("source_chapter_uids")
    @classmethod
    def _source_chapter_uids_valid(cls, v: list[str]) -> list[str]:
        if len(v) < 2:
            raise ValueError("source_chapter_uids must contain at least 2 items")
        for uid in v:
            require_non_empty(uid, field_name="source_chapter_uids item")
        return v

    @field_validator("new_title", "new_chapter_uid")
    @classmethod
    def _non_empty(cls, v: str, info) -> str:
        return require_non_empty(v, field_name=info.field_name)


class PairFootnoteParams(StrictModel):
    """Params for pair_footnote op."""

    fn_block_uid: str
    source_block_uid: str
    occurrence_index: int = 0

    @field_validator("fn_block_uid", "source_block_uid")
    @classmethod
    def _non_empty(cls, v: str, info) -> str:
        return require_non_empty(v, field_name=info.field_name)


class UnpairFootnoteParams(StrictModel):
    """Params for unpair_footnote op."""

    fn_block_uid: str
    occurrence_index: int = 0

    @field_validator("fn_block_uid")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        return require_non_empty(v, field_name="fn_block_uid")


class MarkOrphanParams(StrictModel):
    """Params for mark_orphan op."""

    fn_block_uid: str
    occurrence_index: int = 0

    @field_validator("fn_block_uid")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        return require_non_empty(v, field_name="fn_block_uid")


class SplitMergedTableParams(StrictModel):
    """Params for split_merged_table op."""

    block_uid: str
    segment_html: list[str]
    segment_pages: list[int]
    new_block_uids: list[str]

    @field_validator("block_uid")
    @classmethod
    def _block_uid_non_empty(cls, v: str) -> str:
        return require_non_empty(v, field_name="block_uid")

    @field_validator("new_block_uids")
    @classmethod
    def _new_block_uids_non_empty(cls, v: list[str]) -> list[str]:
        for uid in v:
            require_non_empty(uid, field_name="new_block_uids item")
        return v

    @model_validator(mode="after")
    def _validate_lengths(self) -> "SplitMergedTableParams":
        if len(self.segment_html) < 2:
            raise ValueError("segment_html must contain at least 2 items")
        if len(self.segment_pages) < 2:
            raise ValueError("segment_pages must contain at least 2 items")
        if len(self.new_block_uids) < 2:
            raise ValueError("new_block_uids must contain at least 2 items")
        if not (len(self.segment_html) == len(self.segment_pages) == len(self.new_block_uids)):
            raise ValueError(
                "segment_html, segment_pages, and new_block_uids must all have the same length"
            )
        return self


# ---------------------------------------------------------------------------
# Mapping from op to params model
# ---------------------------------------------------------------------------

_PARAMS_MODELS: dict[str, type[StrictModel]] = {
    "split_block": SplitBlockParams,
    "merge_blocks": MergeBlocksParams,
    "split_chapter": SplitChapterParams,
    "merge_chapters": MergeChaptersParams,
    "relocate_block": RelocateBlockParams,
    "pair_footnote": PairFootnoteParams,
    "unpair_footnote": UnpairFootnoteParams,
    "mark_orphan": MarkOrphanParams,
    "split_merged_table": SplitMergedTableParams,
}

# ---------------------------------------------------------------------------
# PatchCommandError
# ---------------------------------------------------------------------------


class PatchCommandError(RuntimeError):
    """Error raised during PatchCommand compilation."""

    def __init__(self, reason: str, command_id: str) -> None:
        self.reason = reason
        self.command_id = command_id
        super().__init__(f"command {command_id}: {reason}")


# ---------------------------------------------------------------------------
# PatchCommand
# ---------------------------------------------------------------------------


class PatchCommand(StrictModel):
    """High-level ergonomic command. Compiled to BookPatch in Phase 3."""

    command_id: str
    op: PatchCommandOp
    agent_id: str
    rationale: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("command_id")
    @classmethod
    def _validate_command_id(cls, value: str) -> str:
        from epubforge.editor._validators import validate_uuid4
        return validate_uuid4(value, field_name="command_id")

    @field_validator("agent_id", "rationale")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_params(self) -> "PatchCommand":
        model_cls = _PARAMS_MODELS.get(self.op)
        if model_cls is None:
            # Should not happen since op is a Literal, but guard anyway
            raise ValueError(f"unknown op: {self.op!r}")
        try:
            model_cls.model_validate(self.params)
        except Exception as exc:
            raise ValueError(
                f"invalid params for op {self.op!r}: {exc}"
            ) from exc
        return self


# ---------------------------------------------------------------------------
# Helper function
# ---------------------------------------------------------------------------


def command_params(command: PatchCommand) -> StrictModel:
    """Parse and return the typed params model for a command.

    Raises ValueError if params don't match the op's expected schema.
    """
    model_cls = _PARAMS_MODELS[command.op]
    return model_cls.model_validate(command.params)


# ---------------------------------------------------------------------------
# WP2: Compiler Infrastructure
# ---------------------------------------------------------------------------

from epubforge.editor.patches import (  # noqa: E402
    BookPatch,
    PatchScope,
    _serialize_field_value as _serialize_field_value,
    apply_book_patch,
)
from epubforge.editor.text_split import split_text  # noqa: E402
from epubforge.fields import iter_block_text_fields  # noqa: E402
from epubforge.ir.semantic import Block, Book, Chapter, Footnote, Paragraph, Table  # noqa: E402
from epubforge.markers import count_raw_callout, has_raw_callout, make_fn_marker, replace_nth_raw  # noqa: E402
from epubforge.query import find_markers  # noqa: E402
from epubforge.text_utils import cjk_join  # noqa: E402


# PatchCommandAgentKind — local copy to avoid circular import from agent_output.py
PatchCommandAgentKind = Literal["scanner", "fixer", "reviewer", "supervisor"]


# ---------------------------------------------------------------------------
# CompiledCommands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledCommands:
    """Result of compiling a list of PatchCommands."""

    patches: list[BookPatch]
    book_after_commands: Book


# ---------------------------------------------------------------------------
# Op-specific compiler registry
# ---------------------------------------------------------------------------

# Type alias for op compiler functions.
# Each compiler: (book: Book, command: PatchCommand, params: StrictModel)
#                -> tuple[list[IRChange], PatchScope]
# WP3-WP6 will populate this dict.
CompilerFn = Callable[[Book, PatchCommand, StrictModel], tuple[list, PatchScope]]

_COMPILERS: dict[str, CompilerFn] = {}


# ---------------------------------------------------------------------------
# Lookup helpers (used by WP3-WP6 compilers)
# ---------------------------------------------------------------------------


def _find_block(book: Book, block_uid: str, command_id: str) -> tuple[Chapter, Block, int]:
    """Find a block by UID across all chapters.

    Returns (chapter, block, block_index_in_chapter).
    Raises PatchCommandError if not found.
    """
    for chapter in book.chapters:
        for i, block in enumerate(chapter.blocks):
            if block.uid == block_uid:
                return chapter, block, i
    raise PatchCommandError(f"block_uid {block_uid!r} not found", command_id)


def _find_chapter(book: Book, chapter_uid: str, command_id: str) -> tuple[Chapter, int]:
    """Find a chapter by UID.

    Returns (chapter, chapter_index_in_book).
    Raises PatchCommandError if not found.
    """
    for i, chapter in enumerate(book.chapters):
        if chapter.uid == chapter_uid:
            return chapter, i
    raise PatchCommandError(f"chapter_uid {chapter_uid!r} not found", command_id)


def _check_uid_collision(book: Book, uid: str, command_id: str) -> None:
    """Ensure uid doesn't already exist in book."""
    for chapter in book.chapters:
        if chapter.uid == uid:
            raise PatchCommandError(f"uid {uid!r} already exists (chapter)", command_id)
        for block in chapter.blocks:
            if block.uid == uid:
                raise PatchCommandError(f"uid {uid!r} already exists (block)", command_id)


# ---------------------------------------------------------------------------
# WP3: split_block compiler
# ---------------------------------------------------------------------------


def _compile_split_block(
    book: Book, command: PatchCommand, params: SplitBlockParams
) -> tuple[list, PatchScope]:
    chapter, block, _block_idx = _find_block(book, params.block_uid, command.command_id)

    # Check text-bearing
    if not hasattr(block, "text"):
        raise PatchCommandError(
            f"split_block only supports text-bearing blocks; got {block.kind}",
            command.command_id,
        )
    text = getattr(block, "text")
    if not isinstance(text, str):
        raise PatchCommandError("block text field must be a string", command.command_id)

    # Check new_block_uids for collisions
    for uid in params.new_block_uids:
        _check_uid_collision(book, uid, command.command_id)

    # Check no duplicate UIDs within the command
    all_new_uids = params.new_block_uids
    if len(set(all_new_uids)) != len(all_new_uids):
        raise PatchCommandError("new_block_uids contains duplicates", command.command_id)

    # Get display_lines for at_line_index
    display_lines = getattr(block, "display_lines", None) if isinstance(block, Paragraph) else None

    # Split text
    try:
        segments = split_text(
            text,
            strategy=params.strategy,
            marker_occurrence=params.marker_occurrence,
            line_index=params.line_index,
            text_match=params.text_match,
            max_splits=params.max_splits,
            display_lines=display_lines,
        )
    except ValueError as exc:
        raise PatchCommandError(str(exc), command.command_id) from exc

    # Validate segment count matches new_block_uids + 1 (original block keeps first segment)
    expected_segments = params.max_splits + 1
    if len(segments) != expected_segments:
        raise PatchCommandError(
            f"split produced {len(segments)} segments but expected {expected_segments}",
            command.command_id,
        )

    changes: list = []

    # 1. SetFieldChange: update original block text to first segment
    old_text = _serialize_field_value(text)
    changes.append({
        "op": "set_field",
        "target_uid": params.block_uid,
        "field": "text",
        "old": old_text,
        "new": segments[0],
    })

    # 2. InsertNodeChange for each subsequent segment
    prev_uid = params.block_uid
    block_dump = block.model_dump(mode="python")
    for i, segment in enumerate(segments[1:]):
        new_uid = params.new_block_uids[i]
        new_node = dict(block_dump)
        new_node["uid"] = new_uid
        new_node["text"] = segment
        changes.append({
            "op": "insert_node",
            "parent_uid": chapter.uid,
            "after_uid": prev_uid,
            "node": new_node,
        })
        prev_uid = new_uid

    scope = PatchScope(chapter_uid=chapter.uid)
    return changes, scope


_COMPILERS["split_block"] = _compile_split_block  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP3: merge_blocks compiler
# ---------------------------------------------------------------------------


def _compile_merge_blocks(
    book: Book, command: PatchCommand, params: MergeBlocksParams
) -> tuple[list, PatchScope]:
    # Find all blocks and verify same chapter, contiguous, correct order
    chapter = None
    block_positions: list[tuple[Block, int]] = []

    for uid in params.block_uids:
        ch, blk, idx = _find_block(book, uid, command.command_id)
        if chapter is None:
            chapter = ch
        elif ch.uid != chapter.uid:
            raise PatchCommandError(
                f"merge_blocks: all blocks must be in same chapter; "
                f"{uid!r} is in {ch.uid!r} but first block is in {chapter.uid!r}",
                command.command_id,
            )
        block_positions.append((blk, idx))

    assert chapter is not None  # block_uids has min 2 items

    # Check contiguous and in order
    indices = [idx for _, idx in block_positions]
    for i in range(1, len(indices)):
        if indices[i] != indices[i - 1] + 1:
            raise PatchCommandError(
                "merge_blocks: blocks must be contiguous in chapter order",
                command.command_id,
            )

    # Check all have the target field as a string
    texts: list[str] = []
    for blk, _ in block_positions:
        field_val = getattr(blk, params.target_field, None)
        if not isinstance(field_val, str):
            raise PatchCommandError(
                f"merge_blocks: block {blk.uid!r} has no text field '{params.target_field}'",
                command.command_id,
            )
        texts.append(field_val)

    # Join texts
    if params.join == "cjk":
        merged_text = cjk_join(texts)
    elif params.join == "newline":
        merged_text = "\n".join(texts)
    else:  # concat
        merged_text = "".join(texts)

    changes: list = []

    # 1. SetFieldChange on first block
    first_block = block_positions[0][0]
    old_text = _serialize_field_value(getattr(first_block, params.target_field))
    changes.append({
        "op": "set_field",
        "target_uid": params.block_uids[0],
        "field": params.target_field,
        "old": old_text,
        "new": merged_text,
    })

    # 2. DeleteNodeChange for remaining blocks
    for uid in reversed(params.block_uids[1:]):
        _ch, blk, _idx = _find_block(book, uid, command.command_id)
        changes.append({
            "op": "delete_node",
            "target_uid": uid,
            "old_node": blk.model_dump(mode="python"),
        })

    scope = PatchScope(chapter_uid=chapter.uid)
    return changes, scope


_COMPILERS["merge_blocks"] = _compile_merge_blocks  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP3: relocate_block compiler
# ---------------------------------------------------------------------------


def _compile_relocate_block(
    book: Book, command: PatchCommand, params: RelocateBlockParams
) -> tuple[list, PatchScope]:
    src_chapter, _block, _idx = _find_block(book, params.block_uid, command.command_id)
    tgt_chapter, _tgt_idx = _find_chapter(book, params.target_chapter_uid, command.command_id)

    if params.after_uid is not None:
        if params.after_uid == params.block_uid:
            raise PatchCommandError(
                "relocate_block: after_uid cannot be the same as block_uid",
                command.command_id,
            )
        # Verify after_uid exists in target chapter
        found = any(blk.uid == params.after_uid for blk in tgt_chapter.blocks)
        if not found:
            raise PatchCommandError(
                f"relocate_block: after_uid {params.after_uid!r} not found in "
                f"target chapter {params.target_chapter_uid!r}",
                command.command_id,
            )

    changes: list = [{
        "op": "move_node",
        "target_uid": params.block_uid,
        "from_parent_uid": src_chapter.uid,
        "to_parent_uid": tgt_chapter.uid,
        "after_uid": params.after_uid,
    }]

    # Same chapter = chapter scope, cross chapter = book-wide
    if src_chapter.uid == tgt_chapter.uid:
        scope = PatchScope(chapter_uid=src_chapter.uid)
    else:
        scope = PatchScope(chapter_uid=None)

    return changes, scope


_COMPILERS["relocate_block"] = _compile_relocate_block  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP4: split_chapter compiler
# ---------------------------------------------------------------------------


def _compile_split_chapter(
    book: Book, command: PatchCommand, params: SplitChapterParams
) -> tuple[list, PatchScope]:
    # 1. Find the chapter
    chapter, _ch_idx = _find_chapter(book, params.chapter_uid, command.command_id)

    # 2. Find split_at_block_uid within that chapter
    split_idx: int | None = None
    for i, blk in enumerate(chapter.blocks):
        if blk.uid == params.split_at_block_uid:
            split_idx = i
            break
    if split_idx is None:
        raise PatchCommandError(
            f"split_at_block_uid {params.split_at_block_uid!r} not found in chapter {params.chapter_uid!r}",
            command.command_id,
        )

    # 3. Split at first block would leave original chapter empty — disallow
    if split_idx == 0:
        raise PatchCommandError(
            "split_at_block_uid is the first block; splitting here would leave the original chapter empty",
            command.command_id,
        )

    # 4. Check new_chapter_uid for collision
    _check_uid_collision(book, params.new_chapter_uid, command.command_id)

    changes: list = []

    # 5a. InsertNodeChange — insert empty new chapter after original chapter
    changes.append({
        "op": "insert_node",
        "parent_uid": None,  # insert into book.chapters
        "after_uid": params.chapter_uid,
        "node": {
            "uid": params.new_chapter_uid,
            "kind": "chapter",
            "title": params.new_chapter_title,
            "level": chapter.level,
            "id": None,
            "blocks": [],
        },
    })

    # 5b. MoveNodeChange for each block from split_at_block_uid to end
    prev_uid: str | None = None
    for blk in chapter.blocks[split_idx:]:
        changes.append({
            "op": "move_node",
            "target_uid": blk.uid,
            "from_parent_uid": params.chapter_uid,
            "to_parent_uid": params.new_chapter_uid,
            "after_uid": prev_uid,
        })
        prev_uid = blk.uid

    # 6. Always book-wide scope for chapter topology commands
    scope = PatchScope(chapter_uid=None)
    return changes, scope


_COMPILERS["split_chapter"] = _compile_split_chapter  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP4: merge_chapters compiler
# ---------------------------------------------------------------------------


def _compile_merge_chapters(
    book: Book, command: PatchCommand, params: MergeChaptersParams
) -> tuple[list, PatchScope]:
    # 1. Find all source chapters — each must exist
    source_chapters: list[tuple[Chapter, int]] = []
    for uid in params.source_chapter_uids:
        ch, idx = _find_chapter(book, uid, command.command_id)
        source_chapters.append((ch, idx))

    # 2. Validate sections count == source_chapter_uids count
    if len(params.sections) != len(params.source_chapter_uids):
        raise PatchCommandError(
            f"sections length ({len(params.sections)}) must equal "
            f"source_chapter_uids length ({len(params.source_chapter_uids)})",
            command.command_id,
        )

    # 3. Check new_chapter_uid for collision
    _check_uid_collision(book, params.new_chapter_uid, command.command_id)

    # 4. Check all section.new_block_uid for collision and mutual uniqueness
    seen_section_uids: set[str] = set()
    for section in params.sections:
        if section.new_block_uid in seen_section_uids:
            raise PatchCommandError(
                f"section new_block_uid {section.new_block_uid!r} is duplicated within sections",
                command.command_id,
            )
        seen_section_uids.add(section.new_block_uid)
        _check_uid_collision(book, section.new_block_uid, command.command_id)

    # 5. Find insertion point: min(source_indexes)
    source_indexes = [idx for _ch, idx in source_chapters]
    min_index = min(source_indexes)
    after_uid: str | None = None if min_index == 0 else book.chapters[min_index - 1].uid

    changes: list = []

    # 6a. InsertNodeChange — insert empty new chapter at insertion point
    changes.append({
        "op": "insert_node",
        "parent_uid": None,
        "after_uid": after_uid,
        "node": {
            "uid": params.new_chapter_uid,
            "kind": "chapter",
            "title": params.new_title,
            "level": 1,
            "id": None,
            "blocks": [],
        },
    })

    # 6b. For each source chapter: insert section heading + move blocks
    last_inserted_uid: str | None = None
    for (source_chapter, _src_idx), section in zip(source_chapters, params.sections):
        # Determine provenance for the section heading from first block (or passthrough)
        if source_chapter.blocks:
            prov = source_chapter.blocks[0].provenance.model_dump(mode="python")
        else:
            prov = {"page": 1, "source": "passthrough"}

        # Insert section heading block into new chapter
        changes.append({
            "op": "insert_node",
            "parent_uid": params.new_chapter_uid,
            "after_uid": last_inserted_uid,
            "node": {
                "uid": section.new_block_uid,
                "kind": "heading",
                "level": 2,
                "text": section.text,
                "id": section.id,
                "style_class": section.style_class,
                "provenance": prov,
            },
        })

        # Move each block from source chapter to new chapter, after the heading
        prev_uid: str = section.new_block_uid
        for block in source_chapter.blocks:
            changes.append({
                "op": "move_node",
                "target_uid": block.uid,
                "from_parent_uid": source_chapter.uid,
                "to_parent_uid": params.new_chapter_uid,
                "after_uid": prev_uid,
            })
            assert block.uid is not None  # run_init guarantees all blocks have UIDs
            prev_uid = block.uid

        # Track last_inserted_uid for the next section heading's after_uid
        # If source chapter had blocks, last block uid; otherwise the heading uid
        if source_chapter.blocks:
            last_inserted_uid = source_chapter.blocks[-1].uid
        else:
            last_inserted_uid = section.new_block_uid

    # 6c. Delete each source chapter (will be empty after moves)
    for (source_chapter, _src_idx) in source_chapters:
        empty_chapter_dump = {
            "kind": "chapter",
            "uid": source_chapter.uid,
            "title": source_chapter.title,
            "level": source_chapter.level,
            "id": source_chapter.id,
            "blocks": [],
        }
        changes.append({
            "op": "delete_node",
            "target_uid": source_chapter.uid,
            "old_node": empty_chapter_dump,
        })

    # 7. Always book-wide scope
    scope = PatchScope(chapter_uid=None)
    return changes, scope


_COMPILERS["merge_chapters"] = _compile_merge_chapters  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP5: pair_footnote compiler
# ---------------------------------------------------------------------------


def _compile_pair_footnote(
    book: Book, command: PatchCommand, params: PairFootnoteParams
) -> tuple[list, PatchScope]:
    # 1. Find footnote block and verify it is a Footnote
    fn_chapter, fn_block, _fn_idx = _find_block(book, params.fn_block_uid, command.command_id)
    if not isinstance(fn_block, Footnote):
        raise PatchCommandError(
            f"block {params.fn_block_uid!r} is not a footnote (kind={fn_block.kind!r})",
            command.command_id,
        )
    fn = fn_block

    # 2. Find source block
    source_chapter, source_block, _source_idx = _find_block(
        book, params.source_block_uid, command.command_id
    )

    # 3. Find the text field that contains the raw callout
    found_field: str | None = None
    found_value: str | None = None
    for field_name, field_value in iter_block_text_fields(source_block):
        if has_raw_callout(field_value, fn.callout):
            found_field = field_name
            found_value = field_value
            break

    if found_field is None or found_value is None:
        raise PatchCommandError(
            f"source block {params.source_block_uid!r} has no raw callout {fn.callout!r}",
            command.command_id,
        )

    # 4. Validate occurrence_index
    callout_count = count_raw_callout(found_value, fn.callout)
    if params.occurrence_index >= callout_count:
        raise PatchCommandError(
            f"occurrence_index {params.occurrence_index} is out of range "
            f"(callout {fn.callout!r} appears {callout_count} time(s) in field {found_field!r})",
            command.command_id,
        )

    # 5. Generate replacement text: raw callout → fn marker
    marker = make_fn_marker(fn.provenance.page, fn.callout)
    new_text = replace_nth_raw(found_value, fn.callout, marker, params.occurrence_index)

    changes: list = []

    # 6a. If footnote is orphan=True, clear it first
    if fn.orphan:
        changes.append({
            "op": "set_field",
            "target_uid": params.fn_block_uid,
            "field": "orphan",
            "old": True,
            "new": False,
        })

    # 6b. Update source block text field: raw callout → marker
    changes.append({
        "op": "set_field",
        "target_uid": params.source_block_uid,
        "field": found_field,
        "old": _serialize_field_value(found_value),
        "new": new_text,
    })

    # 6c. Set paired=True
    changes.append({
        "op": "set_field",
        "target_uid": params.fn_block_uid,
        "field": "paired",
        "old": fn.paired,
        "new": True,
    })

    # 7. Determine scope
    if fn_chapter.uid == source_chapter.uid:
        scope = PatchScope(chapter_uid=fn_chapter.uid)
    else:
        scope = PatchScope(chapter_uid=None)

    return changes, scope


_COMPILERS["pair_footnote"] = _compile_pair_footnote  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP5: unpair_footnote compiler
# ---------------------------------------------------------------------------


def _compile_unpair_footnote(
    book: Book, command: PatchCommand, params: UnpairFootnoteParams
) -> tuple[list, PatchScope]:
    # 1. Find footnote block and verify it is a Footnote
    fn_chapter, fn_block, _fn_idx = _find_block(book, params.fn_block_uid, command.command_id)
    if not isinstance(fn_block, Footnote):
        raise PatchCommandError(
            f"block {params.fn_block_uid!r} is not a footnote (kind={fn_block.kind!r})",
            command.command_id,
        )
    fn = fn_block

    # 2. Must be currently paired
    if not fn.paired:
        raise PatchCommandError(
            f"footnote {params.fn_block_uid!r} is not currently paired",
            command.command_id,
        )

    # 3. Find marker in the book
    markers = find_markers(book, page=fn.provenance.page, callout=fn.callout)
    if not markers:
        raise PatchCommandError(
            f"footnote {params.fn_block_uid!r} is paired but no marker found in book",
            command.command_id,
        )
    if params.occurrence_index >= len(markers):
        raise PatchCommandError(
            f"occurrence_index {params.occurrence_index} is out of range "
            f"(found {len(markers)} marker(s))",
            command.command_id,
        )
    marker_ref = markers[params.occurrence_index]

    # 4. Reconstruct source text: replace marker with raw callout
    current_value = getattr(marker_ref.block, marker_ref.field)
    new_value = current_value.replace(marker_ref.marker, fn.callout, 1)

    changes: list = []

    # 5a. Update source field: marker → raw callout
    changes.append({
        "op": "set_field",
        "target_uid": marker_ref.block.uid,
        "field": marker_ref.field,
        "old": _serialize_field_value(current_value),
        "new": new_value,
    })

    # 5b. Set paired=False
    changes.append({
        "op": "set_field",
        "target_uid": params.fn_block_uid,
        "field": "paired",
        "old": True,
        "new": False,
    })

    # 6. Determine scope
    assert marker_ref.block.uid is not None
    marker_chapter, _marker_block, _marker_idx = _find_block(
        book, marker_ref.block.uid, command.command_id
    )
    if fn_chapter.uid == marker_chapter.uid:
        scope = PatchScope(chapter_uid=fn_chapter.uid)
    else:
        scope = PatchScope(chapter_uid=None)

    return changes, scope


_COMPILERS["unpair_footnote"] = _compile_unpair_footnote  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP5: mark_orphan compiler
# ---------------------------------------------------------------------------


def _compile_mark_orphan(
    book: Book, command: PatchCommand, params: MarkOrphanParams
) -> tuple[list, PatchScope]:
    # 1. Find footnote block and verify it is a Footnote
    fn_chapter, fn_block, _fn_idx = _find_block(book, params.fn_block_uid, command.command_id)
    if not isinstance(fn_block, Footnote):
        raise PatchCommandError(
            f"block {params.fn_block_uid!r} is not a footnote (kind={fn_block.kind!r})",
            command.command_id,
        )
    fn = fn_block

    # 2. Must not already be orphan
    if fn.orphan:
        raise PatchCommandError(
            f"footnote {params.fn_block_uid!r} is already marked as orphan",
            command.command_id,
        )

    # 3. Check if a marker exists
    markers = find_markers(book, page=fn.provenance.page, callout=fn.callout)

    changes: list = []

    if markers and params.occurrence_index < len(markers):
        # Marker exists — restore source field and clear paired
        marker_ref = markers[params.occurrence_index]
        current_value = getattr(marker_ref.block, marker_ref.field)
        new_value = current_value.replace(marker_ref.marker, fn.callout, 1)

        # Restore source field: marker → raw callout
        changes.append({
            "op": "set_field",
            "target_uid": marker_ref.block.uid,
            "field": marker_ref.field,
            "old": _serialize_field_value(current_value),
            "new": new_value,
        })

        # Clear paired if needed
        if fn.paired:
            changes.append({
                "op": "set_field",
                "target_uid": params.fn_block_uid,
                "field": "paired",
                "old": True,
                "new": False,
            })

        # Set orphan=True
        changes.append({
            "op": "set_field",
            "target_uid": params.fn_block_uid,
            "field": "orphan",
            "old": False,
            "new": True,
        })

        # Scope: consider both fn chapter and marker source chapter
        assert marker_ref.block.uid is not None
        marker_chapter, _marker_block, _marker_idx = _find_block(
            book, marker_ref.block.uid, command.command_id
        )
        if fn_chapter.uid == marker_chapter.uid:
            scope = PatchScope(chapter_uid=fn_chapter.uid)
        else:
            scope = PatchScope(chapter_uid=None)

    else:
        # No marker — just set paired=False (if needed) and orphan=True
        if fn.paired:
            changes.append({
                "op": "set_field",
                "target_uid": params.fn_block_uid,
                "field": "paired",
                "old": True,
                "new": False,
            })

        changes.append({
            "op": "set_field",
            "target_uid": params.fn_block_uid,
            "field": "orphan",
            "old": False,
            "new": True,
        })

        # Chapter-scoped (fn chapter only)
        scope = PatchScope(chapter_uid=fn_chapter.uid)

    return changes, scope


_COMPILERS["mark_orphan"] = _compile_mark_orphan  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WP6: split_merged_table compiler
# ---------------------------------------------------------------------------


def _compile_split_merged_table(
    book: Book, command: PatchCommand, params: SplitMergedTableParams
) -> tuple[list, PatchScope]:
    # 1. Find the block and verify it is a Table
    chapter, block, block_idx = _find_block(book, params.block_uid, command.command_id)
    if not isinstance(block, Table):
        raise PatchCommandError(
            f"block {params.block_uid!r} is not a table (kind={block.kind!r})",
            command.command_id,
        )
    table = block

    # 2. Verify table.multi_page is True
    if not table.multi_page:
        raise PatchCommandError(
            f"table {params.block_uid!r} is not a multi-page merged table (multi_page=False)",
            command.command_id,
        )

    # 3. Check all new_block_uids for collision and mutual uniqueness
    seen_uids: set[str] = set()
    for uid in params.new_block_uids:
        if uid in seen_uids:
            raise PatchCommandError(
                f"new_block_uids contains duplicate: {uid!r}",
                command.command_id,
            )
        seen_uids.add(uid)
        _check_uid_collision(book, uid, command.command_id)

    # 4. Record previous_uid: block before the table in the chapter
    previous_uid: str | None = None
    if block_idx > 0:
        previous_uid = chapter.blocks[block_idx - 1].uid

    changes: list = []

    # 5a. DeleteNodeChange — delete the original merged table
    changes.append({
        "op": "delete_node",
        "target_uid": params.block_uid,
        "old_node": table.model_dump(mode="python"),
    })

    # 5b. InsertNodeChange for each segment
    n = len(params.segment_html)
    for i in range(n):
        after_uid = previous_uid if i == 0 else params.new_block_uids[i - 1]
        changes.append({
            "op": "insert_node",
            "parent_uid": chapter.uid,
            "after_uid": after_uid,
            "node": {
                "uid": params.new_block_uids[i],
                "kind": "table",
                "html": params.segment_html[i],
                "table_title": table.table_title,
                "caption": table.caption if i == n - 1 else "",
                "continuation": i > 0,
                "multi_page": False,
                "bbox": table.bbox,
                "provenance": {
                    "page": params.segment_pages[i],
                    "source": table.provenance.source,
                },
            },
        })

    # 6. Chapter-scoped: all changes happen within one chapter
    scope = PatchScope(chapter_uid=chapter.uid)
    return changes, scope


_COMPILERS["split_merged_table"] = _compile_split_merged_table  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# compile_patch_command
# ---------------------------------------------------------------------------


def compile_patch_command(
    book: Book,
    command: PatchCommand,
    *,
    output_kind: PatchCommandAgentKind,
    output_chapter_uid: str | None,
) -> BookPatch:
    """Compile a single PatchCommand into a BookPatch.

    Raises PatchCommandError on compilation failure.
    Does NOT apply the patch (caller must call apply_book_patch).
    """
    params = command_params(command)

    compiler_fn = _COMPILERS.get(command.op)
    if compiler_fn is None:
        raise PatchCommandError(
            f"compiler for op {command.op!r} is not implemented",
            command.command_id,
        )

    changes, scope = compiler_fn(book, command, params)

    return BookPatch(
        patch_id=command.command_id,
        agent_id=command.agent_id,
        scope=scope,
        changes=changes,
        rationale=command.rationale,
    )


# ---------------------------------------------------------------------------
# compile_patch_commands
# ---------------------------------------------------------------------------


def compile_patch_commands(
    book: Book,
    commands: list[PatchCommand],
    *,
    output_kind: PatchCommandAgentKind,
    output_chapter_uid: str | None,
) -> CompiledCommands:
    """Compile a list of PatchCommands into BookPatches with an evolving book.

    Maintains state: each command is compiled against the book resulting from
    applying all previous commands' patches. This enables command chains where
    later commands reference UIDs or text created by earlier commands.

    On first failure: raises PatchCommandError. Caller (validate_agent_output)
    should catch and decide how to handle remaining commands.
    """
    patches: list[BookPatch] = []
    current_book = book

    for command in commands:
        patch = compile_patch_command(
            current_book,
            command,
            output_kind=output_kind,
            output_chapter_uid=output_chapter_uid,
        )
        current_book = apply_book_patch(current_book, patch)
        patches.append(patch)

    return CompiledCommands(patches=patches, book_after_commands=current_book)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "PatchCommand",
    "PatchCommandOp",
    "PatchCommandError",
    "PatchCommandAgentKind",
    "CompiledCommands",
    "command_params",
    "compile_patch_command",
    "compile_patch_commands",
    "SplitBlockParams",
    "MergeBlocksParams",
    "RelocateBlockParams",
    "SplitChapterParams",
    "MergeChapterSection",
    "MergeChaptersParams",
    "PairFootnoteParams",
    "UnpairFootnoteParams",
    "MarkOrphanParams",
    "SplitMergedTableParams",
]
