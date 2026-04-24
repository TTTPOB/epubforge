"""Typed editor operation schema for the agentic editing layer."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from epubforge.editor._validators import StrictModel, require_non_empty, validate_utc_iso_timestamp, validate_uuid4
from epubforge.editor.memory import MemoryPatch
from epubforge.ir.semantic import Provenance
from epubforge.ir.style_registry import ALLOWED_ROLES


STYLE_CLASS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SET_TEXT_FIELDS = ("text", "table_title", "caption", "callout", "html")
PRECONDITION_FIELDS = (
    "text",
    "table_title",
    "caption",
    "callout",
    "html",
    "role",
    "style_class",
    "level",
    "id",
    "paired",
    "orphan",
    "cross_page",
)
BLOCK_KINDS = ("paragraph", "heading", "footnote", "figure", "table", "equation")


def _validate_style_class(value: str | None) -> str | None:
    if value is None:
        return None
    value = require_non_empty(value, field_name="style_class")
    if not STYLE_CLASS_PATTERN.fullmatch(value):
        raise ValueError("style_class must use [A-Za-z0-9._-] and start with an alphanumeric character")
    return value


class ParagraphPayload(StrictModel):
    text: str
    role: str = "body"
    display_lines: list[str] | None = None
    style_class: str | None = None
    cross_page: bool = False
    provenance: Provenance

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return require_non_empty(value, field_name="text")

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        value = require_non_empty(value, field_name="role")
        if value not in ALLOWED_ROLES:
            raise ValueError(f"role must be one of {sorted(ALLOWED_ROLES)}")
        return value

    @field_validator("style_class")
    @classmethod
    def _validate_style_class(cls, value: str | None) -> str | None:
        return _validate_style_class(value)


class HeadingPayload(StrictModel):
    level: Literal[1, 2, 3] = 1
    text: str
    id: str | None = None
    style_class: str | None = None
    provenance: Provenance

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return require_non_empty(value, field_name="text")

    @field_validator("id")
    @classmethod
    def _validate_heading_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="id")

    @field_validator("style_class")
    @classmethod
    def _validate_style_class(cls, value: str | None) -> str | None:
        return _validate_style_class(value)


class FootnotePayload(StrictModel):
    callout: str
    text: str
    paired: bool = False
    orphan: bool = False
    ref_bbox: list[float] | None = None
    provenance: Provenance

    @field_validator("callout")
    @classmethod
    def _validate_callout(cls, value: str) -> str:
        return require_non_empty(value, field_name="callout")

    @model_validator(mode="after")
    def _validate_flags(self) -> FootnotePayload:
        if self.paired and self.orphan:
            raise ValueError("footnote payload cannot be paired and orphan at the same time")
        return self


class FigurePayload(StrictModel):
    caption: str = ""
    image_ref: str | None = None
    bbox: list[float] | None = None
    provenance: Provenance


class TablePayload(StrictModel):
    html: str
    table_title: str = ""
    caption: str = ""
    continuation: bool = False
    multi_page: bool = False
    bbox: list[float] | None = None
    provenance: Provenance

    @field_validator("html")
    @classmethod
    def _validate_html(cls, value: str) -> str:
        return require_non_empty(value, field_name="html")


class EquationPayload(StrictModel):
    latex: str = ""
    image_ref: str | None = None
    bbox: list[float] | None = None
    provenance: Provenance


class ParagraphSnapshot(ParagraphPayload):
    kind: Literal["paragraph"] = "paragraph"
    uid: str

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="uid")


class HeadingSnapshot(HeadingPayload):
    kind: Literal["heading"] = "heading"
    uid: str

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="uid")


class FootnoteSnapshot(FootnotePayload):
    kind: Literal["footnote"] = "footnote"
    uid: str

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="uid")


class FigureSnapshot(FigurePayload):
    kind: Literal["figure"] = "figure"
    uid: str

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="uid")


class TableSnapshot(TablePayload):
    kind: Literal["table"] = "table"
    uid: str

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="uid")


class EquationSnapshot(EquationPayload):
    kind: Literal["equation"] = "equation"
    uid: str

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="uid")


BlockSnapshot = Annotated[
    ParagraphSnapshot | HeadingSnapshot | FootnoteSnapshot | FigureSnapshot | TableSnapshot | EquationSnapshot,
    Field(discriminator="kind"),
]

BLOCK_PAYLOAD_MODELS = {
    "paragraph": ParagraphPayload,
    "heading": HeadingPayload,
    "footnote": FootnotePayload,
    "figure": FigurePayload,
    "table": TablePayload,
    "equation": EquationPayload,
}


class Precondition(StrictModel):
    kind: Literal[
        "block_exists",
        "field_equals",
        "chapter_exists",
        "footnote_paired_state",
        "version_at_least",
    ]
    block_uid: str | None = None
    chapter_uid: str | None = None
    field: Literal[
        "text",
        "table_title",
        "caption",
        "callout",
        "html",
        "role",
        "style_class",
        "level",
        "id",
        "paired",
        "orphan",
        "cross_page",
    ] | None = None
    expected: Any = None
    min_version: int | None = Field(default=None, ge=0)
    paired: bool | None = None
    orphan: bool | None = None

    @field_validator("block_uid", "chapter_uid")
    @classmethod
    def _validate_optional_uid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_shape(self) -> Precondition:
        if self.kind == "block_exists":
            if self.block_uid is None:
                raise ValueError("block_exists requires block_uid")
            if any(
                value is not None
                for value in (
                    self.chapter_uid,
                    self.field,
                    self.min_version,
                    self.paired,
                    self.orphan,
                )
            ) or self.expected is not None:
                raise ValueError("block_exists only accepts block_uid")
            return self

        if self.kind == "field_equals":
            if self.block_uid is None or self.field is None:
                raise ValueError("field_equals requires block_uid and field")
            if any(
                value is not None
                for value in (
                    self.chapter_uid,
                    self.min_version,
                    self.paired,
                    self.orphan,
                )
            ):
                raise ValueError("field_equals only accepts block_uid, field, and expected")
            return self

        if self.kind == "chapter_exists":
            if self.chapter_uid is None:
                raise ValueError("chapter_exists requires chapter_uid")
            if any(
                value is not None
                for value in (
                    self.block_uid,
                    self.field,
                    self.min_version,
                    self.paired,
                    self.orphan,
                )
            ) or self.expected is not None:
                raise ValueError("chapter_exists only accepts chapter_uid")
            return self

        if self.kind == "footnote_paired_state":
            if self.block_uid is None:
                raise ValueError("footnote_paired_state requires block_uid")
            if self.paired is None and self.orphan is None:
                raise ValueError("footnote_paired_state requires paired or orphan")
            if self.paired and self.orphan:
                raise ValueError("footnote_paired_state cannot require paired and orphan simultaneously")
            if any(
                value is not None
                for value in (
                    self.chapter_uid,
                    self.field,
                    self.min_version,
                )
            ) or self.expected is not None:
                raise ValueError("footnote_paired_state only accepts block_uid, paired, and orphan")
            return self

        if self.min_version is None:
            raise ValueError("version_at_least requires min_version")
        if any(
            value is not None
            for value in (
                self.block_uid,
                self.chapter_uid,
                self.field,
                self.paired,
                self.orphan,
            )
        ) or self.expected is not None:
            raise ValueError("version_at_least only accepts min_version")
        return self


class SetRole(StrictModel):
    op: Literal["set_role"]
    block_uid: str
    value: str

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        value = require_non_empty(value, field_name="value")
        if value not in ALLOWED_ROLES:
            raise ValueError(f"value must be one of {sorted(ALLOWED_ROLES)}")
        return value


class SetStyleClass(StrictModel):
    op: Literal["set_style_class"]
    block_uid: str
    value: str | None

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str | None) -> str | None:
        return _validate_style_class(value)


class SetText(StrictModel):
    op: Literal["set_text"]
    block_uid: str
    field: Literal["text", "table_title", "caption", "callout", "html"]
    value: str

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")


class SetHeadingLevel(StrictModel):
    op: Literal["set_heading_level"]
    block_uid: str
    value: Literal[1, 2, 3]

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")


class SetHeadingId(StrictModel):
    op: Literal["set_heading_id"]
    block_uid: str
    value: str | None

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="value")


class SetFootnoteFlag(StrictModel):
    op: Literal["set_footnote_flag"]
    block_uid: str
    paired: bool | None = None
    orphan: bool | None = None

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @model_validator(mode="after")
    def _validate_flags(self) -> SetFootnoteFlag:
        if self.paired is None and self.orphan is None:
            raise ValueError("set_footnote_flag requires paired or orphan")
        if self.paired and self.orphan:
            raise ValueError("set_footnote_flag cannot set paired and orphan to True simultaneously")
        return self


class MergeBlocks(StrictModel):
    op: Literal["merge_blocks"]
    block_uids: list[str] = Field(min_length=2)
    join: Literal["concat", "cjk", "newline"] = "cjk"
    target_field: Literal["text"] = "text"
    original_blocks: list[BlockSnapshot] | None = None

    @field_validator("block_uids")
    @classmethod
    def _validate_block_uids(cls, value: list[str]) -> list[str]:
        normalized = [require_non_empty(item, field_name="block_uids") for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("block_uids must be unique")
        return normalized

    @model_validator(mode="after")
    def _validate_original_blocks(self) -> MergeBlocks:
        if self.original_blocks is None:
            return self
        snapshot_uids = [block.uid for block in self.original_blocks]
        if len(snapshot_uids) != len(self.block_uids):
            raise ValueError("original_blocks must match block_uids length")
        if snapshot_uids != self.block_uids:
            raise ValueError("original_blocks must preserve the same uid order as block_uids")
        return self


class SplitBlock(StrictModel):
    op: Literal["split_block"]
    block_uid: str
    strategy: Literal["at_marker", "at_line_index", "at_text_match", "at_sentence"]
    marker_occurrence: int | None = Field(default=None, ge=1)
    line_index: int | None = Field(default=None, ge=0)
    text_match: str | None = None
    max_splits: int = Field(default=1, ge=1)
    new_block_uids: list[str] = Field(min_length=1)

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @field_validator("new_block_uids")
    @classmethod
    def _validate_new_block_uids(cls, value: list[str]) -> list[str]:
        normalized = [require_non_empty(item, field_name="new_block_uids") for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("new_block_uids must be unique")
        return normalized

    @field_validator("text_match")
    @classmethod
    def _validate_text_match(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="text_match")

    @model_validator(mode="after")
    def _validate_strategy(self) -> SplitBlock:
        if len(self.new_block_uids) != self.max_splits:
            raise ValueError("new_block_uids length must equal max_splits")

        if self.strategy == "at_marker":
            if self.marker_occurrence is None:
                raise ValueError("at_marker requires marker_occurrence")
            if self.line_index is not None or self.text_match is not None:
                raise ValueError("at_marker only accepts marker_occurrence")
            return self

        if self.strategy == "at_line_index":
            if self.line_index is None:
                raise ValueError("at_line_index requires line_index")
            if self.marker_occurrence is not None or self.text_match is not None:
                raise ValueError("at_line_index only accepts line_index")
            return self

        if self.strategy == "at_text_match":
            if self.text_match is None:
                raise ValueError("at_text_match requires text_match")
            if self.marker_occurrence is not None or self.line_index is not None:
                raise ValueError("at_text_match only accepts text_match")
            return self

        if self.marker_occurrence is not None or self.line_index is not None or self.text_match is not None:
            raise ValueError("at_sentence does not accept strategy-specific parameters")
        return self


class DeleteBlock(StrictModel):
    op: Literal["delete_block"]
    block_uid: str

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")


class InsertBlock(StrictModel):
    op: Literal["insert_block"]
    chapter_uid: str
    after_uid: str | None = None
    block_kind: Literal["paragraph", "heading", "footnote", "figure", "table", "equation"]
    new_block_uid: str
    block_data: dict[str, Any]

    @field_validator("chapter_uid", "after_uid", "new_block_uid")
    @classmethod
    def _validate_optional_uid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_block_data(self) -> InsertBlock:
        payload_model = BLOCK_PAYLOAD_MODELS[self.block_kind]
        payload = payload_model.model_validate(self.block_data)
        self.block_data = payload.model_dump()
        return self


class FootnoteOp(StrictModel):
    op: Literal["pair_footnote", "unpair_footnote", "relink_footnote", "mark_orphan"]
    fn_block_uid: str
    source_block_uid: str | None = None
    new_source_block_uid: str | None = None
    occurrence_index: int = Field(default=0, ge=0)

    @field_validator("fn_block_uid", "source_block_uid", "new_source_block_uid")
    @classmethod
    def _validate_optional_uid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_shape(self) -> FootnoteOp:
        if self.op == "pair_footnote":
            if self.source_block_uid is None or self.new_source_block_uid is not None:
                raise ValueError("pair_footnote requires source_block_uid and forbids new_source_block_uid")
            return self

        if self.op == "unpair_footnote":
            if self.source_block_uid is not None or self.new_source_block_uid is not None:
                raise ValueError("unpair_footnote only accepts fn_block_uid and occurrence_index")
            return self

        if self.op == "mark_orphan":
            if self.source_block_uid is not None or self.new_source_block_uid is not None:
                raise ValueError("mark_orphan only accepts fn_block_uid and occurrence_index")
            return self

        if self.source_block_uid is None or self.new_source_block_uid is None:
            raise ValueError("relink_footnote requires source_block_uid and new_source_block_uid")
        if self.source_block_uid == self.new_source_block_uid:
            raise ValueError("relink_footnote requires different source_block_uid and new_source_block_uid")
        return self


class HeadingSpec(StrictModel):
    text: str
    id: str | None = None
    style_class: str | None = None
    new_block_uid: str

    @field_validator("text", "new_block_uid")
    @classmethod
    def _validate_required_text(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @field_validator("id")
    @classmethod
    def _validate_heading_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="id")

    @field_validator("style_class")
    @classmethod
    def _validate_style_class(cls, value: str | None) -> str | None:
        return _validate_style_class(value)


class MergeChapters(StrictModel):
    op: Literal["merge_chapters"]
    source_chapter_uids: list[str] = Field(min_length=2)
    new_title: str
    new_chapter_uid: str
    sections: list[HeadingSpec] = Field(min_length=2)

    @field_validator("source_chapter_uids")
    @classmethod
    def _validate_source_uids(cls, value: list[str]) -> list[str]:
        normalized = [require_non_empty(item, field_name="source_chapter_uids") for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_chapter_uids must be unique")
        return normalized

    @field_validator("new_title", "new_chapter_uid")
    @classmethod
    def _validate_required_text(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_sections(self) -> MergeChapters:
        if len(self.sections) != len(self.source_chapter_uids):
            raise ValueError("sections length must equal source_chapter_uids length")
        if len({section.new_block_uid for section in self.sections}) != len(self.sections):
            raise ValueError("sections.new_block_uid values must be unique")
        if self.new_chapter_uid in self.source_chapter_uids:
            raise ValueError("new_chapter_uid must differ from source_chapter_uids")
        return self


class SplitChapter(StrictModel):
    op: Literal["split_chapter"]
    chapter_uid: str
    split_at_block_uid: str
    new_chapter_title: str
    new_chapter_uid: str

    @field_validator("chapter_uid", "split_at_block_uid", "new_chapter_title", "new_chapter_uid")
    @classmethod
    def _validate_required_text(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_new_uid(self) -> SplitChapter:
        if self.chapter_uid == self.new_chapter_uid:
            raise ValueError("new_chapter_uid must differ from chapter_uid")
        return self


class RelocateBlock(StrictModel):
    op: Literal["relocate_block"]
    block_uid: str
    target_chapter_uid: str
    after_uid: str | None = None

    @field_validator("block_uid", "target_chapter_uid", "after_uid")
    @classmethod
    def _validate_optional_uid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name=info.field_name)


class NoopOp(StrictModel):
    op: Literal["noop"]
    purpose: Literal["milestone"]


class CompactMarker(StrictModel):
    op: Literal["compact_marker"]
    compacted_at_version: int = Field(ge=0)
    archive_path: str
    archived_op_count: int = Field(ge=0)

    @field_validator("archive_path")
    @classmethod
    def _validate_archive_path(cls, value: str) -> str:
        return require_non_empty(value, field_name="archive_path")


class RevertOp(StrictModel):
    op: Literal["revert"]
    target_op_id: str

    @field_validator("target_op_id")
    @classmethod
    def _validate_target_op_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="target_op_id")


class SplitMergedTable(StrictModel):
    """Split a multi_page-merged Table back into its constituent per-page segments.

    segment_html and segment_pages must have the same length (>= 2).
    New block uids are assigned at apply time; constituent_block_uids is intentionally
    absent because the original pre-merge uids were not stable when the merge was made.
    """

    op: Literal["split_merged_table"]
    block_uid: str
    segment_html: list[str] = Field(min_length=2)
    segment_pages: list[int] = Field(min_length=2)
    multi_page_was: bool

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @field_validator("segment_html")
    @classmethod
    def _validate_segment_html(cls, value: list[str]) -> list[str]:
        for item in value:
            require_non_empty(item, field_name="segment_html item")
        return value

    @model_validator(mode="after")
    def _validate_lengths_match(self) -> SplitMergedTable:
        if len(self.segment_html) != len(self.segment_pages):
            raise ValueError("segment_html and segment_pages must have the same length")
        return self


class ReplaceBlock(StrictModel):
    """Replace a block in-place with a new block of a potentially different kind.

    original_block is required for optimistic locking — apply rejects if the current
    block does not match this snapshot exactly.  block_data is validated as the target
    block_kind at schema time.  uid is injected by apply (original or new_block_uid).
    """

    op: Literal["replace_block"]
    block_uid: str
    block_kind: Literal["paragraph", "heading", "footnote", "figure", "table", "equation"]
    block_data: dict[str, Any]
    new_block_uid: str | None = None
    original_block: dict[str, Any]

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @field_validator("new_block_uid")
    @classmethod
    def _validate_new_block_uid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="new_block_uid")

    @model_validator(mode="after")
    def _validate_block_data(self) -> ReplaceBlock:
        payload_model = BLOCK_PAYLOAD_MODELS[self.block_kind]
        payload = payload_model.model_validate(self.block_data)
        self.block_data = payload.model_dump()
        return self


class SetParagraphCrossPage(StrictModel):
    """Set the cross_page flag on a Paragraph block."""

    op: Literal["set_paragraph_cross_page"]
    block_uid: str
    value: bool

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")


class SetTableMetadata(StrictModel):
    """Update table metadata fields (title, caption, continuation, multi_page, merge_record).

    original_metadata is required for optimistic locking.  Consistency rules:
    - multi_page=True <=> merge_record is not None (with aligned arrays of length >= 2)
    - multi_page=True => continuation must be False
    - continuation=True => multi_page=False and merge_record=None
    - multi_page=False and continuation=False => merge_record=None
    This op does NOT modify table HTML.
    """

    op: Literal["set_table_metadata"]
    block_uid: str
    table_title: str
    caption: str
    continuation: bool
    multi_page: bool
    merge_record: dict[str, Any] | None = None
    original_metadata: dict[str, Any]

    @field_validator("block_uid")
    @classmethod
    def _validate_block_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="block_uid")

    @model_validator(mode="after")
    def _validate_consistency(self) -> SetTableMetadata:
        if self.merge_record is not None and not self.multi_page:
            raise ValueError("merge_record requires multi_page=True")
        if self.multi_page and self.merge_record is None:
            raise ValueError("multi_page=True requires merge_record")
        if self.multi_page and self.continuation:
            raise ValueError("multi_page=True and continuation=True are mutually exclusive")
        if self.continuation and self.merge_record is not None:
            raise ValueError("continuation=True is incompatible with merge_record")
        if not self.multi_page and not self.continuation and self.merge_record is not None:
            raise ValueError("merge_record must be None when multi_page=False and continuation=False")
        if self.merge_record is not None:
            seg_html = self.merge_record.get("segment_html")
            seg_pages = self.merge_record.get("segment_pages")
            seg_order = self.merge_record.get("segment_order")
            if not isinstance(seg_html, list) or not isinstance(seg_pages, list) or not isinstance(seg_order, list):
                raise ValueError("merge_record must have segment_html, segment_pages, segment_order as lists")
            if len(seg_html) < 2:
                raise ValueError("merge_record segment arrays must have length >= 2")
            if len(seg_html) != len(seg_pages) or len(seg_html) != len(seg_order):
                raise ValueError("merge_record segment arrays must be aligned (same length)")
        return self


EditOp = Annotated[
    SetRole
    | SetStyleClass
    | SetText
    | SetHeadingLevel
    | SetHeadingId
    | SetFootnoteFlag
    | MergeBlocks
    | SplitBlock
    | DeleteBlock
    | InsertBlock
    | FootnoteOp
    | MergeChapters
    | SplitChapter
    | RelocateBlock
    | SplitMergedTable
    | ReplaceBlock
    | SetParagraphCrossPage
    | SetTableMetadata
    | NoopOp
    | CompactMarker
    | RevertOp,
    Field(discriminator="op"),
]


class OpEnvelope(StrictModel):
    op_id: str
    ts: str
    agent_id: str
    base_version: int = Field(ge=0)
    preconditions: list[Precondition] = Field(default_factory=list)
    op: EditOp
    rationale: str
    irreversible: bool = False
    applied_version: int | None = Field(default=None, ge=0)
    applied_at: str | None = None
    memory_patches: list[MemoryPatch] | None = None

    @field_validator("agent_id", "rationale")
    @classmethod
    def _validate_required_text(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @field_validator("op_id")
    @classmethod
    def _validate_op_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="op_id")

    @field_validator("ts", "applied_at")
    @classmethod
    def _validate_timestamp(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return validate_utc_iso_timestamp(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_envelope(self) -> OpEnvelope:
        if (self.applied_version is None) != (self.applied_at is None):
            raise ValueError("applied_version and applied_at must both be set or both be omitted")

        if isinstance(self.op, (MergeBlocks, MergeChapters, SplitChapter, RelocateBlock)):
            self.irreversible = True

        if self.applied_version is not None:
            if self.applied_version < self.base_version:
                raise ValueError("applied_version must be >= base_version")
            if isinstance(self.op, (CompactMarker, RevertOp)) and self.applied_version != self.base_version:
                raise ValueError("compact_marker and revert envelopes must keep applied_version == base_version")

        return self


__all__ = [
    "BlockSnapshot",
    "CompactMarker",
    "DeleteBlock",
    "EditOp",
    "FootnoteOp",
    "HeadingSpec",
    "InsertBlock",
    "MergeBlocks",
    "MergeChapters",
    "NoopOp",
    "OpEnvelope",
    "Precondition",
    "RelocateBlock",
    "ReplaceBlock",
    "RevertOp",
    "SetFootnoteFlag",
    "SetHeadingId",
    "SetHeadingLevel",
    "SetParagraphCrossPage",
    "SetRole",
    "SetStyleClass",
    "SetTableMetadata",
    "SetText",
    "SplitBlock",
    "SplitChapter",
    "SplitMergedTable",
]
