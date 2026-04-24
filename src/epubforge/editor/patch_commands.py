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
from epubforge.ir.semantic import Block, Book, Chapter, Paragraph  # noqa: E402
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
