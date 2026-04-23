from __future__ import annotations

import pytest
from pydantic import ValidationError

from epubforge.editor.memory import (
    ChapterStatus,
    ConventionNote,
    EditMemory,
    MemoryPatch,
    OpenQuestion,
    PatternNote,
    canonical_convention_key,
    canonical_pattern_key,
    merge_edit_memory,
)


def _memory() -> EditMemory:
    return EditMemory.create(
        book_id="book-1",
        updated_at="2026-04-23T08:00:00Z",
        updated_by="tester",
        chapter_uids=["ch-1", "ch-2"],
    )


def _convention(
    *,
    value: str = "—",
    confidence: float = 0.8,
    contributed_at: str = "2026-04-23T08:00:00Z",
    statement: str = "Use em dash for numeric ranges.",
) -> ConventionNote:
    return ConventionNote(
        canonical_key=canonical_convention_key("book", None, "dash_range_style"),
        scope="book",
        topic="dash_range_style",
        statement=statement,
        value=value,
        confidence=confidence,
        evidence_uids=["blk-1"],
        contributed_by="scanner-1",
        contributed_at=contributed_at,
    )


def _pattern(*, affected_uids: list[str]) -> PatternNote:
    return PatternNote(
        canonical_key=canonical_pattern_key("same_page_dup_callout", affected_uids),
        topic="same_page_dup_callout",
        description="Duplicate same-page callout cluster.",
        affected_uids=affected_uids,
        contributed_by="scanner-1",
    )


def test_canonical_key_validation_rejects_mismatch_and_normalizes_patterns() -> None:
    with pytest.raises(ValidationError):
        ConventionNote(
            canonical_key="book:-:wrong_topic",
            scope="book",
            topic="dash_range_style",
            statement="Use em dash.",
            value="—",
            confidence=0.8,
            evidence_uids=["blk-1"],
            contributed_by="scanner-1",
            contributed_at="2026-04-23T08:00:00Z",
        )

    first = canonical_pattern_key("same_page_dup_callout", ["blk-3", "blk-1", "blk-2", "blk-2"])
    second = canonical_pattern_key("same_page_dup_callout", ["blk-2", "blk-1", "blk-3"])
    assert first == second

    note = _pattern(affected_uids=["blk-3", "blk-1", "blk-2", "blk-2"])
    assert note.affected_uids == ["blk-1", "blk-2", "blk-3"]


def test_duplicate_restatement_does_not_count_as_fresh_memory_change() -> None:
    baseline = _memory()
    first_merge = merge_edit_memory(
        baseline,
        MemoryPatch(conventions=[_convention()]),
        updated_at="2026-04-23T08:00:00Z",
        updated_by="supervisor",
    )
    second_merge = merge_edit_memory(
        first_merge.memory,
        MemoryPatch(
            conventions=[
                _convention(
                    statement="Same convention restated differently.",
                    confidence=0.8,
                    contributed_at="2026-04-23T08:05:00Z",
                )
            ]
        ),
        updated_at="2026-04-23T08:05:00Z",
        updated_by="supervisor",
    )

    assert second_merge.fresh_change_count == 0
    assert second_merge.true_additions == 0
    assert second_merge.true_revisions == 0
    assert second_merge.decisions[0].outcome == "duplicate"
    assert second_merge.decisions[0].stored_change is True


def test_higher_confidence_wins_and_equal_confidence_uses_later_timestamp() -> None:
    baseline = _memory()
    first_merge = merge_edit_memory(
        baseline,
        MemoryPatch(conventions=[_convention(confidence=0.7, contributed_at="2026-04-23T08:00:00Z")]),
        updated_at="2026-04-23T08:00:00Z",
        updated_by="supervisor",
    )
    stronger = merge_edit_memory(
        first_merge.memory,
        MemoryPatch(conventions=[_convention(confidence=0.9, contributed_at="2026-04-23T08:01:00Z")]),
        updated_at="2026-04-23T08:01:00Z",
        updated_by="supervisor",
    )
    note = stronger.memory.conventions[canonical_convention_key("book", None, "dash_range_style")]
    assert note.confidence == 0.9
    assert stronger.decisions[0].outcome == "revised"
    assert stronger.true_revisions == 1

    later_duplicate = merge_edit_memory(
        stronger.memory,
        MemoryPatch(
            conventions=[
                _convention(
                    confidence=0.9,
                    contributed_at="2026-04-23T08:02:00Z",
                    statement="Restated with the same confidence.",
                )
            ]
        ),
        updated_at="2026-04-23T08:02:00Z",
        updated_by="supervisor",
    )
    latest = later_duplicate.memory.conventions[canonical_convention_key("book", None, "dash_range_style")]
    assert latest.contributed_at == "2026-04-23T08:02:00Z"
    assert later_duplicate.decisions[0].outcome == "duplicate"
    assert later_duplicate.fresh_change_count == 0


def test_close_conflict_opens_question_instead_of_merging() -> None:
    baseline = _memory()
    existing = merge_edit_memory(
        baseline,
        MemoryPatch(conventions=[_convention(value="—", confidence=0.82)]),
        updated_at="2026-04-23T08:00:00Z",
        updated_by="supervisor",
    )
    conflict = merge_edit_memory(
        existing.memory,
        MemoryPatch(
            conventions=[
                _convention(
                    value="-",
                    confidence=0.75,
                    contributed_at="2026-04-23T08:10:00Z",
                    statement="Use hyphen for ranges.",
                )
            ]
        ),
        updated_at="2026-04-23T08:10:00Z",
        updated_by="supervisor",
        question_id_factory=lambda: "2e5a5f32-bd10-4f55-a243-cd98f1c681f4",
    )

    assert len(conflict.memory.open_questions) == 1
    question = conflict.memory.open_questions[0]
    assert question.options == ["—", "-"]
    assert conflict.decisions[0].outcome == "open_question"
    assert conflict.decisions[0].stored_change is False
    retained = conflict.memory.conventions[canonical_convention_key("book", None, "dash_range_style")]
    assert retained.value == "—"


def test_pattern_merge_uses_stable_canonical_key_and_dedupes_rephrasing() -> None:
    baseline = _memory()
    first = merge_edit_memory(
        baseline,
        MemoryPatch(patterns=[_pattern(affected_uids=["blk-3", "blk-1", "blk-2"])]),
        updated_at="2026-04-23T08:00:00Z",
        updated_by="supervisor",
    )
    second = merge_edit_memory(
        first.memory,
        MemoryPatch(
            patterns=[
                PatternNote(
                    canonical_key=canonical_pattern_key("same_page_dup_callout", ["blk-2", "blk-1", "blk-3"]),
                    topic="same_page_dup_callout",
                    description="Same cluster, different wording.",
                    affected_uids=["blk-2", "blk-1", "blk-3"],
                    contributed_by="scanner-2",
                )
            ]
        ),
        updated_at="2026-04-23T08:05:00Z",
        updated_by="supervisor",
    )

    assert second.fresh_change_count == 0
    assert second.decisions[0].outcome == "duplicate"
