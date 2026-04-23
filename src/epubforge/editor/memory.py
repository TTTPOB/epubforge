"""Shared editor memory models and canonical merge helpers."""

from __future__ import annotations

import hashlib
from typing import Callable, Literal, TypeAlias
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from epubforge.editor._validators import StrictModel, require_non_empty, validate_utc_iso_timestamp, validate_uuid4


CONVENTION_TOPICS: TypeAlias = Literal[
    "footnote_callout_set",
    "dash_range_style",
    "dash_breakdash_style",
    "epigraph_role",
    "poem_centering",
    "bibliography_ibid_style",
    "table_title_format",
    "cjk_compound_separator",
]

PATTERN_TOPICS: TypeAlias = Literal[
    "same_page_dup_callout",
    "cross_page_fn_body",
    "table_continuation_split_row",
    "vlm_concatenated_heading",
]


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted({value for value in values if value.strip()})


def canonical_convention_key(scope: Literal["book", "chapter"], chapter_uid: str | None, topic: CONVENTION_TOPICS) -> str:
    normalized_uid = chapter_uid if scope == "chapter" else None
    return f"{scope}:{normalized_uid or '-'}:{topic}"


def pattern_anchor_uids(affected_uids: list[str]) -> tuple[str, ...]:
    unique = tuple(_sorted_unique(affected_uids))
    if not unique:
        raise ValueError("affected_uids must not be empty")
    return unique[:3]


def canonical_pattern_key(topic: PATTERN_TOPICS, affected_uids: list[str]) -> str:
    anchors = pattern_anchor_uids(affected_uids)
    digest = hashlib.sha256("\x1f".join((topic, *anchors)).encode("utf-8")).hexdigest()[:12]
    return f"{topic}:{digest}"


class ConventionNote(StrictModel):
    canonical_key: str
    scope: Literal["book", "chapter"]
    chapter_uid: str | None = None
    topic: CONVENTION_TOPICS
    statement: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_uids: list[str] = Field(default_factory=list)
    contributed_by: str
    contributed_at: str
    supersedes: list[str] = Field(default_factory=list)

    @field_validator("chapter_uid")
    @classmethod
    def _validate_chapter_uid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="chapter_uid")

    @field_validator("statement", "value", "contributed_by")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @field_validator("contributed_at")
    @classmethod
    def _validate_contributed_at(cls, value: str) -> str:
        return validate_utc_iso_timestamp(value, field_name="contributed_at")

    @field_validator("evidence_uids", "supersedes")
    @classmethod
    def _normalize_uid_lists(cls, value: list[str], info) -> list[str]:
        if info.field_name == "evidence_uids" and not value:
            raise ValueError("evidence_uids must not be empty")
        normalized = _sorted_unique(value)
        if info.field_name == "evidence_uids" and not normalized:
            raise ValueError("evidence_uids must not be empty")
        return normalized

    @model_validator(mode="after")
    def _check_canonical_key(self) -> ConventionNote:
        if self.scope == "book" and self.chapter_uid is not None:
            raise ValueError("book-scoped conventions must not set chapter_uid")
        if self.scope == "chapter" and self.chapter_uid is None:
            raise ValueError("chapter-scoped conventions must set chapter_uid")
        expected = canonical_convention_key(self.scope, self.chapter_uid, self.topic)
        if self.canonical_key != expected:
            raise ValueError(f"canonical_key mismatch: expected {expected}")
        return self


class PatternNote(StrictModel):
    canonical_key: str
    topic: PATTERN_TOPICS
    description: str
    affected_uids: list[str]
    suggested_fix: str | None = None
    contributed_by: str
    resolved: bool = False

    @field_validator("description", "contributed_by")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @field_validator("suggested_fix")
    @classmethod
    def _validate_suggested_fix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="suggested_fix")

    @field_validator("affected_uids")
    @classmethod
    def _normalize_affected_uids(cls, value: list[str]) -> list[str]:
        normalized = _sorted_unique(value)
        if not normalized:
            raise ValueError("affected_uids must not be empty")
        return normalized

    @model_validator(mode="after")
    def _check_canonical_key(self) -> PatternNote:
        expected = canonical_pattern_key(self.topic, self.affected_uids)
        if self.canonical_key != expected:
            raise ValueError(f"canonical_key mismatch: expected {expected}")
        return self


class ChapterStatus(StrictModel):
    chapter_uid: str
    read_passes: int = Field(default=0, ge=0)
    last_reader: str | None = None
    issues_found: int = Field(default=0, ge=0)
    issues_fixed: int = Field(default=0, ge=0)
    notes: str = ""

    @field_validator("chapter_uid")
    @classmethod
    def _validate_chapter_uid(cls, value: str) -> str:
        return require_non_empty(value, field_name="chapter_uid")

    @field_validator("last_reader")
    @classmethod
    def _validate_last_reader(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="last_reader")


class OpenQuestion(StrictModel):
    q_id: str
    question: str
    context_uids: list[str] = Field(default_factory=list)
    asked_by: str
    options: list[str] = Field(default_factory=list)
    resolved: bool = False
    resolution: str | None = None

    @field_validator("q_id")
    @classmethod
    def _validate_q_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="q_id")

    @field_validator("question", "asked_by")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @field_validator("context_uids", "options")
    @classmethod
    def _normalize_lists(cls, value: list[str], info) -> list[str]:
        if info.field_name == "context_uids":
            return _sorted_unique(value)
        return [item.strip() for item in value if item.strip()]

    @field_validator("resolution")
    @classmethod
    def _validate_resolution(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="resolution")

    @model_validator(mode="after")
    def _check_resolution(self) -> OpenQuestion:
        if self.resolved and self.resolution is None:
            raise ValueError("resolved questions must include resolution")
        if not self.resolved and self.resolution is not None:
            raise ValueError("unresolved questions must not include resolution")
        return self


class EditMemory(StrictModel):
    book_id: str
    imported: bool = False
    imported_from: str | None = None
    imported_at: str | None = None
    assume_verified: bool = False
    conventions: dict[str, ConventionNote] = Field(default_factory=dict)
    patterns: dict[str, PatternNote] = Field(default_factory=dict)
    chapter_status: dict[str, ChapterStatus] = Field(default_factory=dict)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    updated_at: str
    updated_by: str

    @field_validator("book_id", "updated_by")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: str) -> str:
        return validate_utc_iso_timestamp(value, field_name="updated_at")

    @field_validator("imported_from")
    @classmethod
    def _validate_imported_from(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="imported_from")

    @field_validator("imported_at")
    @classmethod
    def _validate_imported_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_utc_iso_timestamp(value, field_name="imported_at")

    @model_validator(mode="after")
    def _check_import_fields(self) -> EditMemory:
        if self.imported and (self.imported_from is None or self.imported_at is None):
            raise ValueError("imported memory must include imported_from and imported_at")
        if not self.imported and (self.imported_from is not None or self.imported_at is not None):
            raise ValueError("non-imported memory must not set imported_from/imported_at")
        return self

    @classmethod
    def create(
        cls,
        *,
        book_id: str,
        updated_at: str,
        updated_by: str,
        chapter_uids: list[str] | None = None,
    ) -> EditMemory:
        statuses = {
            chapter_uid: ChapterStatus(chapter_uid=chapter_uid)
            for chapter_uid in sorted(set(chapter_uids or []))
        }
        return cls(book_id=book_id, updated_at=updated_at, updated_by=updated_by, chapter_status=statuses)

    def with_legacy_import(
        self,
        *,
        imported_from: str,
        imported_at: str,
        updated_by: str,
        chapter_uids: list[str],
        assume_verified: bool = False,
    ) -> EditMemory:
        chapter_status = dict(self.chapter_status)
        note = f"imported from {imported_from}"
        for chapter_uid in sorted(set(chapter_uids)):
            current = chapter_status.get(chapter_uid, ChapterStatus(chapter_uid=chapter_uid))
            if assume_verified:
                chapter_status[chapter_uid] = current.model_copy(
                    update={"read_passes": max(current.read_passes, 1), "last_reader": "legacy-import", "notes": note}
                )
            else:
                chapter_status[chapter_uid] = current
        return self.model_copy(
            update={
                "imported": True,
                "imported_from": imported_from,
                "imported_at": imported_at,
                "assume_verified": assume_verified,
                "chapter_status": chapter_status,
                "open_questions": [] if assume_verified else self.open_questions,
                "updated_at": imported_at,
                "updated_by": updated_by,
            }
        )


class MemoryPatch(StrictModel):
    conventions: list[ConventionNote] = Field(default_factory=list)
    patterns: list[PatternNote] = Field(default_factory=list)
    chapter_status: list[ChapterStatus] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


class MemoryHistoryEntry(StrictModel):
    item_type: Literal["convention", "pattern"]
    canonical_key: str
    reason: Literal["replaced", "duplicate", "conflict_open_question"]
    recorded_at: str
    recorded_by: str
    payload: dict[str, object]
    winner_key: str | None = None

    @field_validator("canonical_key", "recorded_by")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)

    @field_validator("recorded_at")
    @classmethod
    def _validate_recorded_at(cls, value: str) -> str:
        return validate_utc_iso_timestamp(value, field_name="recorded_at")


class MemoryMergeDecision(StrictModel):
    item_type: Literal["convention", "pattern", "chapter_status", "open_question"]
    canonical_key: str | None = None
    question_id: str | None = None
    outcome: Literal["added", "revised", "duplicate", "open_question"]
    fresh_change: bool = False
    stored_change: bool = False
    reason: str

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return require_non_empty(value, field_name="reason")


class MemoryMergeResult(StrictModel):
    memory: EditMemory
    decisions: list[MemoryMergeDecision] = Field(default_factory=list)
    history: list[MemoryHistoryEntry] = Field(default_factory=list)
    true_additions: int = 0
    true_revisions: int = 0
    duplicates: int = 0
    open_questions_added: int = 0
    fresh_change_count: int = 0
    stored_change_count: int = 0


def _history_entry(
    *,
    item_type: Literal["convention", "pattern"],
    note: ConventionNote | PatternNote,
    reason: Literal["replaced", "duplicate", "conflict_open_question"],
    recorded_at: str,
    recorded_by: str,
    winner_key: str | None,
) -> MemoryHistoryEntry:
    return MemoryHistoryEntry(
        item_type=item_type,
        canonical_key=note.canonical_key,
        reason=reason,
        recorded_at=recorded_at,
        recorded_by=recorded_by,
        payload=note.model_dump(mode="json"),
        winner_key=winner_key,
    )


def _convention_fresh_signature(note: ConventionNote) -> tuple[str, float]:
    return (note.value, round(note.confidence, 6))


def _pattern_fresh_signature(note: PatternNote) -> tuple[tuple[str, ...], bool, str | None]:
    return (tuple(note.affected_uids), note.resolved, note.suggested_fix)


def _question_signature(question: OpenQuestion) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (question.question, tuple(question.context_uids), tuple(question.options))


def _timestamps_order(existing: ConventionNote, incoming: ConventionNote) -> tuple[float, str]:
    return (incoming.confidence - existing.confidence, incoming.contributed_at)


def _merge_convention_note(
    existing: ConventionNote,
    incoming: ConventionNote,
    *,
    recorded_at: str,
    recorded_by: str,
    question_id_factory: Callable[[], str],
    open_questions: list[OpenQuestion],
) -> tuple[ConventionNote | None, MemoryMergeDecision, list[MemoryHistoryEntry], list[OpenQuestion]]:
    history: list[MemoryHistoryEntry] = []
    new_questions: list[OpenQuestion] = []
    if existing.value != incoming.value and abs(existing.confidence - incoming.confidence) < 0.15:
        options = [existing.value, incoming.value]
        conflict = OpenQuestion(
            q_id=question_id_factory(),
            question=(
                f"Convention conflict for {existing.topic} in {existing.scope} scope: "
                f"choose {existing.value!r} or {incoming.value!r}."
            ),
            context_uids=_sorted_unique(existing.evidence_uids + incoming.evidence_uids),
            asked_by=recorded_by,
            options=options,
        )
        if not any(not question.resolved and _question_signature(question) == _question_signature(conflict) for question in open_questions):
            new_questions.append(conflict)
        history.append(
            _history_entry(
                item_type="convention",
                note=incoming,
                reason="conflict_open_question",
                recorded_at=recorded_at,
                recorded_by=recorded_by,
                winner_key=existing.canonical_key,
            )
        )
        return (
            None,
            MemoryMergeDecision(
                item_type="convention",
                canonical_key=existing.canonical_key,
                outcome="open_question",
                fresh_change=bool(new_questions),
                stored_change=False,
                reason="value conflict below confidence threshold",
            ),
            history,
            new_questions,
        )

    choose_incoming = False
    if incoming.confidence > existing.confidence:
        choose_incoming = True
    elif incoming.confidence == existing.confidence and incoming.contributed_at > existing.contributed_at:
        choose_incoming = True

    winner = incoming if choose_incoming else existing
    loser = existing if choose_incoming else incoming
    merged = winner.model_copy(
        update={
            "evidence_uids": _sorted_unique(existing.evidence_uids + incoming.evidence_uids),
            "supersedes": _sorted_unique(existing.supersedes + incoming.supersedes),
        }
    )
    if choose_incoming and _convention_fresh_signature(existing) != _convention_fresh_signature(incoming):
        merged = merged.model_copy(update={"supersedes": _sorted_unique(merged.supersedes + [existing.canonical_key])})

    history_reason: Literal["replaced", "duplicate"] = "replaced" if choose_incoming else "duplicate"
    history.append(
        _history_entry(
            item_type="convention",
            note=loser,
            reason=history_reason,
            recorded_at=recorded_at,
            recorded_by=recorded_by,
            winner_key=merged.canonical_key,
        )
    )

    fresh_change = _convention_fresh_signature(existing) != _convention_fresh_signature(merged)
    stored_change = existing.model_dump(mode="json") != merged.model_dump(mode="json")
    outcome: Literal["revised", "duplicate"] = "revised" if fresh_change else "duplicate"
    if not choose_incoming and not stored_change:
        history = []
    return (
        merged,
        MemoryMergeDecision(
            item_type="convention",
            canonical_key=merged.canonical_key,
            outcome=outcome,
            fresh_change=fresh_change,
            stored_change=stored_change,
            reason="merged by canonical key",
        ),
        history,
        new_questions,
    )


def _merge_pattern_note(
    existing: PatternNote,
    incoming: PatternNote,
    *,
    recorded_at: str,
    recorded_by: str,
) -> tuple[PatternNote, MemoryMergeDecision, list[MemoryHistoryEntry]]:
    merged = existing.model_copy(
        update={
            "affected_uids": _sorted_unique(existing.affected_uids + incoming.affected_uids),
            "suggested_fix": existing.suggested_fix or incoming.suggested_fix,
            "resolved": existing.resolved or incoming.resolved,
            "contributed_by": incoming.contributed_by if incoming != existing else existing.contributed_by,
        }
    )
    fresh_change = _pattern_fresh_signature(existing) != _pattern_fresh_signature(merged)
    stored_change = existing.model_dump(mode="json") != merged.model_dump(mode="json")
    history = []
    if existing.model_dump(mode="json") != incoming.model_dump(mode="json"):
        history.append(
            _history_entry(
                item_type="pattern",
                note=incoming,
                reason="replaced" if fresh_change else "duplicate",
                recorded_at=recorded_at,
                recorded_by=recorded_by,
                winner_key=merged.canonical_key,
            )
        )
    return (
        merged,
        MemoryMergeDecision(
            item_type="pattern",
            canonical_key=merged.canonical_key,
            outcome="revised" if fresh_change else "duplicate",
            fresh_change=fresh_change,
            stored_change=stored_change,
            reason="merged by canonical key",
        ),
        history,
    )


def _merge_chapter_status(existing: ChapterStatus | None, incoming: ChapterStatus) -> tuple[ChapterStatus, MemoryMergeDecision]:
    if existing is None:
        return (
            incoming,
            MemoryMergeDecision(
                item_type="chapter_status",
                canonical_key=incoming.chapter_uid,
                outcome="added",
                fresh_change=True,
                stored_change=True,
                reason="new chapter status",
            ),
        )
    merged = existing.model_copy(
        update={
            "read_passes": max(existing.read_passes, incoming.read_passes),
            "last_reader": incoming.last_reader or existing.last_reader,
            "issues_found": max(existing.issues_found, incoming.issues_found),
            "issues_fixed": max(existing.issues_fixed, incoming.issues_fixed),
            "notes": incoming.notes or existing.notes,
        }
    )
    stored_change = existing.model_dump(mode="json") != merged.model_dump(mode="json")
    return (
        merged,
        MemoryMergeDecision(
            item_type="chapter_status",
            canonical_key=incoming.chapter_uid,
            outcome="revised" if stored_change else "duplicate",
            fresh_change=stored_change,
            stored_change=stored_change,
            reason="chapter status updated",
        ),
    )


def _merge_open_question(existing: OpenQuestion | None, incoming: OpenQuestion) -> tuple[OpenQuestion | None, MemoryMergeDecision]:
    if existing is None:
        return (
            incoming,
            MemoryMergeDecision(
                item_type="open_question",
                question_id=incoming.q_id,
                outcome="added",
                fresh_change=True,
                stored_change=True,
                reason="new open question",
            ),
        )
    stored_change = existing.model_dump(mode="json") != incoming.model_dump(mode="json")
    return (
        incoming if stored_change else existing,
        MemoryMergeDecision(
            item_type="open_question",
            question_id=incoming.q_id,
            outcome="revised" if stored_change else "duplicate",
            fresh_change=stored_change,
            stored_change=stored_change,
            reason="open question merged by q_id",
        ),
    )


def merge_edit_memory(
    memory: EditMemory,
    patch: MemoryPatch,
    *,
    updated_at: str,
    updated_by: str,
    question_id_factory: Callable[[], str] | None = None,
) -> MemoryMergeResult:
    validate_utc_iso_timestamp(updated_at, field_name="updated_at")
    updated_by = require_non_empty(updated_by, field_name="updated_by")
    next_memory = memory.model_copy(deep=True)
    decisions: list[MemoryMergeDecision] = []
    history: list[MemoryHistoryEntry] = []
    fresh_change_count = 0
    stored_change_count = 0
    true_additions = 0
    true_revisions = 0
    duplicates = 0
    open_questions_added = 0
    question_factory = question_id_factory or (lambda: str(uuid4()))

    for note in patch.conventions:
        existing = next_memory.conventions.get(note.canonical_key)
        if existing is None:
            next_memory.conventions[note.canonical_key] = note
            decision = MemoryMergeDecision(
                item_type="convention",
                canonical_key=note.canonical_key,
                outcome="added",
                fresh_change=True,
                stored_change=True,
                reason="new canonical convention",
            )
        else:
            merged, decision, note_history, new_questions = _merge_convention_note(
                existing,
                note,
                recorded_at=updated_at,
                recorded_by=updated_by,
                question_id_factory=question_factory,
                open_questions=next_memory.open_questions,
            )
            history.extend(note_history)
            for question in new_questions:
                next_memory.open_questions.append(question)
                open_questions_added += 1
            if merged is not None and decision.stored_change:
                next_memory.conventions[note.canonical_key] = merged
        decisions.append(decision)
        if decision.fresh_change:
            fresh_change_count += 1
            if decision.outcome == "added":
                true_additions += 1
            else:
                true_revisions += 1
        elif decision.outcome == "duplicate":
            duplicates += 1
        if decision.stored_change:
            stored_change_count += 1

    for note in patch.patterns:
        existing = next_memory.patterns.get(note.canonical_key)
        if existing is None:
            next_memory.patterns[note.canonical_key] = note
            decision = MemoryMergeDecision(
                item_type="pattern",
                canonical_key=note.canonical_key,
                outcome="added",
                fresh_change=True,
                stored_change=True,
                reason="new canonical pattern",
            )
        else:
            merged, decision, note_history = _merge_pattern_note(
                existing,
                note,
                recorded_at=updated_at,
                recorded_by=updated_by,
            )
            history.extend(note_history)
            if decision.stored_change:
                next_memory.patterns[note.canonical_key] = merged
        decisions.append(decision)
        if decision.fresh_change:
            fresh_change_count += 1
            if decision.outcome == "added":
                true_additions += 1
            else:
                true_revisions += 1
        elif decision.outcome == "duplicate":
            duplicates += 1
        if decision.stored_change:
            stored_change_count += 1

    for status in patch.chapter_status:
        merged, decision = _merge_chapter_status(next_memory.chapter_status.get(status.chapter_uid), status)
        if decision.stored_change:
            next_memory.chapter_status[status.chapter_uid] = merged
        decisions.append(decision)
        if decision.fresh_change:
            fresh_change_count += 1
            if decision.outcome == "added":
                true_additions += 1
            else:
                true_revisions += 1
        elif decision.outcome == "duplicate":
            duplicates += 1
        if decision.stored_change:
            stored_change_count += 1

    for question in patch.open_questions:
        existing = next((item for item in next_memory.open_questions if item.q_id == question.q_id), None)
        merged, decision = _merge_open_question(existing, question)
        if existing is None and merged is not None:
            next_memory.open_questions.append(merged)
        elif existing is not None and merged is not None and decision.stored_change:
            next_memory.open_questions = [
                merged if item.q_id == question.q_id else item for item in next_memory.open_questions
            ]
        decisions.append(decision)
        if decision.fresh_change:
            fresh_change_count += 1
            if decision.outcome == "added":
                true_additions += 1
                open_questions_added += 1
            else:
                true_revisions += 1
        elif decision.outcome == "duplicate":
            duplicates += 1
        if decision.stored_change:
            stored_change_count += 1

    next_memory.updated_at = updated_at
    next_memory.updated_by = updated_by
    return MemoryMergeResult(
        memory=next_memory,
        decisions=decisions,
        history=history,
        true_additions=true_additions,
        true_revisions=true_revisions,
        duplicates=duplicates,
        open_questions_added=open_questions_added,
        fresh_change_count=fresh_change_count,
        stored_change_count=stored_change_count,
    )


__all__ = [
    "CONVENTION_TOPICS",
    "PATTERN_TOPICS",
    "ChapterStatus",
    "ConventionNote",
    "EditMemory",
    "MemoryHistoryEntry",
    "MemoryMergeDecision",
    "MemoryMergeResult",
    "MemoryPatch",
    "OpenQuestion",
    "PatternNote",
    "canonical_convention_key",
    "canonical_pattern_key",
    "merge_edit_memory",
    "pattern_anchor_uids",
]
