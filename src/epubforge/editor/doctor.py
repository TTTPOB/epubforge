"""Doctor core report models, readiness evaluation, and convergence helpers."""

from __future__ import annotations

from collections import Counter
from statistics import mean, pstdev
from typing import Iterable, Literal

from pydantic import Field, field_validator, model_validator

from epubforge.audit import DASH_CHAR_LABELS, AuditBundle, DashInventoryChapter, run_all_detectors
from epubforge.editor._validators import StrictModel, require_non_empty
from epubforge.editor.memory import EditMemory, OpenQuestion
from epubforge.ir.semantic import AuditNote, Book, Paragraph, Table
from epubforge.query import iter_blocks


def canonical_issue_key(issue: AuditNote) -> str:
    block_index = issue.block_index if issue.block_index is not None else -1
    return f"{issue.page}:{block_index}:{issue.kind}:{issue.hint}"


def canonical_hint_key(hint: Hint) -> str:
    return ":".join(
        (
            hint.kind,
            hint.scope,
            hint.chapter_uid or "-",
            hint.block_uid or "-",
            hint.severity,
            hint.message,
        )
    )


def unresolved_questions(memory: EditMemory) -> list[OpenQuestion]:
    return [question for question in memory.open_questions if not question.resolved]


def chapters_missing_scan(memory: EditMemory, chapter_uids: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for chapter_uid in sorted(set(chapter_uids)):
        status = memory.chapter_status.get(chapter_uid)
        if status is None or status.read_passes < 1:
            missing.append(chapter_uid)
    return missing


def _resolve_chapter_uids(*, book: Book | None, chapter_uids: list[str] | None) -> list[str]:
    if chapter_uids is not None:
        return chapter_uids
    if book is None:
        raise ValueError("chapter_uids or book must be provided")
    resolved: list[str] = []
    for chapter_idx, chapter in enumerate(book.chapters):
        if chapter.uid is not None and chapter.uid.strip():
            resolved.append(chapter.uid)
        else:
            resolved.append(f"chapter-{chapter_idx}")
    return resolved


class Hint(StrictModel):
    kind: Literal[
        "needs_scan",
        "style_inconsistency",
        "unusual_density",
        "open_question",
        "convergence",
        "candidate_review",
        "table_merge_pending",
    ]
    severity: Literal["info", "warn"] = "info"
    message: str
    scope: Literal["book", "chapter", "block"]
    chapter_uid: str | None = None
    block_uid: str | None = None
    suggested_subagent_type: str | None = None

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        return require_non_empty(value, field_name="message")

    @field_validator("chapter_uid", "block_uid", "suggested_subagent_type")
    @classmethod
    def _validate_optional_strings(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _validate_scope(self) -> Hint:
        if self.scope == "book" and (self.chapter_uid is not None or self.block_uid is not None):
            raise ValueError("book-scoped hints must not set chapter_uid or block_uid")
        if self.scope == "chapter" and self.chapter_uid is None:
            raise ValueError("chapter-scoped hints must set chapter_uid")
        if self.scope == "block" and (self.chapter_uid is None or self.block_uid is None):
            raise ValueError("block-scoped hints must set chapter_uid and block_uid")
        return self


class ReadinessChecklist(StrictModel):
    chapters_scanned: list[str] = Field(default_factory=list)
    chapters_unscanned: list[str] = Field(default_factory=list)
    open_questions: int = 0
    audit_issues: int = 0
    converged: bool = False


class DoctorDelta(StrictModel):
    new_issue_keys: list[str] = Field(default_factory=list)
    resolved_issue_keys: list[str] = Field(default_factory=list)
    new_hint_keys: list[str] = Field(default_factory=list)
    resolved_hint_keys: list[str] = Field(default_factory=list)
    fresh_convention_keys: list[str] = Field(default_factory=list)
    fresh_pattern_keys: list[str] = Field(default_factory=list)
    new_open_question_ids: list[str] = Field(default_factory=list)
    resolved_open_question_ids: list[str] = Field(default_factory=list)
    new_applied_op_count: int = 0
    quiet_round: bool = False
    quiet_round_streak: int = 0


class DoctorReport(StrictModel):
    issues: list[AuditNote] = Field(default_factory=list)
    hints: list[Hint] = Field(default_factory=list)
    readiness: ReadinessChecklist
    suggested_next_actions: list[str] = Field(default_factory=list)
    delta: DoctorDelta | None = None


def _book_dominant_dash(inventory: list[DashInventoryChapter]) -> tuple[str | None, Counter[str]]:
    totals: Counter[str] = Counter()
    for entry in inventory:
        totals.update(entry.counts)
    if not totals:
        return None, totals
    dominant = min(totals.items(), key=lambda item: (-item[1], ord(item[0])))[0]
    return dominant, totals


def _style_inconsistency_hints(bundle: AuditBundle) -> list[Hint]:
    inventory = [entry for entry in bundle.dash_inventory if entry.total >= 3 and entry.dominant_char is not None]
    book_dominant, totals = _book_dominant_dash(inventory)
    if book_dominant is None or len([count for count in totals.values() if count > 0]) < 2:
        return []
    hints: list[Hint] = []
    for entry in inventory:
        assert entry.dominant_char is not None
        if entry.dominant_char == book_dominant:
            continue
        if entry.dominant_count * 10 < entry.total * 6:
            continue
        hints.append(
            Hint(
                kind="style_inconsistency",
                severity="warn",
                message=(
                    f"{entry.chapter_uid} 的破折号样式偏离全书主样式：本章以 "
                    f"U+{ord(entry.dominant_char):04X} {DASH_CHAR_LABELS[entry.dominant_char]} 为主，"
                    f"全书以 U+{ord(book_dominant):04X} {DASH_CHAR_LABELS[book_dominant]} 为主。"
                ),
                scope="chapter",
                chapter_uid=entry.chapter_uid,
                suggested_subagent_type="scanner",
            )
        )
    return hints


def _unusual_density_hints(bundle: AuditBundle) -> list[Hint]:
    if len(bundle.footnote_density) < 2:
        return []
    counts = [entry.count for entry in bundle.footnote_density]
    sigma = pstdev(counts)
    if sigma <= 0:
        return []
    average = mean(counts)
    threshold = average + 3 * sigma
    hints: list[Hint] = []
    for entry in bundle.footnote_density:
        if entry.count <= threshold:
            continue
        if entry.chapter_uid is None:
            hints.append(
                Hint(
                    kind="unusual_density",
                    severity="warn",
                    message=f"第 {entry.page} 页脚注密度异常高（{entry.count}，均值 {average:.2f}，σ {sigma:.2f}）。",
                    scope="book",
                    suggested_subagent_type="scanner",
                )
            )
            continue
        hints.append(
            Hint(
                kind="unusual_density",
                severity="warn",
                message=f"第 {entry.page} 页脚注密度异常高（{entry.count}，均值 {average:.2f}，σ {sigma:.2f}）。",
                scope="chapter",
                chapter_uid=entry.chapter_uid,
                suggested_subagent_type="scanner",
            )
        )
    return hints


def _detector_hints(bundle: AuditBundle) -> list[Hint]:
    return [*_style_inconsistency_hints(bundle), *_unusual_density_hints(bundle)]


def _skip_vlm_hints(book: Book, complex_pages: list[int]) -> list[Hint]:
    """Emit info-level hints when the book was produced by the skip-VLM path.

    These are never errors — they guide scanner/fixer agents toward work that
    remains after mechanical evidence-draft extraction.
    """
    hints: list[Hint] = []

    # Map each block to its chapter uid so we can associate pages with chapters.
    page_to_chapter_uid: dict[int, str | None] = {}
    for ref in iter_blocks(book):
        page = ref.block.provenance.page
        page_to_chapter_uid.setdefault(page, ref.chapter.uid)

    # needs_scan for chapters that cover complex pages
    complex_page_set = set(complex_pages)
    seen_chapter_uids: set[str] = set()
    for page in sorted(complex_page_set):
        chapter_uid = page_to_chapter_uid.get(page)
        if chapter_uid is None or chapter_uid in seen_chapter_uids:
            continue
        seen_chapter_uids.add(chapter_uid)
        hints.append(
            Hint(
                kind="needs_scan",
                severity="info",
                message=(
                    f"{chapter_uid} 包含复杂页面（如第 {page} 页），"
                    "skip-VLM 未做 VLM 语义提取，建议开 scanner subagent 仔细核查。"
                ),
                scope="chapter",
                chapter_uid=chapter_uid,
                suggested_subagent_type="scanner",
            )
        )

    # candidate_review hints for docling_*_candidate paragraph blocks
    for ref in iter_blocks(book):
        block = ref.block
        if not isinstance(block, Paragraph):
            continue
        if not block.role.startswith("docling_") or not block.role.endswith("_candidate"):
            continue
        if ref.chapter.uid is None or block.uid is None:
            continue
        hints.append(
            Hint(
                kind="candidate_review",
                severity="info",
                message=(
                    f"block {block.uid!r} has candidate role {block.role!r} — "
                    "needs scanner/fixer review before semantic promotion"
                ),
                scope="block",
                chapter_uid=ref.chapter.uid,
                block_uid=block.uid,
                suggested_subagent_type="fixer",
            )
        )

    # table_merge_pending: VLM-sourced Table with continuation=True but multi_page=False
    for ref in iter_blocks(book):
        block = ref.block
        if not isinstance(block, Table):
            continue
        if not block.continuation or block.multi_page:
            continue
        if ref.chapter.uid is None or block.uid is None:
            continue
        hints.append(
            Hint(
                kind="table_merge_pending",
                severity="info",
                message=(
                    f"table {block.uid!r} has continuation=True but multi_page=False — "
                    "assembler did not find a predecessor; manual merge may be needed"
                ),
                scope="block",
                chapter_uid=ref.chapter.uid,
                block_uid=block.uid,
                suggested_subagent_type="fixer",
            )
        )

    return hints


def _convention_signature(memory: EditMemory) -> dict[str, tuple[str, float]]:
    return {key: (note.value, round(note.confidence, 6)) for key, note in memory.conventions.items()}


def _pattern_signature(memory: EditMemory) -> dict[str, tuple[tuple[str, ...], bool, str | None]]:
    return {
        key: (tuple(note.affected_uids), note.resolved, note.suggested_fix)
        for key, note in memory.patterns.items()
    }


def compute_doctor_delta(
    *,
    memory: EditMemory,
    hints: list[Hint],
    issues: list[AuditNote],
    previous_memory: EditMemory | None = None,
    previous_report: DoctorReport | None = None,
    new_applied_op_count: int = 0,
) -> DoctorDelta:
    current_issue_keys: set[str] = {canonical_issue_key(issue) for issue in issues}
    previous_issue_keys: set[str] = (
        {canonical_issue_key(issue) for issue in previous_report.issues}
        if previous_report is not None
        else set()
    )
    current_hint_keys: set[str] = {canonical_hint_key(hint) for hint in hints}
    previous_hint_keys: set[str] = (
        {canonical_hint_key(hint) for hint in previous_report.hints}
        if previous_report is not None
        else set()
    )

    current_conventions = _convention_signature(memory)
    previous_conventions = _convention_signature(previous_memory) if previous_memory is not None else {}
    fresh_convention_keys = sorted(
        key
        for key, signature in current_conventions.items()
        if key not in previous_conventions or previous_conventions[key] != signature
    )

    current_patterns = _pattern_signature(memory)
    previous_patterns = _pattern_signature(previous_memory) if previous_memory is not None else {}
    fresh_pattern_keys = sorted(
        key
        for key, signature in current_patterns.items()
        if key not in previous_patterns or previous_patterns[key] != signature
    )

    current_open_questions: set[str] = {question.q_id for question in memory.open_questions if not question.resolved}
    previous_open_questions: set[str] = (
        {question.q_id for question in previous_memory.open_questions if not question.resolved}
        if previous_memory is not None
        else set()
    )

    quiet_round = not fresh_convention_keys and not fresh_pattern_keys and new_applied_op_count == 0
    prior_streak = previous_report.delta.quiet_round_streak if previous_report and previous_report.delta else 0
    quiet_round_streak = prior_streak + 1 if quiet_round else 0

    return DoctorDelta(
        new_issue_keys=sorted(current_issue_keys - previous_issue_keys),
        resolved_issue_keys=sorted(previous_issue_keys - current_issue_keys),
        new_hint_keys=sorted(current_hint_keys - previous_hint_keys),
        resolved_hint_keys=sorted(previous_hint_keys - current_hint_keys),
        fresh_convention_keys=fresh_convention_keys,
        fresh_pattern_keys=fresh_pattern_keys,
        new_open_question_ids=sorted(current_open_questions - previous_open_questions),
        resolved_open_question_ids=sorted(previous_open_questions - current_open_questions),
        new_applied_op_count=new_applied_op_count,
        quiet_round=quiet_round,
        quiet_round_streak=quiet_round_streak,
    )


def evaluate_convergence(
    *,
    memory: EditMemory,
    chapter_uids: list[str],
    issues: list[AuditNote],
    delta: DoctorDelta,
    supervisor_ready_to_stop: bool = True,
) -> ReadinessChecklist:
    chapters_unscanned = chapters_missing_scan(memory, chapter_uids)
    chapters_scanned = [chapter_uid for chapter_uid in sorted(set(chapter_uids)) if chapter_uid not in chapters_unscanned]
    open_question_count = len(unresolved_questions(memory))
    converged = (
        not issues
        and not chapters_unscanned
        and open_question_count == 0
        and delta.quiet_round_streak >= 2
        and supervisor_ready_to_stop
    )
    return ReadinessChecklist(
        chapters_scanned=chapters_scanned,
        chapters_unscanned=chapters_unscanned,
        open_questions=open_question_count,
        audit_issues=len(issues),
        converged=converged,
    )


def _core_hints(memory: EditMemory, chapter_uids: list[str]) -> list[Hint]:
    hints: list[Hint] = []
    for chapter_uid in chapters_missing_scan(memory, chapter_uids):
        hints.append(
            Hint(
                kind="needs_scan",
                severity="warn",
                message=f"{chapter_uid} 尚未完整通读，建议开 scanner subagent。",
                scope="chapter",
                chapter_uid=chapter_uid,
                suggested_subagent_type="scanner",
            )
        )
    for question in unresolved_questions(memory):
        hints.append(
            Hint(
                kind="open_question",
                severity="warn",
                message=f"OpenQuestion {question.q_id} 需要决策：{question.question}",
                scope="book",
                suggested_subagent_type="reviewer",
            )
        )
    return hints


def _suggested_next_actions(
    *,
    memory: EditMemory,
    chapter_uids: list[str],
    issues: list[AuditNote],
    readiness: ReadinessChecklist,
    delta: DoctorDelta,
    supervisor_ready_to_stop: bool,
) -> list[str]:
    actions: list[str] = []
    if issues:
        actions.append(f"存在 {len(issues)} 条结构审计问题，建议开 fixer subagent 处理。")
    for chapter_uid in readiness.chapters_unscanned:
        actions.append(f"{chapter_uid} 尚未通读，建议开 scanner subagent。")
    for question in unresolved_questions(memory):
        actions.append(f"OpenQuestion {question.q_id} 需要 reviewer 仲裁：{question.question}")
    if readiness.converged:
        actions.append("doctor 判定已收敛；supervisor 可停止编辑循环。")
    elif not supervisor_ready_to_stop and not issues and readiness.open_questions == 0 and not readiness.chapters_unscanned:
        actions.append("结构与扫描条件已满足，但仍需 supervisor 做最后一轮约定审视。")
    elif not readiness.converged and not issues and readiness.open_questions == 0 and not readiness.chapters_unscanned:
        if delta.quiet_round_streak < 2:
            actions.append("继续下一轮 doctor；还未达到连续 2 轮静默收敛窗口。")
    return actions


def build_doctor_report(
    *,
    memory: EditMemory,
    chapter_uids: list[str] | None = None,
    book: Book | None = None,
    issues: list[AuditNote] | None = None,
    detector_hints: list[Hint] | None = None,
    previous_memory: EditMemory | None = None,
    previous_report: DoctorReport | None = None,
    new_applied_op_count: int = 0,
    supervisor_ready_to_stop: bool = True,
) -> DoctorReport:
    resolved_chapter_uids = _resolve_chapter_uids(book=book, chapter_uids=chapter_uids)
    auto_detector_hints: list[Hint] = []
    skip_vlm_hint_list: list[Hint] = []
    if book is not None:
        bundle = run_all_detectors(book)
        auto_detector_hints = _detector_hints(bundle)
        if issues is None:
            issues = bundle.to_audit_notes()
        # Emit skip-VLM guidance hints when extraction was done without VLM
        if book.extraction.stage3_mode == "skip_vlm":
            skip_vlm_hint_list = _skip_vlm_hints(book, book.extraction.complex_pages)
    if issues is None:
        raise ValueError("issues or book must be provided")
    hints = [*(detector_hints or []), *auto_detector_hints, *skip_vlm_hint_list, *_core_hints(memory, resolved_chapter_uids)]
    delta = compute_doctor_delta(
        memory=memory,
        hints=hints,
        issues=issues,
        previous_memory=previous_memory,
        previous_report=previous_report,
        new_applied_op_count=new_applied_op_count,
    )
    readiness = evaluate_convergence(
        memory=memory,
        chapter_uids=resolved_chapter_uids,
        issues=issues,
        delta=delta,
        supervisor_ready_to_stop=supervisor_ready_to_stop,
    )
    if readiness.converged:
        convergence_hint = Hint(
            kind="convergence",
            severity="info",
            message="连续两轮无新增 memory/op 且无未决问题，doctor 认为可以停止。",
            scope="book",
            suggested_subagent_type="supervisor",
        )
        hints.append(convergence_hint)
        delta = compute_doctor_delta(
            memory=memory,
            hints=hints,
            issues=issues,
            previous_memory=previous_memory,
            previous_report=previous_report,
            new_applied_op_count=new_applied_op_count,
        )
    return DoctorReport(
        issues=issues,
        hints=hints,
        readiness=readiness,
        suggested_next_actions=_suggested_next_actions(
            memory=memory,
            chapter_uids=resolved_chapter_uids,
            issues=issues,
            readiness=readiness,
            delta=delta,
            supervisor_ready_to_stop=supervisor_ready_to_stop,
        ),
        delta=delta,
    )


__all__ = [
    "DoctorDelta",
    "DoctorReport",
    "Hint",
    "ReadinessChecklist",
    "build_doctor_report",
    "canonical_hint_key",
    "canonical_issue_key",
    "chapters_missing_scan",
    "compute_doctor_delta",
    "evaluate_convergence",
    "unresolved_questions",
]
