"""Book snapshot diff generation for UID-addressed editor patches.

Phase 6C supports same-UID / same-topology field diffs and block
``replace_node`` semantics. Topology planning remains deliberately fail-closed
until Phase 6D.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from epubforge.editor.patches import (
    BookPatch,
    PatchScope,
    ReplaceNodeChange,
    SetFieldChange,
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

_TOPOLOGY_NOT_IMPLEMENTED = (
    "topology diff generation is not implemented in Phase 6C yet"
)


def diff_books(base: Book, proposed: Book) -> BookPatch:
    """Return a UID-addressed BookPatch from ``base`` to ``proposed``.

    Phase 6C supports same-UID / same-topology chapter and block field diffs,
    plus block replacement for kind changes, table ``merge_record`` changes,
    and coupled footnote ``paired``/``orphan`` changes. Topology deltas still
    fail closed until Phase 6D.
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

    topology_deltas = _detect_topology_deltas(base_index, proposed_index)
    if topology_deltas:
        details = _format_delta_details(topology_deltas)
        raise DiffError(f"{_TOPOLOGY_NOT_IMPLEMENTED}: {details}")

    replace_changes, replaced_block_uids = _compare_block_replacements(
        base_index, proposed_index
    )
    unclassified_deltas = _detect_unclassified_same_topology_deltas(
        base_index,
        proposed_index,
        replaced_block_uids=replaced_block_uids,
    )
    if unclassified_deltas:
        details = _format_delta_details(unclassified_deltas)
        raise DiffError(f"unsupported or unclassified node delta(s): {details}")

    field_changes = _compare_same_topology_fields(
        base_index,
        proposed_index,
        replaced_block_uids=replaced_block_uids,
    )

    changes = [*replace_changes, *field_changes]
    if changes:
        return BookPatch(
            patch_id=str(uuid4()),
            agent_id="diff-engine",
            scope=PatchScope(chapter_uid=None),
            changes=changes,
            rationale="Semantic diff generated from base and proposed Book snapshots.",
            evidence_refs=[],
        )

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
        if _should_replace_block(base_block, proposed_block):
            # Replacement snapshots carry immutable fields as part of the full
            # node payload. Do not misclassify associated full-node differences
            # as unsupported immutable deltas.
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


def _format_delta_details(deltas: list[str]) -> str:
    details = "; ".join(deltas[:8])
    if len(deltas) > 8:
        details += f"; ... and {len(deltas) - 8} more"
    return details


def _detect_topology_deltas(
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
        base_loc = base_index.loc_by_uid[uid]
        proposed_loc = proposed_index.loc_by_uid[uid]
        if base_loc.chapter_index != proposed_loc.chapter_index:
            deltas.append(
                f"chapter topology/order delta uid={uid!r} "
                f"from index={base_loc.chapter_index!r} "
                f"to index={proposed_loc.chapter_index!r}"
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

    return deltas


def _compare_block_replacements(
    base_index: BookDiffIndex,
    proposed_index: BookDiffIndex,
) -> tuple[list[ReplaceNodeChange], set[str]]:
    changes: list[ReplaceNodeChange] = []
    replaced_block_uids: set[str] = set()

    proposed_block_uids = set(proposed_index.block_by_uid)
    for uid in _block_order(base_index):
        if uid not in proposed_block_uids:
            continue
        base_block = base_index.block_by_uid[uid]
        proposed_block = proposed_index.block_by_uid[uid]
        if not _should_replace_block(base_block, proposed_block):
            continue

        changes.append(
            ReplaceNodeChange(
                op="replace_node",
                target_uid=uid,
                old_node=base_block.model_dump(mode="python"),
                new_node=_replacement_new_node_payload(proposed_block),
            )
        )
        replaced_block_uids.add(uid)

    return changes, replaced_block_uids


def _replacement_new_node_payload(block: Block) -> dict[str, object]:
    payload = block.model_dump(mode="python")
    payload.pop("uid", None)
    return payload


def _should_replace_block(base_block: Block, proposed_block: Block) -> bool:
    if base_block.kind != proposed_block.kind:
        return True

    if base_block.kind == "table":
        old_merge = serialize_patch_field_value(getattr(base_block, "merge_record"))
        new_merge = serialize_patch_field_value(getattr(proposed_block, "merge_record"))
        if old_merge != new_merge:
            return True

    if base_block.kind == "footnote":
        paired_changed = serialize_patch_field_value(
            getattr(base_block, "paired")
        ) != serialize_patch_field_value(getattr(proposed_block, "paired"))
        orphan_changed = serialize_patch_field_value(
            getattr(base_block, "orphan")
        ) != serialize_patch_field_value(getattr(proposed_block, "orphan"))
        if paired_changed and orphan_changed:
            return True

    return False


def _compare_same_topology_fields(
    base_index: BookDiffIndex,
    proposed_index: BookDiffIndex,
    *,
    replaced_block_uids: set[str],
) -> list[SetFieldChange]:
    changes: list[SetFieldChange] = []

    base_chapter_uids = set(base_index.chapter_by_uid)
    proposed_chapter_uids = set(proposed_index.chapter_by_uid)
    for uid in _chapter_order(base_index):
        if uid not in base_chapter_uids or uid not in proposed_chapter_uids:
            continue
        changes.extend(
            _compare_node_fields(
                uid=uid,
                kind="chapter",
                base_node=base_index.chapter_by_uid[uid],
                proposed_node=proposed_index.chapter_by_uid[uid],
            )
        )

    base_block_uids = set(base_index.block_by_uid)
    proposed_block_uids = set(proposed_index.block_by_uid)
    for uid in _block_order(base_index):
        if (
            uid not in base_block_uids
            or uid not in proposed_block_uids
            or uid in replaced_block_uids
        ):
            continue
        base_block = base_index.block_by_uid[uid]
        proposed_block = proposed_index.block_by_uid[uid]
        if base_block.kind != proposed_block.kind:
            continue
        changes.extend(
            _compare_node_fields(
                uid=uid,
                kind=base_block.kind,
                base_node=base_block,
                proposed_node=proposed_block,
            )
        )

    return changes


def _compare_node_fields(
    *,
    uid: str,
    kind: str,
    base_node: Chapter | Block,
    proposed_node: Chapter | Block,
) -> list[SetFieldChange]:
    changes: list[SetFieldChange] = []
    for field in sorted(allowed_set_fields(kind)):
        old = serialize_patch_field_value(getattr(base_node, field))
        new = serialize_patch_field_value(getattr(proposed_node, field))
        if old != new:
            changes.append(
                SetFieldChange(
                    op="set_field",
                    target_uid=uid,
                    field=field,
                    old=old,
                    new=new,
                )
            )
    return changes


def _detect_unclassified_same_topology_deltas(
    base_index: BookDiffIndex,
    proposed_index: BookDiffIndex,
    *,
    replaced_block_uids: set[str],
) -> list[str]:
    deltas: list[str] = []

    for uid in sorted(set(base_index.chapter_by_uid) & set(proposed_index.chapter_by_uid)):
        _append_unknown_node_deltas(
            deltas,
            uid=uid,
            kind="chapter",
            base_node=base_index.chapter_by_uid[uid],
            proposed_node=proposed_index.chapter_by_uid[uid],
            known_fields={"uid", "kind", "blocks", *allowed_set_fields("chapter")},
        )

    for uid in sorted(set(base_index.block_by_uid) & set(proposed_index.block_by_uid)):
        if uid in replaced_block_uids:
            continue
        base_block = base_index.block_by_uid[uid]
        proposed_block = proposed_index.block_by_uid[uid]
        if base_block.kind != proposed_block.kind:
            continue
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


def _block_order(index: BookDiffIndex) -> tuple[str, ...]:
    return tuple(
        uid
        for uid, loc in sorted(
            index.loc_by_uid.items(),
            key=lambda item: (
                item[1].chapter_index,
                -1 if item[1].block_index is None else item[1].block_index,
            ),
        )
        if loc.block_index is not None
    )


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
