"""Book snapshot diff skeleton for UID-addressed editor patches.

Phase 6B deliberately implements only indexing, input validation, and
fail-closed detection. Field-level diff generation and topology planning are
implemented in later sub-phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from epubforge.editor.patches import (
    BookPatch,
    PatchScope,
    allowed_set_fields,
    serialize_patch_field_value,
)
from epubforge.ir.semantic import Block, Book, Chapter


class DiffError(RuntimeError):
    """Raised when a Book diff cannot be generated safely."""


@dataclass(frozen=True)
class NodeLoc:
    """Stable location of a chapter or block in a Book snapshot."""

    uid: str
    kind: str
    chapter_uid: str | None
    chapter_index: int
    block_index: int | None


@dataclass(frozen=True)
class BookDiffIndex:
    """UID index used by the diff engine."""

    chapter_by_uid: dict[str, Chapter]
    block_by_uid: dict[str, Block]
    loc_by_uid: dict[str, NodeLoc]


_BOOK_LEVEL_FIELDS: tuple[str, ...] = (
    "initialized_at",
    "uid_seed",
    "title",
    "authors",
    "language",
    "source_pdf",
    "extraction",
)

_PHASE_6B_NOT_IMPLEMENTED = (
    "diff generation for representable deltas is not implemented in Phase 6B yet"
)


def diff_books(base: Book, proposed: Book) -> BookPatch:
    """Return a UID-addressed BookPatch from ``base`` to ``proposed``.

    Phase 6B supports identity/no-op diffs and validates inputs. Any detected
    unsupported delta, or any otherwise representable chapter/block delta whose
    generation belongs to Phase 6C/6D, raises ``DiffError`` instead of returning
    a partial patch.
    """

    if not isinstance(base, Book):
        raise DiffError(f"base must be a Book instance, got {type(base).__name__}")
    if not isinstance(proposed, Book):
        raise DiffError(
            f"proposed must be a Book instance, got {type(proposed).__name__}"
        )

    base_index = _build_diff_index(base, snapshot_name="base")
    proposed_index = _build_diff_index(proposed, snapshot_name="proposed")

    _reject_book_level_deltas(base, proposed)
    _reject_unsupported_immutable_deltas(base_index, proposed_index)

    representable_deltas = _detect_representable_deltas(base_index, proposed_index)
    if representable_deltas:
        details = "; ".join(representable_deltas[:8])
        if len(representable_deltas) > 8:
            details += f"; ... and {len(representable_deltas) - 8} more"
        raise DiffError(f"{_PHASE_6B_NOT_IMPLEMENTED}: {details}")

    return _empty_patch(
        rationale="No semantic changes detected between base and proposed Book snapshots."
    )


def _empty_patch(*, rationale: str) -> BookPatch:
    return BookPatch(
        patch_id=str(uuid4()),
        agent_id="diff-engine",
        scope=PatchScope(chapter_uid=None),
        changes=[],
        rationale=rationale,
        evidence_refs=[],
    )


def _build_diff_index(book: Book, *, snapshot_name: str) -> BookDiffIndex:
    chapter_by_uid: dict[str, Chapter] = {}
    block_by_uid: dict[str, Block] = {}
    loc_by_uid: dict[str, NodeLoc] = {}

    for ch_idx, chapter in enumerate(book.chapters):
        chapter_uid = _require_uid(
            chapter.uid,
            snapshot_name=snapshot_name,
            node_kind="chapter",
            path=f"chapters[{ch_idx}]",
        )
        chapter_loc = NodeLoc(
            uid=chapter_uid,
            kind="chapter",
            chapter_uid=None,
            chapter_index=ch_idx,
            block_index=None,
        )
        _register_uid(
            loc_by_uid,
            chapter_uid,
            chapter_loc,
            snapshot_name=snapshot_name,
        )
        chapter_by_uid[chapter_uid] = chapter

        for block_idx, block in enumerate(chapter.blocks):
            block_uid = _require_uid(
                block.uid,
                snapshot_name=snapshot_name,
                node_kind=block.kind,
                path=f"chapters[{ch_idx}].blocks[{block_idx}]",
            )
            block_loc = NodeLoc(
                uid=block_uid,
                kind=block.kind,
                chapter_uid=chapter_uid,
                chapter_index=ch_idx,
                block_index=block_idx,
            )
            _register_uid(
                loc_by_uid,
                block_uid,
                block_loc,
                snapshot_name=snapshot_name,
            )
            block_by_uid[block_uid] = block

    return BookDiffIndex(
        chapter_by_uid=chapter_by_uid,
        block_by_uid=block_by_uid,
        loc_by_uid=loc_by_uid,
    )


def _require_uid(
    uid: str | None,
    *,
    snapshot_name: str,
    node_kind: str,
    path: str,
) -> str:
    if uid is None:
        raise DiffError(
            f"{snapshot_name} {node_kind} at {path} has uid=None; "
            "diff_books requires every chapter/block UID to be non-empty and stable"
        )
    if not isinstance(uid, str):
        raise DiffError(
            f"{snapshot_name} {node_kind} at {path} has non-string uid {uid!r}; "
            "diff_books requires string UIDs"
        )
    if not uid.strip():
        raise DiffError(
            f"{snapshot_name} {node_kind} at {path} has empty uid; "
            "diff_books requires every chapter/block UID to be non-empty"
        )
    return uid


def _register_uid(
    loc_by_uid: dict[str, NodeLoc],
    uid: str,
    loc: NodeLoc,
    *,
    snapshot_name: str,
) -> None:
    previous = loc_by_uid.get(uid)
    if previous is not None:
        raise DiffError(
            f"duplicate uid {uid!r} in {snapshot_name}: "
            f"{_format_loc(loc)} collides with {_format_loc(previous)}; "
            "chapter and block UIDs must be globally unique across the whole Book"
        )
    loc_by_uid[uid] = loc


def _format_loc(loc: NodeLoc) -> str:
    if loc.block_index is None:
        return f"chapter uid={loc.uid!r} at chapters[{loc.chapter_index}]"
    return (
        f"block uid={loc.uid!r} kind={loc.kind!r} at "
        f"chapters[{loc.chapter_index}].blocks[{loc.block_index}] "
        f"parent={loc.chapter_uid!r}"
    )


def _reject_book_level_deltas(base: Book, proposed: Book) -> None:
    changed_fields: list[str] = []
    for field in _BOOK_LEVEL_FIELDS:
        old = serialize_patch_field_value(getattr(base, field))
        new = serialize_patch_field_value(getattr(proposed, field))
        if old != new:
            changed_fields.append(field)

    if changed_fields:
        fields = ", ".join(changed_fields)
        raise DiffError(
            f"unsupported Book-level delta(s): {fields}; BookPatch has no Book-level "
            "target. Add an explicit metadata patch design before diffing these fields."
        )


def _reject_unsupported_immutable_deltas(
    base_index: BookDiffIndex,
    proposed_index: BookDiffIndex,
) -> None:
    for uid in sorted(
        set(base_index.chapter_by_uid).intersection(proposed_index.chapter_by_uid)
    ):
        base_chapter = base_index.chapter_by_uid[uid]
        proposed_chapter = proposed_index.chapter_by_uid[uid]
        if base_chapter.kind != proposed_chapter.kind:
            raise DiffError(
                f"unsupported immutable delta for chapter uid={uid!r}: kind changed "
                f"from {base_chapter.kind!r} to {proposed_chapter.kind!r}"
            )

    base_block_uids = set(base_index.block_by_uid)
    proposed_block_uids = set(proposed_index.block_by_uid)
    for uid in sorted(base_block_uids.intersection(proposed_block_uids)):
        base_block = base_index.block_by_uid[uid]
        proposed_block = proposed_index.block_by_uid[uid]
        if base_block.kind != proposed_block.kind:
            # Phase 6C will represent this as replace_node. Do not misclassify
            # associated full-node differences as unsupported immutable deltas.
            continue
        old_prov = serialize_patch_field_value(base_block.provenance)
        new_prov = serialize_patch_field_value(proposed_block.provenance)
        if old_prov != new_prov:
            raise DiffError(
                f"unsupported immutable delta for block uid={uid!r} kind={base_block.kind!r}: "
                "field 'provenance' changed; provenance is immutable in BookPatch. "
                "Use replace/insert semantics only after the diff engine supports them."
            )

    for uid in sorted(base_index.chapter_by_uid.keys() & proposed_block_uids):
        raise DiffError(
            f"unsupported UID category delta for uid={uid!r}: base chapter became "
            "proposed block; chapter/block UIDs share one namespace"
        )
    for uid in sorted(base_block_uids & proposed_index.chapter_by_uid.keys()):
        raise DiffError(
            f"unsupported UID category delta for uid={uid!r}: base block became "
            "proposed chapter; chapter/block UIDs share one namespace"
        )


def _detect_representable_deltas(
    base_index: BookDiffIndex,
    proposed_index: BookDiffIndex,
) -> list[str]:
    deltas: list[str] = []

    base_chapter_uids = set(base_index.chapter_by_uid)
    proposed_chapter_uids = set(proposed_index.chapter_by_uid)
    for uid in sorted(proposed_chapter_uids - base_chapter_uids):
        deltas.append(f"chapter insert uid={uid!r}")
    for uid in sorted(base_chapter_uids - proposed_chapter_uids):
        deltas.append(f"chapter delete uid={uid!r}")

    base_chapter_order = _chapter_order(base_index)
    proposed_chapter_order = _chapter_order(proposed_index)
    if base_chapter_order != proposed_chapter_order:
        deltas.append("chapter topology/order delta")

    for uid in sorted(base_chapter_uids & proposed_chapter_uids):
        _append_editable_field_deltas(
            deltas,
            uid=uid,
            kind="chapter",
            base_node=base_index.chapter_by_uid[uid],
            proposed_node=proposed_index.chapter_by_uid[uid],
        )
        _append_unknown_node_deltas(
            deltas,
            uid=uid,
            kind="chapter",
            base_node=base_index.chapter_by_uid[uid],
            proposed_node=proposed_index.chapter_by_uid[uid],
            known_fields={"uid", "kind", "blocks", *allowed_set_fields("chapter")},
        )

    base_block_uids = set(base_index.block_by_uid)
    proposed_block_uids = set(proposed_index.block_by_uid)
    for uid in sorted(proposed_block_uids - base_block_uids):
        deltas.append(f"block insert uid={uid!r}")
    for uid in sorted(base_block_uids - proposed_block_uids):
        deltas.append(f"block delete uid={uid!r}")

    for uid in sorted(base_block_uids & proposed_block_uids):
        base_loc = base_index.loc_by_uid[uid]
        proposed_loc = proposed_index.loc_by_uid[uid]
        if (
            base_loc.chapter_uid != proposed_loc.chapter_uid
            or base_loc.block_index != proposed_loc.block_index
        ):
            deltas.append(
                f"block topology/order delta uid={uid!r} "
                f"from parent={base_loc.chapter_uid!r} index={base_loc.block_index!r} "
                f"to parent={proposed_loc.chapter_uid!r} index={proposed_loc.block_index!r}"
            )

        base_block = base_index.block_by_uid[uid]
        proposed_block = proposed_index.block_by_uid[uid]
        if base_block.kind != proposed_block.kind:
            deltas.append(
                f"block replace/kind delta uid={uid!r} "
                f"from {base_block.kind!r} to {proposed_block.kind!r}"
            )
            continue

        if base_block.kind == "table":
            old_merge = serialize_patch_field_value(getattr(base_block, "merge_record"))
            new_merge = serialize_patch_field_value(getattr(proposed_block, "merge_record"))
            if old_merge != new_merge:
                deltas.append(f"table merge_record replace delta uid={uid!r}")

        _append_editable_field_deltas(
            deltas,
            uid=uid,
            kind=base_block.kind,
            base_node=base_block,
            proposed_node=proposed_block,
        )
        known_fields = {"uid", "kind", "provenance", *allowed_set_fields(base_block.kind)}
        if base_block.kind == "table":
            known_fields.add("merge_record")
        _append_unknown_node_deltas(
            deltas,
            uid=uid,
            kind=base_block.kind,
            base_node=base_block,
            proposed_node=proposed_block,
            known_fields=known_fields,
        )

    return deltas


def _chapter_order(index: BookDiffIndex) -> tuple[str, ...]:
    return tuple(
        uid
        for uid, loc in sorted(
            index.loc_by_uid.items(), key=lambda item: item[1].chapter_index
        )
        if loc.block_index is None
    )


def _append_editable_field_deltas(
    deltas: list[str],
    *,
    uid: str,
    kind: str,
    base_node: Chapter | Block,
    proposed_node: Chapter | Block,
) -> None:
    for field in sorted(allowed_set_fields(kind)):
        old = serialize_patch_field_value(getattr(base_node, field))
        new = serialize_patch_field_value(getattr(proposed_node, field))
        if old != new:
            deltas.append(f"field delta uid={uid!r} kind={kind!r} field={field!r}")


def _append_unknown_node_deltas(
    deltas: list[str],
    *,
    uid: str,
    kind: str,
    base_node: Chapter | Block,
    proposed_node: Chapter | Block,
    known_fields: set[str],
) -> None:
    base_dump = base_node.model_dump(mode="json")
    proposed_dump = proposed_node.model_dump(mode="json")
    for field in sorted(set(base_dump) | set(proposed_dump)):
        if field in known_fields:
            continue
        if base_dump.get(field) != proposed_dump.get(field):
            deltas.append(
                f"unclassified node delta uid={uid!r} kind={kind!r} field={field!r}"
            )
