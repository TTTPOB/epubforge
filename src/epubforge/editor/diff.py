"""Book snapshot diff generation for UID-addressed editor patches.

Phase 6D supports apply-safe topology planning in addition to same-UID field
diffs and block ``replace_node`` semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from epubforge.editor.patches import (
    BookPatch,
    DeleteNodeChange,
    InsertNodeChange,
    MoveNodeChange,
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


@dataclass
class SimBookOrder:
    """Mutable topology simulation matching sequential BookPatch apply order."""

    chapters: list[str]
    blocks_by_chapter: dict[str, list[str]]
    parent_by_block: dict[str, str]


_BOOK_LEVEL_FIELDS: tuple[str, ...] = (
    "initialized_at",
    "uid_seed",
    "title",
    "authors",
    "language",
    "source_pdf",
    "extraction",
)

def diff_books(base: Book, proposed: Book) -> BookPatch:
    """Return a UID-addressed BookPatch from ``base`` to ``proposed``.

    Supports apply-safe topology deltas, same-UID chapter/block field diffs,
    plus block replacement for kind changes, table ``merge_record`` changes,
    and coupled footnote ``paired``/``orphan`` changes.
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
    topology_changes = _plan_topology_changes(
        base,
        proposed,
        base_index=base_index,
        proposed_index=proposed_index,
    )

    changes = [*topology_changes, *replace_changes, *field_changes]
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


def _plan_topology_changes(
    base: Book,
    proposed: Book,
    *,
    base_index: BookDiffIndex,
    proposed_index: BookDiffIndex,
) -> list[InsertNodeChange | MoveNodeChange | DeleteNodeChange]:
    """Plan topology changes while updating a lightweight apply simulation."""

    sim = _build_sim_order(base)
    changes: list[InsertNodeChange | MoveNodeChange | DeleteNodeChange] = []

    base_chapter_uids = set(base_index.chapter_by_uid)
    proposed_chapter_order = _chapter_order(proposed_index)

    # 1. Insert proposed-only chapters as empty containers. Blocks are placed
    # later through block-level operations to avoid full-node chapter inserts.
    previous_chapter_uid: str | None = None
    for chapter_uid in proposed_chapter_order:
        if chapter_uid not in base_chapter_uids:
            proposed_chapter = proposed_index.chapter_by_uid[chapter_uid]
            empty_chapter = proposed_chapter.model_copy(update={"blocks": []})
            changes.append(
                InsertNodeChange(
                    op="insert_node",
                    parent_uid=None,
                    after_uid=previous_chapter_uid,
                    node=empty_chapter.model_dump(mode="python"),
                )
            )
            _sim_insert_chapter(sim, chapter_uid, after_uid=previous_chapter_uid)
        previous_chapter_uid = chapter_uid

    # 2. Reorder all proposed chapters. Missing base-only chapters may still be
    # present as temporary source containers and will be deleted after blocks.
    previous_chapter_uid = None
    for chapter_uid in proposed_chapter_order:
        if not _sim_is_immediately_after(sim.chapters, chapter_uid, previous_chapter_uid):
            changes.append(
                MoveNodeChange(
                    op="move_node",
                    target_uid=chapter_uid,
                    from_parent_uid=None,
                    to_parent_uid=None,
                    after_uid=previous_chapter_uid,
                )
            )
            _sim_move_chapter(sim, chapter_uid, after_uid=previous_chapter_uid)
        previous_chapter_uid = chapter_uid

    # 3. Insert/move blocks into their proposed chapters in proposed order.
    base_block_uids = set(base_index.block_by_uid)
    for proposed_chapter in proposed.chapters:
        target_chapter_uid = _require_uid(
            proposed_chapter.uid,
            snapshot_name="proposed",
            node_kind="chapter",
            path="chapters[*]",
        )
        previous_block_uid: str | None = None
        for proposed_block in proposed_chapter.blocks:
            block_uid = _require_uid(
                proposed_block.uid,
                snapshot_name="proposed",
                node_kind=proposed_block.kind,
                path=f"chapter {target_chapter_uid!r}.blocks[*]",
            )
            if block_uid not in base_block_uids:
                changes.append(
                    InsertNodeChange(
                        op="insert_node",
                        parent_uid=target_chapter_uid,
                        after_uid=previous_block_uid,
                        node=proposed_block.model_dump(mode="python"),
                    )
                )
                _sim_insert_block(
                    sim,
                    block_uid,
                    parent_uid=target_chapter_uid,
                    after_uid=previous_block_uid,
                )
            else:
                current_parent_uid = sim.parent_by_block[block_uid]
                target_blocks = sim.blocks_by_chapter[target_chapter_uid]
                if (
                    current_parent_uid != target_chapter_uid
                    or not _sim_is_immediately_after(
                        target_blocks, block_uid, previous_block_uid
                    )
                ):
                    changes.append(
                        MoveNodeChange(
                            op="move_node",
                            target_uid=block_uid,
                            from_parent_uid=current_parent_uid,
                            to_parent_uid=target_chapter_uid,
                            after_uid=previous_block_uid,
                        )
                    )
                    _sim_move_block(
                        sim,
                        block_uid,
                        from_parent_uid=current_parent_uid,
                        to_parent_uid=target_chapter_uid,
                        after_uid=previous_block_uid,
                    )
            previous_block_uid = block_uid

    # 4. Delete base blocks absent from proposed, in base order for stability.
    proposed_block_uids = set(proposed_index.block_by_uid)
    for block_uid in _block_order(base_index):
        if block_uid in proposed_block_uids or block_uid not in sim.parent_by_block:
            continue
        changes.append(
            DeleteNodeChange(
                op="delete_node",
                target_uid=block_uid,
                old_node=base_index.block_by_uid[block_uid].model_dump(mode="python"),
            )
        )
        _sim_delete_block(sim, block_uid)

    # 5. Delete missing chapters after they have become empty.
    proposed_chapter_uids = set(proposed_index.chapter_by_uid)
    for chapter_uid in _chapter_order(base_index):
        if chapter_uid in proposed_chapter_uids:
            continue
        if sim.blocks_by_chapter.get(chapter_uid):
            raise DiffError(
                f"internal topology planning error: missing chapter {chapter_uid!r} "
                "still contains blocks after move/delete planning"
            )
        old_empty_chapter = base_index.chapter_by_uid[chapter_uid].model_copy(
            update={"blocks": []}
        )
        changes.append(
            DeleteNodeChange(
                op="delete_node",
                target_uid=chapter_uid,
                old_node=old_empty_chapter.model_dump(mode="python"),
            )
        )
        _sim_delete_chapter(sim, chapter_uid)

    return changes


def _build_sim_order(book: Book) -> SimBookOrder:
    chapters: list[str] = []
    blocks_by_chapter: dict[str, list[str]] = {}
    parent_by_block: dict[str, str] = {}
    for chapter in book.chapters:
        assert chapter.uid is not None
        chapter_uid = chapter.uid
        chapters.append(chapter_uid)
        block_uids: list[str] = []
        for block in chapter.blocks:
            assert block.uid is not None
            block_uids.append(block.uid)
            parent_by_block[block.uid] = chapter_uid
        blocks_by_chapter[chapter_uid] = block_uids
    return SimBookOrder(
        chapters=chapters,
        blocks_by_chapter=blocks_by_chapter,
        parent_by_block=parent_by_block,
    )


def _sim_is_immediately_after(
    items: list[str],
    uid: str,
    after_uid: str | None,
) -> bool:
    if uid not in items:
        return False
    if after_uid is None:
        return bool(items) and items[0] == uid
    try:
        after_index = items.index(after_uid)
    except ValueError:
        return False
    return after_index + 1 < len(items) and items[after_index + 1] == uid


def _sim_insert_chapter(
    sim: SimBookOrder,
    chapter_uid: str,
    *,
    after_uid: str | None,
) -> None:
    _sim_insert_uid_after(sim.chapters, chapter_uid, after_uid=after_uid)
    sim.blocks_by_chapter[chapter_uid] = []


def _sim_move_chapter(
    sim: SimBookOrder,
    chapter_uid: str,
    *,
    after_uid: str | None,
) -> None:
    _sim_move_uid_after(sim.chapters, chapter_uid, after_uid=after_uid)


def _sim_delete_chapter(sim: SimBookOrder, chapter_uid: str) -> None:
    sim.chapters.remove(chapter_uid)
    sim.blocks_by_chapter.pop(chapter_uid, None)


def _sim_insert_block(
    sim: SimBookOrder,
    block_uid: str,
    *,
    parent_uid: str,
    after_uid: str | None,
) -> None:
    _sim_insert_uid_after(sim.blocks_by_chapter[parent_uid], block_uid, after_uid=after_uid)
    sim.parent_by_block[block_uid] = parent_uid


def _sim_move_block(
    sim: SimBookOrder,
    block_uid: str,
    *,
    from_parent_uid: str,
    to_parent_uid: str,
    after_uid: str | None,
) -> None:
    sim.blocks_by_chapter[from_parent_uid].remove(block_uid)
    _sim_insert_uid_after(
        sim.blocks_by_chapter[to_parent_uid], block_uid, after_uid=after_uid
    )
    sim.parent_by_block[block_uid] = to_parent_uid


def _sim_delete_block(sim: SimBookOrder, block_uid: str) -> None:
    parent_uid = sim.parent_by_block.pop(block_uid)
    sim.blocks_by_chapter[parent_uid].remove(block_uid)


def _sim_insert_uid_after(
    items: list[str],
    uid: str,
    *,
    after_uid: str | None,
) -> None:
    if uid in items:
        raise DiffError(f"internal topology planning error: duplicate simulated uid {uid!r}")
    if after_uid is None:
        insert_at = 0
    else:
        try:
            insert_at = items.index(after_uid) + 1
        except ValueError as exc:
            raise DiffError(
                f"internal topology planning error: after_uid {after_uid!r} not found"
            ) from exc
    items.insert(insert_at, uid)


def _sim_move_uid_after(
    items: list[str],
    uid: str,
    *,
    after_uid: str | None,
) -> None:
    if uid not in items:
        raise DiffError(f"internal topology planning error: uid {uid!r} not found")
    items.remove(uid)
    if after_uid is None:
        insert_at = 0
    else:
        try:
            insert_at = items.index(after_uid) + 1
        except ValueError as exc:
            raise DiffError(
                f"internal topology planning error: after_uid {after_uid!r} not found"
            ) from exc
    items.insert(insert_at, uid)


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
