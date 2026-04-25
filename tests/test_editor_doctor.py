from __future__ import annotations

from epubforge.editor.doctor import (
    DoctorDelta,
    DoctorReport,
    build_doctor_report,
    evaluate_convergence,
)
from epubforge.editor.memory import (
    ConventionNote,
    EditMemory,
    OpenQuestion,
    canonical_convention_key,
)
from epubforge.ir.semantic import AuditNote


def _memory() -> EditMemory:
    return EditMemory.create(
        book_id="book-1",
        updated_at="2026-04-23T08:00:00Z",
        updated_by="tester",
        chapter_uids=["ch-1", "ch-2"],
    )


def _scanned_memory() -> EditMemory:
    """Return a memory where all chapters have read_passes >= 1."""
    memory = _memory()
    chapter_status = {
        uid: memory.chapter_status[uid].model_copy(update={"read_passes": 1})
        for uid in memory.chapter_status
    }
    return memory.model_copy(update={"chapter_status": chapter_status})


def _report(
    *, quiet_round_streak: int, issues: list[AuditNote] | None = None
) -> DoctorReport:
    return DoctorReport(
        issues=issues or [],
        readiness=evaluate_convergence(
            memory=_scanned_memory(),
            chapter_uids=["ch-1", "ch-2"],
            issues=issues or [],
            delta=DoctorDelta(quiet_round=True, quiet_round_streak=quiet_round_streak),
        ),
        hints=[],
        suggested_next_actions=[],
        delta=DoctorDelta(quiet_round=True, quiet_round_streak=quiet_round_streak),
    )


def test_doctor_readiness_hints_and_actions_cover_core_paths() -> None:
    memory = _memory().model_copy(
        update={
            "open_questions": [
                OpenQuestion(
                    q_id="9e367943-fa9f-4976-aae9-3ddb9d5775a4",
                    question="Dash style unresolved.",
                    asked_by="scanner-1",
                    context_uids=["blk-1"],
                )
            ]
        }
    )
    report = build_doctor_report(
        memory=memory,
        chapter_uids=["ch-1", "ch-2"],
        issues=[
            AuditNote(page=3, block_index=2, kind="other", hint="Unknown style class")
        ],
        previous_memory=memory,
        previous_report=_report(quiet_round_streak=0),
        new_applied_op_count=2,
    )

    hint_kinds = {hint.kind for hint in report.hints}
    assert "needs_scan" in hint_kinds
    assert "open_question" in hint_kinds
    assert report.readiness.chapters_unscanned == ["ch-1", "ch-2"]
    assert report.readiness.audit_issues == 1
    assert any("fixer" in action for action in report.suggested_next_actions)
    assert any("scanner" in action for action in report.suggested_next_actions)
    assert any("reviewer" in action for action in report.suggested_next_actions)
    assert report.delta is not None
    assert report.delta.new_applied_op_count == 2
    assert report.delta.quiet_round is False
    assert len(report.tasks) > 0  # tasks should be generated from issues/hints
    # verify tasks are sorted by priority
    for i in range(len(report.tasks) - 1):
        assert report.tasks[i].priority <= report.tasks[i + 1].priority


def test_convergence_hint_and_delta_use_fresh_memory_change_not_duplicates() -> None:
    current = _scanned_memory().model_copy(
        update={
            "conventions": {
                canonical_convention_key(
                    "book", None, "dash_range_style"
                ): ConventionNote(
                    canonical_key=canonical_convention_key(
                        "book", None, "dash_range_style"
                    ),
                    scope="book",
                    topic="dash_range_style",
                    statement="Use em dash.",
                    value="—",
                    confidence=0.8,
                    evidence_uids=["blk-1"],
                    contributed_by="scanner-1",
                    contributed_at="2026-04-23T08:00:00Z",
                )
            }
        }
    )
    previous_memory = current.model_copy(
        update={
            "conventions": {
                canonical_convention_key(
                    "book", None, "dash_range_style"
                ): ConventionNote(
                    canonical_key=canonical_convention_key(
                        "book", None, "dash_range_style"
                    ),
                    scope="book",
                    topic="dash_range_style",
                    statement="Use em dash, restated.",
                    value="—",
                    confidence=0.8,
                    evidence_uids=["blk-1"],
                    contributed_by="scanner-2",
                    contributed_at="2026-04-23T07:59:00Z",
                )
            }
        }
    )
    previous_report = _report(quiet_round_streak=1)

    report = build_doctor_report(
        memory=current,
        chapter_uids=["ch-1", "ch-2"],
        issues=[],
        previous_memory=previous_memory,
        previous_report=previous_report,
        new_applied_op_count=0,
    )

    assert report.delta is not None
    assert report.delta.fresh_convention_keys == []
    assert report.delta.quiet_round is True
    assert report.delta.quiet_round_streak == 2
    assert report.readiness.converged is True
    assert any(hint.kind == "convergence" for hint in report.hints)
    assert report.suggested_next_actions == [
        "doctor 判定已收敛；supervisor 可停止编辑循环。"
    ]


def test_convergence_requires_supervisor_stop_gate_even_when_other_conditions_pass() -> (
    None
):
    report = build_doctor_report(
        memory=_scanned_memory(),
        chapter_uids=["ch-1", "ch-2"],
        issues=[],
        previous_memory=_scanned_memory(),
        previous_report=_report(quiet_round_streak=1),
        new_applied_op_count=0,
        supervisor_ready_to_stop=False,
    )

    assert report.readiness.chapters_unscanned == []
    assert report.readiness.open_questions == 0
    assert report.readiness.audit_issues == 0
    assert report.readiness.converged is False
    assert report.suggested_next_actions == [
        "结构与扫描条件已满足，但仍需 supervisor 做最后一轮约定审视。"
    ]
