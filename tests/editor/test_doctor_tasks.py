"""Tests for DoctorTask generation (Phase 10A)."""

from __future__ import annotations

import pytest
from uuid import uuid4

from epubforge.editor.doctor import (
    DoctorDelta,
    DoctorReport,
    DoctorTask,
    Hint,
    ReadinessChecklist,
    generate_doctor_tasks,
)
from epubforge.ir.semantic import AuditNote


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_readiness(**overrides):
    defaults = {
        "chapters_scanned": [],
        "chapters_unscanned": [],
        "open_questions": 0,
        "audit_issues": 0,
        "converged": False,
    }
    defaults.update(overrides)
    return ReadinessChecklist(**defaults)


def _make_delta(**overrides):
    defaults = {
        "new_issue_keys": [],
        "resolved_issue_keys": [],
        "new_hint_keys": [],
        "resolved_hint_keys": [],
        "fresh_convention_keys": [],
        "fresh_pattern_keys": [],
        "new_open_question_ids": [],
        "resolved_open_question_ids": [],
        "new_applied_op_count": 0,
        "quiet_round": True,
        "quiet_round_streak": 0,
    }
    defaults.update(overrides)
    return DoctorDelta(**defaults)


def _make_report(*, issues=None, hints=None, **overrides):
    defaults = {
        "issues": issues or [],
        "hints": hints or [],
        "readiness": _make_readiness(),
        "suggested_next_actions": [],
        "delta": _make_delta(),
    }
    defaults.update(overrides)
    return DoctorReport(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDoctorTaskModel:
    """DoctorTask model validation."""

    def test_valid_task(self):
        task = DoctorTask(
            task_id="a" * 8 + "-" + "b" * 4 + "-4" + "c" * 3 + "-8" + "d" * 3 + "-" + "e" * 12,
            kind="scan",
            priority=2,
            recommended_agent="scanner",
            message="Chapter needs scanning",
        )
        assert task.kind == "scan"

    def test_invalid_kind_rejected(self):
        with pytest.raises(Exception):
            DoctorTask.model_validate({
                "task_id": str(uuid4()),
                "kind": "invalid",
                "priority": 2,
                "recommended_agent": "scanner",
                "message": "test",
            })


class TestGenerateDoctorTasks:
    """Test generate_doctor_tasks() mapping and priority."""

    def test_empty_report_produces_no_tasks(self):
        report = _make_report()
        tasks = generate_doctor_tasks(report)
        assert tasks == []

    def test_orphan_footnote_issue_maps_to_fixer_p0(self):
        issue = AuditNote(page=5, block_index=3, kind="orphan_footnote", hint="Orphan footnote on page 5")
        report = _make_report(issues=[issue])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        t = tasks[0]
        assert t.kind == "fix"
        assert t.recommended_agent == "fixer"
        assert t.priority == 0
        assert t.source_issue_key is not None

    def test_unknown_callout_issue_maps_to_fixer_p0(self):
        issue = AuditNote(page=10, kind="unknown_callout", hint="Unknown callout")
        report = _make_report(issues=[issue])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].priority == 0
        assert tasks[0].recommended_agent == "fixer"

    def test_punctuation_anomaly_maps_to_fixer_p1(self):
        issue = AuditNote(page=7, kind="punctuation_anomaly", hint="Bad punctuation")
        report = _make_report(issues=[issue])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].priority == 1
        assert tasks[0].recommended_agent == "fixer"

    def test_suspect_attribution_maps_to_reviewer_p1(self):
        issue = AuditNote(page=3, kind="suspect_attribution", hint="Suspect attribution")
        report = _make_report(issues=[issue])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].kind == "review"
        assert tasks[0].recommended_agent == "reviewer"
        assert tasks[0].priority == 1

    def test_other_issue_maps_to_reviewer_p1(self):
        issue = AuditNote(page=1, kind="other", hint="Other issue")
        report = _make_report(issues=[issue])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].kind == "review"
        assert tasks[0].recommended_agent == "reviewer"

    def test_needs_scan_hint_maps_to_scanner_p2(self):
        hint = Hint(kind="needs_scan", severity="warn", message="Chapter not scanned", scope="chapter", chapter_uid="ch-001")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        t = tasks[0]
        assert t.kind == "scan"
        assert t.recommended_agent == "scanner"
        assert t.priority == 2
        assert t.chapter_uid == "ch-001"

    def test_style_inconsistency_hint_maps_to_fixer_p2(self):
        hint = Hint(kind="style_inconsistency", severity="warn", message="Dash style mismatch", scope="book")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].kind == "fix"
        assert tasks[0].recommended_agent == "fixer"
        assert tasks[0].priority == 2

    def test_unusual_density_hint_maps_to_scanner_p2(self):
        hint = Hint(kind="unusual_density", severity="warn", message="High footnote density", scope="chapter", chapter_uid="ch-002")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].kind == "scan"
        assert tasks[0].recommended_agent == "scanner"

    def test_open_question_hint_maps_to_reviewer_p2(self):
        hint = Hint(kind="open_question", severity="warn", message="Unresolved question", scope="chapter", chapter_uid="ch-003")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].kind == "review"
        assert tasks[0].recommended_agent == "reviewer"

    def test_candidate_review_hint_maps_to_scanner_p3(self):
        hint = Hint(kind="candidate_review", severity="info", message="Candidate role block", scope="block", chapter_uid="ch-001", block_uid="blk-001")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        t = tasks[0]
        assert t.kind == "scan"
        assert t.recommended_agent == "scanner"
        assert t.priority == 3
        assert t.block_uid == "blk-001"

    def test_table_merge_pending_hint_maps_to_fixer_p3(self):
        hint = Hint(kind="table_merge_pending", severity="info", message="Table merge pending", scope="block", chapter_uid="ch-002", block_uid="blk-005")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 1
        assert tasks[0].kind == "fix"
        assert tasks[0].recommended_agent == "fixer"
        assert tasks[0].priority == 3

    def test_convergence_hint_skipped(self):
        hint = Hint(kind="convergence", severity="info", message="System has converged", scope="book")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert tasks == []

    def test_tasks_sorted_by_priority_then_chapter(self):
        """Tasks should be sorted: lowest priority number first, then by chapter UID."""
        hints = [
            Hint(kind="candidate_review", severity="info", message="Review candidate", scope="chapter", chapter_uid="ch-002"),
            Hint(kind="needs_scan", severity="warn", message="Needs scan", scope="chapter", chapter_uid="ch-001"),
        ]
        issues = [
            AuditNote(page=5, kind="orphan_footnote", hint="Orphan footnote"),
        ]
        report = _make_report(issues=issues, hints=hints)
        tasks = generate_doctor_tasks(report)
        assert len(tasks) == 3
        # P0 issue first, then P2 hint, then P3 hint
        assert tasks[0].priority == 0
        assert tasks[1].priority == 2
        assert tasks[2].priority == 3

    def test_mixed_issues_and_hints(self):
        """Full scenario with multiple issues and hints."""
        issues = [
            AuditNote(page=1, kind="orphan_footnote", hint="Orphan fn"),
            AuditNote(page=2, kind="punctuation_anomaly", hint="Bad punct"),
        ]
        hints = [
            Hint(kind="needs_scan", severity="warn", message="Scan needed", scope="chapter", chapter_uid="ch-001"),
            Hint(kind="convergence", severity="info", message="Converged", scope="book"),
        ]
        report = _make_report(issues=issues, hints=hints)
        tasks = generate_doctor_tasks(report)
        # convergence skipped, so 3 tasks total
        assert len(tasks) == 3
        # All have unique task_ids
        task_ids = [t.task_id for t in tasks]
        assert len(set(task_ids)) == 3

    def test_task_message_from_issue_hint_field(self):
        """Task message should come from AuditNote.hint for issues."""
        issue = AuditNote(page=5, kind="orphan_footnote", hint="Orphan footnote on page 5")
        report = _make_report(issues=[issue])
        tasks = generate_doctor_tasks(report)
        assert tasks[0].message == "Orphan footnote on page 5"

    def test_task_message_from_hint_message_field(self):
        """Task message should come from Hint.message for hints."""
        hint = Hint(kind="needs_scan", severity="warn", message="Chapter ch-001 needs scanning", scope="chapter", chapter_uid="ch-001")
        report = _make_report(hints=[hint])
        tasks = generate_doctor_tasks(report)
        assert tasks[0].message == "Chapter ch-001 needs scanning"
