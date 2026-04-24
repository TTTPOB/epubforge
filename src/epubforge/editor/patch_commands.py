"""PatchCommand model — high-level ergonomic commands compiled to BookPatch in Phase 3."""

from __future__ import annotations

from typing import Any, Literal

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
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "PatchCommand",
    "PatchCommandOp",
    "PatchCommandError",
    "command_params",
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
