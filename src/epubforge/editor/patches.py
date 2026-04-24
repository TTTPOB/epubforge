"""BookPatch model, IRChange union, and transactional apply/validate functions.

This module provides:
- PatchError: runtime error for patch validation/apply failures
- PatchScope: patch scope declaration
- Five IRChange types: SetFieldChange, ReplaceNodeChange, InsertNodeChange,
  DeleteNodeChange, MoveNodeChange
- IRChange: discriminated union of the five types
- BookPatch: top-level patch container
- validate_book_patch(): lightweight static pre-check (no deep copy)
- apply_book_patch(): transactional validate+apply (single deep copy)
"""

from __future__ import annotations

import dataclasses
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from epubforge.editor._validators import StrictModel, require_non_empty, validate_uuid4
from epubforge.ir.semantic import (
    Block,
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Provenance,
    Table,
)
from epubforge.ir.style_registry import ALLOWED_ROLES


# ---------------------------------------------------------------------------
# PatchError
# ---------------------------------------------------------------------------


class PatchError(RuntimeError):
    """Raised when a patch cannot be validated or applied."""

    def __init__(self, reason: str, patch_id: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.patch_id = patch_id


# ---------------------------------------------------------------------------
# PatchScope
# ---------------------------------------------------------------------------


class PatchScope(StrictModel):
    """Declares the scope of a BookPatch.

    chapter_uid non-None: patch may only touch nodes within that chapter.
    chapter_uid=None: patch may touch any node (including cross-chapter).
    """

    chapter_uid: str | None = None


# ---------------------------------------------------------------------------
# IRChange models
# ---------------------------------------------------------------------------


class SetFieldChange(StrictModel):
    """Set a single field on a block or chapter node."""

    op: Literal["set_field"]
    target_uid: str
    field: str
    old: Any  # JSON-compatible precondition value
    new: Any  # JSON-compatible target value

    @field_validator("target_uid", "field")
    @classmethod
    def _require_non_empty(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _no_op_check(self) -> SetFieldChange:
        if self.old == self.new:
            raise ValueError("set_field old and new must differ")
        return self


class ReplaceNodeChange(StrictModel):
    """Replace an entire block node (may change kind).

    Only valid for blocks, not chapters. Use SetFieldChange for chapter metadata.
    new_node must not contain uid (injected at apply time from target_uid).
    """

    op: Literal["replace_node"]
    target_uid: str
    old_node: dict[str, Any]  # full snapshot for precondition check (mode="python")
    new_node: dict[
        str, Any
    ]  # replacement content; must include kind, must NOT include uid

    @model_validator(mode="after")
    def _validate_new_node(self) -> ReplaceNodeChange:
        if "uid" in self.new_node:
            raise ValueError(
                "new_node must not contain uid — it is injected at apply time"
            )
        if "kind" not in self.new_node:
            raise ValueError("new_node must contain a kind field")
        return self


class InsertNodeChange(StrictModel):
    """Insert a new block into a chapter, or a new chapter into the book.

    parent_uid=chapter_uid: insert block into that chapter.
    parent_uid=None: insert chapter into book.chapters.
    after_uid=None: insert at the front of the container.
    node must include both uid and kind.
    """

    op: Literal["insert_node"]
    parent_uid: str | None
    after_uid: str | None
    node: dict[str, Any]

    @model_validator(mode="after")
    def _validate_node(self) -> InsertNodeChange:
        if "uid" not in self.node:
            raise ValueError("insert_node.node must contain a uid field")
        if "kind" not in self.node:
            raise ValueError("insert_node.node must contain a kind field")
        return self


class DeleteNodeChange(StrictModel):
    """Delete a block or empty chapter node.

    old_node is the full snapshot for optimistic-locking precondition check
    (serialized with model_dump(mode='python')).
    For chapters, blocks must already be empty (or pre-deleted in this patch).
    """

    op: Literal["delete_node"]
    target_uid: str
    old_node: dict[str, Any]


class MoveNodeChange(StrictModel):
    """Move a block between (or within) chapters, or reorder a chapter.

    from_parent_uid=None and to_parent_uid=None: move chapter within book.chapters.
    Otherwise: move block between chapters (cross-chapter or same-chapter).
    after_uid=None: place at the front of the target container.
    after_uid must differ from target_uid.
    """

    op: Literal["move_node"]
    target_uid: str
    from_parent_uid: str | None
    to_parent_uid: str | None
    after_uid: str | None

    @model_validator(mode="after")
    def _validate_no_self_ref(self) -> MoveNodeChange:
        if self.after_uid is not None and self.after_uid == self.target_uid:
            raise ValueError("move_node after_uid must differ from target_uid")
        return self


# ---------------------------------------------------------------------------
# IRChange discriminated union
# ---------------------------------------------------------------------------

IRChange = Annotated[
    SetFieldChange
    | ReplaceNodeChange
    | InsertNodeChange
    | DeleteNodeChange
    | MoveNodeChange,
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# BookPatch
# ---------------------------------------------------------------------------


class BookPatch(StrictModel):
    """Top-level patch container.

    patch_id: UUID4, used as context key in PatchError.
    agent_id: non-empty string identifying the originating agent.
    scope: restricts which nodes the patch may touch.
    changes: ordered list of atomic IRChange operations (min 1).
    rationale: non-empty explanation for the changes.
    evidence_refs: optional list of VLMObservation ids or other evidence refs.
    """

    patch_id: str
    agent_id: str
    scope: PatchScope
    changes: list[IRChange] = Field(min_length=1)
    rationale: str
    evidence_refs: list[str] = []

    @field_validator("patch_id")
    @classmethod
    def _validate_patch_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="patch_id")

    @field_validator("agent_id", "rationale")
    @classmethod
    def _validate_required_text(cls, value: str, info: Any) -> str:
        return require_non_empty(value, field_name=info.field_name)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STYLE_CLASS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BLOCK_KINDS = ("paragraph", "heading", "footnote", "figure", "table", "equation")


def _validate_style_class_value(value: str | None) -> str | None:
    if value is None:
        return None
    value = require_non_empty(value, field_name="style_class")
    if not STYLE_CLASS_PATTERN.fullmatch(value):
        raise ValueError(
            "style_class must use [A-Za-z0-9._-] and start with an alphanumeric character"
        )
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
        return _validate_style_class_value(value)


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
        return _validate_style_class_value(value)


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
            raise ValueError(
                "footnote payload cannot be paired and orphan at the same time"
            )
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


BLOCK_PAYLOAD_MODELS = {
    "paragraph": ParagraphPayload,
    "heading": HeadingPayload,
    "footnote": FootnotePayload,
    "figure": FigurePayload,
    "table": TablePayload,
    "equation": EquationPayload,
}

# Fields that cannot be modified via SetFieldChange
_IMMUTABLE_FIELDS: frozenset[str] = frozenset({"uid", "kind", "provenance"})

# Per-kind allowed fields for SetFieldChange
_ALLOWED_SET_FIELD: dict[str, frozenset[str]] = {
    "paragraph": frozenset(
        {"text", "role", "style_class", "cross_page", "display_lines"}
    ),
    "heading": frozenset({"text", "level", "id", "style_class"}),
    "footnote": frozenset({"callout", "text", "paired", "orphan", "ref_bbox"}),
    "figure": frozenset({"caption", "image_ref", "bbox"}),
    "table": frozenset(
        {"html", "table_title", "caption", "continuation", "multi_page", "bbox"}
    ),
    "equation": frozenset({"latex", "image_ref", "bbox"}),
    "chapter": frozenset({"title", "level", "id"}),
}

# Heading.level in IR is plain int; enforce valid range here
_VALID_HEADING_LEVELS: frozenset[int] = frozenset({1, 2, 3})

# Chapter.level in IR is plain int; enforce valid range here
_VALID_CHAPTER_LEVELS: frozenset[int] = frozenset({1, 2, 3})

# Map block kind -> IR class for _make_block
_KIND_TO_CLASS: dict[str, type] = {
    "paragraph": Paragraph,
    "heading": Heading,
    "footnote": Footnote,
    "figure": Figure,
    "table": Table,
    "equation": Equation,
}


# ---------------------------------------------------------------------------
# Internal index dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _BookIndex:
    block_index: dict[str, tuple[int, int]]  # uid -> (chapter_idx, block_idx)
    chapter_index: dict[str, int]  # uid -> chapter_idx


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_index(book: Book) -> _BookIndex:
    """Build a UID lookup index from a Book."""
    block_index: dict[str, tuple[int, int]] = {}
    chapter_index: dict[str, int] = {}
    for ch_idx, chapter in enumerate(book.chapters):
        if chapter.uid is not None:
            chapter_index[chapter.uid] = ch_idx
        for b_idx, block in enumerate(chapter.blocks):
            if block.uid is not None:
                block_index[block.uid] = (ch_idx, b_idx)
    return _BookIndex(block_index=block_index, chapter_index=chapter_index)


def _get_node(book: Book, uid: str, index: _BookIndex) -> Block | Chapter:
    """Return the Block or Chapter with the given uid, or raise KeyError."""
    if uid in index.block_index:
        ch_idx, b_idx = index.block_index[uid]
        return book.chapters[ch_idx].blocks[b_idx]
    if uid in index.chapter_index:
        ch_idx = index.chapter_index[uid]
        return book.chapters[ch_idx]
    raise KeyError(uid)


def _get_node_field_value(book: Book, uid: str, field: str, index: _BookIndex) -> Any:
    """Return the raw Python value of a named field on the identified node."""
    node = _get_node(book, uid, index)
    return getattr(node, field)


def _serialize_field_value(value: Any) -> Any:
    """Serialize a field value for precondition comparison.

    Pydantic model instances are serialized with model_dump(mode='json').
    JSON-compatible primitives are returned as-is.
    Lists are serialized element-wise.
    Dicts are serialized value-wise (values that are BaseModel instances are serialized).
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_serialize_field_value(item) for item in value]
    if isinstance(value, dict):
        return {k: _serialize_field_value(v) for k, v in value.items()}
    # str, int, float, bool, None — return directly
    return value


def _block_pos_in_chapter(chapter: Chapter, uid: str, patch_id: str) -> int:
    """Return the index of the block with the given uid within chapter.blocks."""
    for i, block in enumerate(chapter.blocks):
        if block.uid == uid:
            return i
    raise PatchError(
        f"after_uid {uid!r} not found in chapter {chapter.uid!r}",
        patch_id,
    )


def _make_block(kind: str, uid: str, data: dict[str, Any]) -> Block:
    """Construct a Block instance from kind + uid + payload dict."""
    cls = _KIND_TO_CLASS[kind]
    node_data = dict(data)
    node_data["uid"] = uid
    node_data["kind"] = kind
    return cls.model_validate(node_data)  # type: ignore[return-value]


def _validated_set_field_node(
    node: Block | Chapter,
    node_kind: str,
    field: str,
    new_value: Any,
    patch_id: str,
) -> Block | Chapter:
    """Return a validated node copy with one field changed."""
    data = node.model_dump(mode="python")
    data[field] = new_value

    try:
        if node_kind == "chapter":
            return Chapter.model_validate(data)

        assert node_kind in BLOCK_KINDS
        payload_cls = BLOCK_PAYLOAD_MODELS[node_kind]
        payload_data = {
            key: value
            for key, value in data.items()
            if key not in ("uid", "kind", "merge_record")
        }
        payload_cls.model_validate(payload_data)
        cls = _KIND_TO_CLASS[node_kind]
        return cls.model_validate(data)  # type: ignore[return-value]
    except Exception as exc:
        raise PatchError(
            f"set_field value validation failed for {node_kind}.{field}: {exc}",
            patch_id,
        ) from exc


def _require_all_uids_non_none(book: Book, patch_id: str) -> None:
    """Verify every chapter and block in the book has a non-None uid.

    BookPatch can only operate on fully-initialized books where all nodes
    have been assigned stable uids (typically by uid_init stage).
    """
    for ch_idx, chapter in enumerate(book.chapters):
        if chapter.uid is None:
            raise PatchError(
                f"chapter at index {ch_idx} has uid=None — "
                "BookPatch requires all chapters and blocks to have non-None uids. "
                "Run uid_init stage before applying patches.",
                patch_id,
            )
        for b_idx, block in enumerate(chapter.blocks):
            if block.uid is None:
                raise PatchError(
                    f"block at chapter[{ch_idx}].blocks[{b_idx}] (kind={block.kind}) "
                    "has uid=None — BookPatch requires all nodes to have non-None uids.",
                    patch_id,
                )


# ---------------------------------------------------------------------------
# Footnote invariant check
# ---------------------------------------------------------------------------


def _check_footnote_invariants(book: Book, patch_id: str) -> None:
    """Verify no footnote block is simultaneously paired and orphan."""
    for chapter in book.chapters:
        for block in chapter.blocks:
            if block.kind != "footnote":
                continue
            if block.paired and block.orphan:  # type: ignore[union-attr]
                raise PatchError(
                    f"footnote {block.uid} cannot be both paired and orphan",
                    patch_id,
                )


# ---------------------------------------------------------------------------
# Precondition checks (incremental, against evolving working copy)
# ---------------------------------------------------------------------------


def _check_set_field_preconditions(
    working: Book,
    change: SetFieldChange,
    index: _BookIndex,
    patch_id: str,
) -> None:
    """Check preconditions and semantic validity for a SetFieldChange."""
    # Resolve node and determine its kind
    if change.target_uid in index.block_index:
        node = _get_node(working, change.target_uid, index)
        node_kind = node.kind  # type: ignore[union-attr]
    elif change.target_uid in index.chapter_index:
        node_kind = "chapter"
        node = _get_node(working, change.target_uid, index)
    else:
        raise PatchError(
            f"set_field target_uid {change.target_uid!r} not found",
            patch_id,
        )

    # Immutable field guard
    if change.field in _IMMUTABLE_FIELDS:
        raise PatchError(
            f"field {change.field!r} is immutable and cannot be changed via set_field",
            patch_id,
        )

    # Allowed field guard
    allowed = _ALLOWED_SET_FIELD.get(node_kind, frozenset())
    if change.field not in allowed:
        raise PatchError(
            f"field {change.field!r} is not editable for kind {node_kind!r}",
            patch_id,
        )

    # Precondition: compare serialized current value against change.old
    current_value = getattr(node, change.field)
    current_serialized = _serialize_field_value(current_value)
    if current_serialized != change.old:
        raise PatchError(
            f"set_field precondition mismatch for {change.target_uid}.{change.field}: "
            f"expected old={change.old!r}, got {current_serialized!r}",
            patch_id,
        )

    _validated_set_field_node(node, node_kind, change.field, change.new, patch_id)

    # Semantic checks on new value
    if change.field == "role":
        if not isinstance(change.new, str) or change.new not in ALLOWED_ROLES:
            raise PatchError(
                f"set_field role {change.new!r} is not a valid role "
                f"(allowed: {sorted(ALLOWED_ROLES)})",
                patch_id,
            )

    if change.field == "level":
        if node_kind == "heading":
            if change.new not in _VALID_HEADING_LEVELS:
                raise PatchError(
                    f"set_field heading level {change.new!r} is not valid "
                    f"(allowed: {sorted(_VALID_HEADING_LEVELS)})",
                    patch_id,
                )
        elif node_kind == "chapter":
            if change.new not in _VALID_CHAPTER_LEVELS:
                raise PatchError(
                    f"set_field chapter level {change.new!r} is not valid "
                    f"(allowed: {sorted(_VALID_CHAPTER_LEVELS)})",
                    patch_id,
                )

    if change.field == "paired" and node_kind == "footnote":
        if not isinstance(change.new, bool):
            raise PatchError(
                f"set_field footnote.paired must be bool, got {type(change.new).__name__!r}",
                patch_id,
            )
        # Mutual-exclusion check: if setting paired=True, orphan must be False
        if change.new is True:
            orphan_val = getattr(node, "orphan")
            if orphan_val is True:
                raise PatchError(
                    f"footnote {change.target_uid} cannot be both paired=True and orphan=True",
                    patch_id,
                )

    if change.field == "orphan" and node_kind == "footnote":
        if not isinstance(change.new, bool):
            raise PatchError(
                f"set_field footnote.orphan must be bool, got {type(change.new).__name__!r}",
                patch_id,
            )
        # Mutual-exclusion check: if setting orphan=True, paired must be False
        if change.new is True:
            paired_val = getattr(node, "paired")
            if paired_val is True:
                raise PatchError(
                    f"footnote {change.target_uid} cannot be both paired=True and orphan=True",
                    patch_id,
                )


def _check_replace_node_preconditions(
    working: Book,
    change: ReplaceNodeChange,
    index: _BookIndex,
    patch_id: str,
) -> None:
    """Check preconditions for a ReplaceNodeChange."""
    if change.target_uid not in index.block_index:
        if change.target_uid in index.chapter_index:
            raise PatchError(
                f"replace_node target_uid {change.target_uid!r} is a chapter; "
                "replace_node is only valid for blocks",
                patch_id,
            )
        raise PatchError(
            f"replace_node target_uid {change.target_uid!r} not found",
            patch_id,
        )

    current_node = _get_node(working, change.target_uid, index)
    current_serialized = current_node.model_dump(mode="python")  # type: ignore[union-attr]
    if current_serialized != change.old_node:
        raise PatchError(
            f"replace_node old_node precondition mismatch for {change.target_uid!r}",
            patch_id,
        )

    # Validate new_node kind and payload
    new_kind = change.new_node.get("kind")
    if new_kind not in BLOCK_KINDS:
        raise PatchError(
            f"replace_node new_node kind {new_kind!r} is not a valid block kind",
            patch_id,
        )
    assert isinstance(new_kind, str)
    payload_cls = BLOCK_PAYLOAD_MODELS[new_kind]
    payload_data = {
        k: v for k, v in change.new_node.items() if k not in ("uid", "kind")
    }
    try:
        payload_cls.model_validate(payload_data)
    except Exception as exc:
        raise PatchError(
            f"replace_node new_node payload validation failed for kind {new_kind!r}: {exc}",
            patch_id,
        ) from exc


def _check_insert_node_preconditions(
    working: Book,
    change: InsertNodeChange,
    index: _BookIndex,
    patch_id: str,
) -> None:
    """Check preconditions for an InsertNodeChange."""
    node_uid = change.node.get("uid")
    node_kind = change.node.get("kind")

    # UID collision check against current working state
    if node_uid in index.block_index or node_uid in index.chapter_index:
        raise PatchError(
            f"insert_node uid {node_uid!r} already exists in book",
            patch_id,
        )

    if change.parent_uid is not None:
        # Insert block: parent_uid must be a known chapter
        if change.parent_uid not in index.chapter_index:
            raise PatchError(
                f"insert_node parent_uid {change.parent_uid!r} not found as a chapter",
                patch_id,
            )
        # Validate node as block
        if node_kind not in BLOCK_KINDS:
            raise PatchError(
                f"insert_node node kind {node_kind!r} is not a valid block kind",
                patch_id,
            )
        assert isinstance(node_kind, str)
        payload_cls = BLOCK_PAYLOAD_MODELS[node_kind]
        payload_data = {
            k: v for k, v in change.node.items() if k not in ("uid", "kind")
        }
        try:
            payload_cls.model_validate(payload_data)
        except Exception as exc:
            raise PatchError(
                f"insert_node node payload validation failed for kind {node_kind!r}: {exc}",
                patch_id,
            ) from exc

        # Validate after_uid exists in the target chapter's blocks
        if change.after_uid is not None:
            ch_idx = index.chapter_index[change.parent_uid]
            chapter = working.chapters[ch_idx]
            found = any(b.uid == change.after_uid for b in chapter.blocks)
            if not found:
                raise PatchError(
                    f"insert_node after_uid {change.after_uid!r} not found in "
                    f"chapter {change.parent_uid!r}",
                    patch_id,
                )
    else:
        # Insert chapter: validate as Chapter
        try:
            Chapter.model_validate(change.node)
        except Exception as exc:
            raise PatchError(
                f"insert_node chapter node validation failed: {exc}",
                patch_id,
            ) from exc

        # after_uid must be a known chapter
        if change.after_uid is not None:
            if change.after_uid not in index.chapter_index:
                raise PatchError(
                    f"insert_node after_uid {change.after_uid!r} not found in book.chapters",
                    patch_id,
                )


def _check_delete_node_preconditions(
    working: Book,
    change: DeleteNodeChange,
    index: _BookIndex,
    patch_id: str,
) -> None:
    """Check preconditions for a DeleteNodeChange."""
    if (
        change.target_uid not in index.block_index
        and change.target_uid not in index.chapter_index
    ):
        raise PatchError(
            f"delete_node target_uid {change.target_uid!r} not found",
            patch_id,
        )

    current_node = _get_node(working, change.target_uid, index)
    current_serialized = current_node.model_dump(mode="python")  # type: ignore[union-attr]
    if current_serialized != change.old_node:
        raise PatchError(
            f"delete_node old_node precondition mismatch for {change.target_uid!r}",
            patch_id,
        )

    # If deleting a chapter, it must be empty
    if change.target_uid in index.chapter_index:
        ch_idx = index.chapter_index[change.target_uid]
        if working.chapters[ch_idx].blocks:
            raise PatchError(
                f"cannot delete non-empty chapter {change.target_uid!r} "
                "(delete all blocks first)",
                patch_id,
            )


def _check_move_node_preconditions(
    working: Book,
    change: MoveNodeChange,
    index: _BookIndex,
    patch_id: str,
) -> None:
    """Check preconditions for a MoveNodeChange."""
    if (
        change.target_uid not in index.block_index
        and change.target_uid not in index.chapter_index
    ):
        raise PatchError(
            f"move_node target_uid {change.target_uid!r} not found",
            patch_id,
        )

    if change.from_parent_uid is None and change.to_parent_uid is None:
        # Chapter-level move
        if change.target_uid not in index.chapter_index:
            raise PatchError(
                f"move_node with from_parent_uid=None and to_parent_uid=None requires "
                f"target_uid {change.target_uid!r} to be a chapter",
                patch_id,
            )
        if change.after_uid is not None and change.after_uid not in index.chapter_index:
            raise PatchError(
                f"move_node after_uid {change.after_uid!r} not found in book.chapters",
                patch_id,
            )
    else:
        # Block move
        if change.target_uid not in index.block_index:
            raise PatchError(
                f"move_node target_uid {change.target_uid!r} not found as a block",
                patch_id,
            )

        if change.from_parent_uid is None:
            raise PatchError(
                "move_node from_parent_uid is required for block moves",
                patch_id,
            )

        if change.to_parent_uid is None:
            raise PatchError(
                "move_node to_parent_uid is required for block moves",
                patch_id,
            )

        # Verify from_parent_uid matches actual current parent
        actual_ch_idx, _ = index.block_index[change.target_uid]
        actual_ch_uid = working.chapters[actual_ch_idx].uid
        if actual_ch_uid != change.from_parent_uid:
            raise PatchError(
                f"move_node from_parent_uid {change.from_parent_uid!r} does not match "
                f"actual parent {actual_ch_uid!r} for block {change.target_uid!r}",
                patch_id,
            )

        # Verify to_parent_uid exists
        if change.to_parent_uid not in index.chapter_index:
            raise PatchError(
                f"move_node to_parent_uid {change.to_parent_uid!r} not found",
                patch_id,
            )
        # after_uid must be in the target chapter (if specified)
        # Note: after removal from source the target chapter may be the same;
        # we check after_uid against the current blocks (before removal).
        if change.after_uid is not None:
            to_ch_idx = index.chapter_index[change.to_parent_uid]
            to_chapter = working.chapters[to_ch_idx]
            # after_uid may be the block being moved itself only if it's in the
            # target chapter — but that's already blocked by MoveNodeChange validator.
            found = any(b.uid == change.after_uid for b in to_chapter.blocks)
            if not found:
                raise PatchError(
                    f"move_node after_uid {change.after_uid!r} not found in "
                    f"target chapter {change.to_parent_uid!r}",
                    patch_id,
                )


# ---------------------------------------------------------------------------
# Apply helpers
# ---------------------------------------------------------------------------


def _apply_set_field(
    working: Book,
    change: SetFieldChange,
    index: _BookIndex,
    *,
    patch_id: str,
) -> None:
    """Apply a SetFieldChange to the working copy."""
    if change.target_uid in index.block_index:
        ch_idx, b_idx = index.block_index[change.target_uid]
        block = working.chapters[ch_idx].blocks[b_idx]
        working.chapters[ch_idx].blocks[b_idx] = _validated_set_field_node(  # type: ignore[index]
            block,
            block.kind,
            change.field,
            change.new,
            patch_id,
        )
    elif change.target_uid in index.chapter_index:
        ch_idx = index.chapter_index[change.target_uid]
        chapter = working.chapters[ch_idx]
        updated_chapter = _validated_set_field_node(
            chapter,
            "chapter",
            change.field,
            change.new,
            patch_id,
        )
        if not isinstance(updated_chapter, Chapter):
            raise PatchError(
                f"set_field validation returned non-chapter for {change.target_uid!r}",
                patch_id,
            )
        working.chapters[ch_idx] = updated_chapter
    else:
        raise PatchError(
            f"set_field target_uid {change.target_uid!r} not found at apply time",
            patch_id,
        )


def _apply_replace_node(
    working: Book,
    change: ReplaceNodeChange,
    index: _BookIndex,
    *,
    patch_id: str,
) -> None:
    """Apply a ReplaceNodeChange to the working copy."""
    if change.target_uid not in index.block_index:
        raise PatchError(
            f"replace_node target_uid {change.target_uid!r} not found at apply time",
            patch_id,
        )
    node_data = dict(change.new_node)
    kind = node_data["kind"]
    payload = {k: v for k, v in node_data.items() if k not in ("uid", "kind")}
    new_block = _make_block(kind, change.target_uid, payload)
    ch_idx, b_idx = index.block_index[change.target_uid]
    working.chapters[ch_idx].blocks[b_idx] = new_block  # type: ignore[index]


def _apply_insert_node(
    working: Book,
    change: InsertNodeChange,
    index: _BookIndex,
    *,
    patch_id: str,
) -> None:
    """Apply an InsertNodeChange to the working copy."""
    if change.parent_uid is not None:
        ch_idx = index.chapter_index[change.parent_uid]
        chapter = working.chapters[ch_idx]
        node_data = dict(change.node)
        new_block = _make_block(
            node_data["kind"],
            node_data["uid"],
            {k: v for k, v in node_data.items() if k not in ("uid", "kind")},
        )
        if change.after_uid is None:
            insert_at = 0
        else:
            insert_at = _block_pos_in_chapter(chapter, change.after_uid, patch_id) + 1
        chapter.blocks = (
            chapter.blocks[:insert_at] + [new_block] + chapter.blocks[insert_at:]
        )
    else:
        # Insert chapter into book.chapters
        node_data = dict(change.node)
        new_chapter = Chapter.model_validate(node_data)
        if change.after_uid is None:
            insert_at = 0
        else:
            if change.after_uid not in index.chapter_index:
                raise PatchError(
                    f"insert_node after_uid {change.after_uid!r} not found in book.chapters",
                    patch_id,
                )
            insert_at = index.chapter_index[change.after_uid] + 1
        working.chapters = (
            working.chapters[:insert_at] + [new_chapter] + working.chapters[insert_at:]
        )


def _apply_delete_node(
    working: Book,
    change: DeleteNodeChange,
    index: _BookIndex,
    *,
    patch_id: str,
) -> None:
    """Apply a DeleteNodeChange to the working copy."""
    if change.target_uid in index.block_index:
        ch_idx, b_idx = index.block_index[change.target_uid]
        chapter = working.chapters[ch_idx]
        chapter.blocks = [b for i, b in enumerate(chapter.blocks) if i != b_idx]
    elif change.target_uid in index.chapter_index:
        ch_idx = index.chapter_index[change.target_uid]
        working.chapters = [c for i, c in enumerate(working.chapters) if i != ch_idx]
    else:
        raise PatchError(
            f"delete_node target_uid {change.target_uid!r} not found at apply time",
            patch_id,
        )


def _apply_move_node(
    working: Book,
    change: MoveNodeChange,
    index: _BookIndex,
    *,
    patch_id: str,
) -> None:
    """Apply a MoveNodeChange to the working copy."""
    if change.from_parent_uid is None and change.to_parent_uid is None:
        # Chapter-level move within book.chapters
        ch_idx = index.chapter_index[change.target_uid]
        chapter = working.chapters[ch_idx]
        working.chapters = [c for i, c in enumerate(working.chapters) if i != ch_idx]
        if change.after_uid is None:
            insert_at = 0
        else:
            # Recompute position after removal
            new_positions = {c.uid: i for i, c in enumerate(working.chapters)}
            if change.after_uid not in new_positions:
                raise PatchError(
                    f"move_node after_uid {change.after_uid!r} not found in book.chapters",
                    patch_id,
                )
            insert_at = new_positions[change.after_uid] + 1
        working.chapters = (
            working.chapters[:insert_at] + [chapter] + working.chapters[insert_at:]
        )
    else:
        # Block move
        if change.target_uid not in index.block_index:
            raise PatchError(
                f"move_node target_uid {change.target_uid!r} not found as a block",
                patch_id,
            )
        ch_idx, b_idx = index.block_index[change.target_uid]
        block = working.chapters[ch_idx].blocks[b_idx]

        # Remove from source chapter
        working.chapters[ch_idx].blocks = [
            b for i, b in enumerate(working.chapters[ch_idx].blocks) if i != b_idx
        ]

        # Determine target chapter
        if change.to_parent_uid is None:
            raise PatchError(
                "move_node to_parent_uid=None is only valid when from_parent_uid=None "
                "(chapter-level move)",
                patch_id,
            )
        target_ch_idx = index.chapter_index[change.to_parent_uid]
        target_chapter = working.chapters[target_ch_idx]

        if change.after_uid is None:
            insert_at = 0
        else:
            insert_at = (
                _block_pos_in_chapter(target_chapter, change.after_uid, patch_id) + 1
            )

        target_chapter.blocks = (
            target_chapter.blocks[:insert_at]
            + [block]
            + target_chapter.blocks[insert_at:]
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_book_patch(book: Book, patch: BookPatch) -> None:
    """Lightweight static pre-check — no deep copy, no precondition evaluation.

    Checks: uid=None guard, scope consistency, schema-level constraints
    (uid uniqueness within patch, field name legality, etc.).
    Does NOT verify old/new preconditions (those require transactional apply).
    Raises PatchError if any static check fails.
    """
    pid = patch.patch_id

    # Step 1: uid=None guard
    _require_all_uids_non_none(book, pid)

    # Step 2: build read-only index for static checks
    block_index: dict[str, tuple[int, int]] = {}
    chapter_index: dict[str, int] = {}
    for ch_idx, chapter in enumerate(book.chapters):
        if chapter.uid is not None:
            if chapter.uid in chapter_index or chapter.uid in block_index:
                raise PatchError(f"duplicate uid {chapter.uid!r} in book", pid)
            chapter_index[chapter.uid] = ch_idx
        for b_idx, block in enumerate(chapter.blocks):
            if block.uid is not None:
                if block.uid in block_index or block.uid in chapter_index:
                    raise PatchError(f"duplicate uid {block.uid!r} in book", pid)
                block_index[block.uid] = (ch_idx, b_idx)

    # Step 3: per-change static checks (no precondition evaluation)
    insert_uids_seen: set[str] = set()
    # Track chapter UIDs inserted within this patch so that subsequent move_node
    # changes targeting those chapters can be validated correctly.
    inserted_chapter_uids: set[str] = set()

    for change in patch.changes:
        if isinstance(change, SetFieldChange):
            _validate_static_set_field(
                change, block_index, chapter_index, pid, book=book
            )

        elif isinstance(change, ReplaceNodeChange):
            _validate_static_replace_node(change, block_index, chapter_index, pid)

        elif isinstance(change, InsertNodeChange):
            _validate_static_insert_node(
                change,
                block_index,
                chapter_index,
                insert_uids_seen,
                pid,
                inserted_chapter_uids=inserted_chapter_uids,
            )
            node_uid = change.node.get("uid")
            if node_uid is not None:
                insert_uids_seen.add(node_uid)
                # Track if this insert adds a chapter (parent_uid=None means chapter insert)
                if change.parent_uid is None:
                    inserted_chapter_uids.add(node_uid)

        elif isinstance(change, DeleteNodeChange):
            if (
                change.target_uid not in block_index
                and change.target_uid not in chapter_index
                and change.target_uid not in inserted_chapter_uids
            ):
                raise PatchError(
                    f"delete_node target_uid {change.target_uid!r} not found",
                    pid,
                )

        elif isinstance(change, MoveNodeChange):
            _validate_static_move_node(
                change,
                block_index,
                chapter_index,
                pid,
                inserted_chapter_uids=inserted_chapter_uids,
            )

    # Step 5: PatchScope range check
    scope_uid = patch.scope.chapter_uid
    if scope_uid is not None:
        for change in patch.changes:
            _check_scope(change, scope_uid, block_index, chapter_index, pid)


def _validate_static_set_field(
    change: SetFieldChange,
    block_index: dict[str, tuple[int, int]],
    chapter_index: dict[str, int],
    pid: str,
    book: Book | None = None,
) -> None:
    """Static checks for SetFieldChange (no precondition evaluation)."""
    if change.target_uid not in block_index and change.target_uid not in chapter_index:
        raise PatchError(
            f"set_field target_uid {change.target_uid!r} not found",
            pid,
        )

    if change.field in _IMMUTABLE_FIELDS:
        raise PatchError(
            f"field {change.field!r} is immutable and cannot be changed via set_field",
            pid,
        )

    # Kind-specific field checks using the book when available
    if book is not None:
        if change.target_uid in block_index:
            ch_idx, b_idx = block_index[change.target_uid]
            block = book.chapters[ch_idx].blocks[b_idx]
            allowed = _ALLOWED_SET_FIELD.get(block.kind, frozenset())
            if change.field not in allowed:
                raise PatchError(
                    f"field {change.field!r} is not editable for kind {block.kind!r}",
                    pid,
                )
        elif change.target_uid in chapter_index:
            allowed = _ALLOWED_SET_FIELD.get("chapter", frozenset())
            if change.field not in allowed:
                raise PatchError(
                    f"field {change.field!r} is not editable for kind 'chapter'",
                    pid,
                )


def _validate_static_replace_node(
    change: ReplaceNodeChange,
    block_index: dict[str, tuple[int, int]],
    chapter_index: dict[str, int],
    pid: str,
) -> None:
    """Static checks for ReplaceNodeChange."""
    if change.target_uid not in block_index:
        if change.target_uid in chapter_index:
            raise PatchError(
                f"replace_node target_uid {change.target_uid!r} is a chapter; "
                "replace_node is only valid for blocks",
                pid,
            )
        raise PatchError(
            f"replace_node target_uid {change.target_uid!r} not found",
            pid,
        )
    new_kind = change.new_node.get("kind")
    if new_kind not in BLOCK_KINDS:
        raise PatchError(
            f"replace_node new_node kind {new_kind!r} is not a valid block kind",
            pid,
        )
    assert isinstance(new_kind, str)
    payload_cls = BLOCK_PAYLOAD_MODELS[new_kind]
    payload_data = {
        k: v for k, v in change.new_node.items() if k not in ("uid", "kind")
    }
    try:
        payload_cls.model_validate(payload_data)
    except Exception as exc:
        raise PatchError(
            f"replace_node new_node payload validation failed for kind {new_kind!r}: {exc}",
            pid,
        ) from exc


def _validate_static_insert_node(
    change: InsertNodeChange,
    block_index: dict[str, tuple[int, int]],
    chapter_index: dict[str, int],
    insert_uids_seen: set[str],
    pid: str,
    *,
    inserted_chapter_uids: set[str] | None = None,
) -> None:
    """Static checks for InsertNodeChange.

    inserted_chapter_uids: UIDs of chapters inserted earlier in the same patch,
    so that inserting a block into a newly-inserted chapter is not wrongly rejected.
    """
    node_uid = change.node.get("uid")
    node_kind = change.node.get("kind")

    # UID collision against existing book
    if node_uid in block_index or node_uid in chapter_index:
        raise PatchError(
            f"insert_node uid {node_uid!r} already exists in book",
            pid,
        )
    # UID collision within same patch
    if node_uid in insert_uids_seen:
        raise PatchError(
            f"insert_node uid {node_uid!r} is duplicated within the same patch",
            pid,
        )

    all_chapter_uids = set(chapter_index.keys()) | (inserted_chapter_uids or set())

    if change.parent_uid is not None:
        if change.parent_uid not in all_chapter_uids:
            raise PatchError(
                f"insert_node parent_uid {change.parent_uid!r} not found as a chapter",
                pid,
            )
        if node_kind not in BLOCK_KINDS:
            raise PatchError(
                f"insert_node node kind {node_kind!r} is not a valid block kind",
                pid,
            )
        assert isinstance(node_kind, str)
        payload_cls = BLOCK_PAYLOAD_MODELS[node_kind]
        payload_data = {
            k: v for k, v in change.node.items() if k not in ("uid", "kind")
        }
        try:
            payload_cls.model_validate(payload_data)
        except Exception as exc:
            raise PatchError(
                f"insert_node node payload validation failed for kind {node_kind!r}: {exc}",
                pid,
            ) from exc
    else:
        # Insert chapter
        try:
            Chapter.model_validate(change.node)
        except Exception as exc:
            raise PatchError(
                f"insert_node chapter node validation failed: {exc}",
                pid,
            ) from exc


def _validate_static_move_node(
    change: MoveNodeChange,
    block_index: dict[str, tuple[int, int]],
    chapter_index: dict[str, int],
    pid: str,
    *,
    inserted_chapter_uids: set[str] | None = None,
) -> None:
    """Static checks for MoveNodeChange.

    inserted_chapter_uids: UIDs of chapters inserted earlier in the same patch,
    so that move_node targeting those new chapters is not wrongly rejected.
    """
    all_chapter_uids = set(chapter_index.keys()) | (inserted_chapter_uids or set())

    if change.target_uid not in block_index and change.target_uid not in chapter_index:
        raise PatchError(
            f"move_node target_uid {change.target_uid!r} not found",
            pid,
        )

    if change.from_parent_uid is None and change.to_parent_uid is None:
        # Chapter-level move: target must be a chapter
        if change.target_uid not in chapter_index:
            raise PatchError(
                f"move_node with from_parent_uid=None and to_parent_uid=None requires "
                f"target_uid {change.target_uid!r} to be a chapter",
                pid,
            )
    else:
        if (
            change.to_parent_uid is not None
            and change.to_parent_uid not in all_chapter_uids
        ):
            raise PatchError(
                f"move_node to_parent_uid {change.to_parent_uid!r} not found",
                pid,
            )
        if change.from_parent_uid is None:
            raise PatchError(
                "move_node from_parent_uid is required for block moves",
                pid,
            )
        if change.to_parent_uid is None:
            raise PatchError(
                "move_node to_parent_uid is required for block moves",
                pid,
            )


def _check_scope(
    change: IRChange,  # type: ignore[valid-type]
    scope_uid: str,
    block_index: dict[str, tuple[int, int]],
    chapter_index: dict[str, int],
    pid: str,
) -> None:
    """Verify a change does not violate the patch scope restriction."""

    def _block_belongs_to_chapter(uid: str) -> bool:
        if uid not in block_index:
            return False
        ch_idx, _ = block_index[uid]
        # Reverse-lookup chapter uid from chapter_index
        for ch_uid, idx in chapter_index.items():
            if idx == ch_idx:
                return ch_uid == scope_uid
        return False

    if isinstance(change, SetFieldChange | ReplaceNodeChange | DeleteNodeChange):
        # target_uid must belong to the scoped chapter
        target = change.target_uid
        if target in block_index:
            if not _block_belongs_to_chapter(target):
                raise PatchError(
                    f"change target_uid {target!r} is out of scope "
                    f"(scope chapter_uid={scope_uid!r})",
                    pid,
                )
        elif target in chapter_index:
            if target != scope_uid:
                raise PatchError(
                    f"change target_uid {target!r} is out of scope "
                    f"(scope chapter_uid={scope_uid!r})",
                    pid,
                )

    elif isinstance(change, InsertNodeChange):
        if change.parent_uid != scope_uid:
            raise PatchError(
                f"insert_node parent_uid {change.parent_uid!r} is out of scope "
                f"(scope chapter_uid={scope_uid!r})",
                pid,
            )

    elif isinstance(change, MoveNodeChange):
        if change.from_parent_uid != scope_uid or change.to_parent_uid != scope_uid:
            raise PatchError(
                f"move_node crosses scope boundary "
                f"(from_parent_uid={change.from_parent_uid!r}, "
                f"to_parent_uid={change.to_parent_uid!r}, "
                f"scope chapter_uid={scope_uid!r})",
                pid,
            )


def apply_book_patch(book: Book, patch: BookPatch) -> Book:
    """Validate and apply a BookPatch to a Book in a single transactional operation.

    Performs all precondition checks incrementally against the evolving working copy.
    Returns a new Book on success.
    Raises PatchError on any validation or apply failure.
    The input book is never modified.
    """
    pid = patch.patch_id

    # Static pre-check (lightweight, no deep copy)
    validate_book_patch(book, patch)

    # Uid=None guard on original book (validate_book_patch already did this,
    # but keep explicit for clarity — it's cheap)
    _require_all_uids_non_none(book, pid)

    # Single deep copy — all mutations happen on this working copy
    working = book.model_copy(deep=True)

    # Build initial index
    index = _build_index(working)

    # Apply each change in order with incremental precondition checks
    for change in patch.changes:
        if isinstance(change, SetFieldChange):
            _check_set_field_preconditions(working, change, index, pid)
            _apply_set_field(working, change, index, patch_id=pid)
            # set_field does not change structure; no index rebuild needed

        elif isinstance(change, ReplaceNodeChange):
            _check_replace_node_preconditions(working, change, index, pid)
            _apply_replace_node(working, change, index, patch_id=pid)
            # uid and position unchanged; no index rebuild needed

        elif isinstance(change, InsertNodeChange):
            _check_insert_node_preconditions(working, change, index, pid)
            _apply_insert_node(working, change, index, patch_id=pid)
            index = _build_index(working)

        elif isinstance(change, DeleteNodeChange):
            _check_delete_node_preconditions(working, change, index, pid)
            _apply_delete_node(working, change, index, patch_id=pid)
            index = _build_index(working)

        elif isinstance(change, MoveNodeChange):
            _check_move_node_preconditions(working, change, index, pid)
            _apply_move_node(working, change, index, patch_id=pid)
            index = _build_index(working)

    # Final invariant check
    _check_footnote_invariants(working, pid)

    return working


__all__ = [
    "BookPatch",
    "BLOCK_KINDS",
    "BLOCK_PAYLOAD_MODELS",
    "DeleteNodeChange",
    "IRChange",
    "InsertNodeChange",
    "MoveNodeChange",
    "PatchError",
    "PatchScope",
    "ReplaceNodeChange",
    "SetFieldChange",
    "apply_book_patch",
    "validate_book_patch",
]
