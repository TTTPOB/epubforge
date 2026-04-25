"""AgentOutput model, storage helpers, and validate/submit business logic."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from epubforge.editor._validators import (
    StrictModel,
    require_non_empty,
    validate_utc_iso_timestamp,
    validate_uuid4,
)
from epubforge.editor.cli_support import CommandError
from epubforge.editor.memory import (
    EditMemory,
    MemoryMergeDecision,
    MemoryPatch,
    OpenQuestion,
    merge_edit_memory,
)
from epubforge.editor.log import append_audit_event
from epubforge.editor.patch_commands import (
    PatchCommand,
    PatchCommandError,
    compile_patch_commands,
)
from epubforge.editor.patches import BookPatch, PatchError, apply_book_patch
from epubforge.editor.state import (
    EditorPaths,
    atomic_write_model,
    atomic_write_text,
    save_memory,
)
from epubforge.ir.semantic import Book

AgentKind = Literal["scanner", "fixer", "reviewer", "supervisor"]


# ---------------------------------------------------------------------------
# AgentOutput model
# ---------------------------------------------------------------------------


class AgentOutput(StrictModel):
    """Structured output produced by an agent during a work session."""

    output_id: str
    kind: AgentKind
    agent_id: str
    chapter_uid: str | None = None
    created_at: str
    updated_at: str
    patches: list[BookPatch] = Field(default_factory=list)
    commands: list[PatchCommand] = Field(default_factory=list)
    memory_patches: list[MemoryPatch] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # evidence_refs validated by _validate_agent_output_impl when paths is provided
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("output_id")
    @classmethod
    def _validate_output_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="output_id")

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        return require_non_empty(value, field_name="agent_id")

    @field_validator("chapter_uid")
    @classmethod
    def _validate_chapter_uid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_non_empty(value, field_name="chapter_uid")

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamps(cls, value: str, info) -> str:
        return validate_utc_iso_timestamp(value, field_name=info.field_name)

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, value: list[str]) -> list[str]:
        return [note.strip() for note in value if note.strip()]

    @model_validator(mode="after")
    def _validate_timestamps_order(self) -> AgentOutput:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be >= created_at")
        return self


# ---------------------------------------------------------------------------
# SubmitResult
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SubmitResult:
    """Result of a successful submit_agent_output call."""

    submitted: bool
    output_id: str
    patches_applied: int
    memory_patches_applied: int
    archive_path: str
    memory_decisions: list[dict]
    errors: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class StageResult:
    """Result of validating and archiving an AgentOutput without applying patches."""

    staged: bool
    output_id: str
    patches_validated: int
    archive_path: str
    errors: list[str] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def load_agent_output(paths: EditorPaths, output_id: str) -> AgentOutput:
    """Load and validate AgentOutput from disk.

    Raises CommandError if not found or if the output has already been archived
    (i.e., no in-progress file but an archive entry exists).
    """
    output_path = paths.agent_outputs_dir / f"{output_id}.json"
    if not output_path.exists():
        # Check whether it was already submitted (archived)
        if paths.agent_outputs_archives_dir.exists():
            archived = list(
                paths.agent_outputs_archives_dir.glob(f"{output_id}_*.json")
            )
            if archived:
                raise CommandError(
                    f"output already submitted: {output_id}",
                    exit_code=1,
                    payload={"error": f"output already submitted: {output_id}"},
                )
        raise CommandError(
            f"output not found: {output_id}",
            exit_code=1,
            payload={"error": f"output not found: {output_id}"},
        )
    try:
        raw = output_path.read_text(encoding="utf-8")
        return AgentOutput.model_validate_json(raw)
    except Exception as exc:
        raise CommandError(
            f"output file corrupted or invalid: {output_id}: {exc}",
            exit_code=1,
            payload={"error": f"output file corrupted or invalid: {output_id}: {exc}"},
        ) from exc


def save_agent_output(paths: EditorPaths, output: AgentOutput) -> None:
    """Atomically save AgentOutput back to disk using atomic_write_model."""
    paths.agent_outputs_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_model(paths.agent_outputs_dir / f"{output.output_id}.json", output)


# ---------------------------------------------------------------------------
# Internal UID existence helper
# ---------------------------------------------------------------------------


def _uid_exists(uid: str, book: Book) -> bool:
    """Check if uid is a chapter uid or block uid in book."""
    for chapter in book.chapters:
        if chapter.uid == uid:
            return True
        for block in chapter.blocks:
            if block.uid == uid:
                return True
    return False


# ---------------------------------------------------------------------------
# AgentOutputValidationResult
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AgentOutputValidationResult:
    """Internal validation result shared by validate and submit paths."""

    errors: list[str]
    compiled_patches: list[BookPatch]
    book_after_commands: Book | None


# ---------------------------------------------------------------------------
# Internal: scope and kind permission check helper
# ---------------------------------------------------------------------------


def _check_patches_permissions(
    patches: list[BookPatch],
    output: AgentOutput,
    prefix: str,
) -> list[str]:
    """Check scope consistency and kind-specific permissions for a list of patches.

    prefix is used in error messages (e.g. "patches" or "compiled_patches").
    """
    errors: list[str] = []

    # §5.6 — scope consistency (chapter-scoped output cannot have book-wide patches)
    for i, patch in enumerate(patches):
        if output.chapter_uid is not None:
            if patch.scope.chapter_uid is None:
                errors.append(
                    f"{prefix}[{i}]: chapter-scoped output (chapter_uid={output.chapter_uid!r}) "
                    f"may not contain book-wide patches (scope.chapter_uid=None)"
                )
            elif patch.scope.chapter_uid != output.chapter_uid:
                errors.append(
                    f"{prefix}[{i}]: scope.chapter_uid {patch.scope.chapter_uid!r} "
                    f"does not match output.chapter_uid {output.chapter_uid!r}"
                )

    # §5.7 — kind-specific permissions

    # scanner: set_field only, no book-wide scope
    if output.kind == "scanner":
        for i, patch in enumerate(patches):
            for j, change in enumerate(patch.changes):
                if change.op != "set_field":
                    errors.append(
                        f"scanner output {prefix}[{i}].changes[{j}]: "
                        f"scanner may only submit set_field changes, got {change.op!r}"
                    )
            if patch.scope.chapter_uid is None:
                errors.append(
                    f"scanner output {prefix}[{i}]: book-wide scope requires supervisor"
                )

    # fixer: no topology ops in BookPatch; use PatchCommand instead
    if output.kind == "fixer":
        topology_ops = {"insert_node", "delete_node", "move_node"}
        for i, patch in enumerate(patches):
            for j, change in enumerate(patch.changes):
                if change.op in topology_ops:
                    errors.append(
                        f"fixer output {prefix}[{i}].changes[{j}]: "
                        f"fixer may not submit topology changes directly via BookPatch "
                        f"({change.op!r}); use PatchCommand instead (enforced by Phase 3 compiler)"
                    )
            if output.chapter_uid is not None and patch.scope.chapter_uid is None:
                errors.append(
                    f"fixer output {prefix}[{i}]: chapter-scoped fixer "
                    f"may not use book-wide scope"
                )

    # reviewer: set_field only
    if output.kind == "reviewer":
        allowed_ops = {"set_field"}
        for i, patch in enumerate(patches):
            for j, change in enumerate(patch.changes):
                if change.op not in allowed_ops:
                    errors.append(
                        f"reviewer output {prefix}[{i}].changes[{j}]: "
                        f"reviewer may only submit set_field changes, got {change.op!r}"
                    )

    # supervisor: unrestricted (no additional checks beyond BookPatch validate)

    return errors


# ---------------------------------------------------------------------------
# Internal: shared validation implementation
# ---------------------------------------------------------------------------


def _validate_agent_output_impl(
    output: AgentOutput,
    book: Book,
    *,
    paths: EditorPaths | None = None,
) -> AgentOutputValidationResult:
    """Single shared validation helper used by both validate_agent_output and submit_agent_output.

    Performs all semantic validation and returns an AgentOutputValidationResult containing
    errors, compiled_patches, and book_after_commands.
    """
    errors: list[str] = []

    # §5.2 — chapter_uid existence
    if output.chapter_uid is not None:
        chapter_uids = {ch.uid for ch in book.chapters}
        if output.chapter_uid not in chapter_uids:
            errors.append(f"chapter_uid not found: {output.chapter_uid}")

    # §5.2b — agent_id consistency checks (NEW)
    for i, cmd in enumerate(output.commands):
        if cmd.agent_id != output.agent_id:
            errors.append(
                f"commands[{i}] ({cmd.command_id}): agent_id mismatch: "
                f"command.agent_id={cmd.agent_id!r} != output.agent_id={output.agent_id!r}"
            )
    for i, patch in enumerate(output.patches):
        if patch.agent_id != output.agent_id:
            errors.append(
                f"patches[{i}] ({patch.patch_id}): agent_id mismatch: "
                f"patch.agent_id={patch.agent_id!r} != output.agent_id={output.agent_id!r}"
            )

    # §5.3 — scanner/reviewer may not submit commands (NEW)
    if output.kind in ("scanner", "reviewer") and output.commands:
        errors.append(f"{output.kind} may not submit PatchCommands")

    # §5.4 — Compile commands with evolving book
    compiled_patches: list[BookPatch] = []
    book_after_commands: Book | None = None
    compilation_failed = False

    if output.commands:
        current_book = book
        for i, cmd in enumerate(output.commands):
            try:
                result = compile_patch_commands(
                    current_book,
                    [cmd],
                    output_kind=output.kind,
                    output_chapter_uid=output.chapter_uid,
                )
                compiled_patches.extend(result.patches)
                current_book = result.book_after_commands
            except PatchCommandError as exc:
                errors.append(f"commands[{i}] ({cmd.command_id}): {exc.reason}")
                # Record skipped for subsequent commands
                for j in range(i + 1, len(output.commands)):
                    skipped_cmd = output.commands[j]
                    errors.append(
                        f"commands[{j}] ({skipped_cmd.command_id}): skipped because previous command failed"
                    )
                compilation_failed = True
                break
        if not compilation_failed:
            book_after_commands = current_book
    else:
        book_after_commands = book

    # §5.5 — validate output.patches (skip if compilation failed)
    if compilation_failed:
        errors.append(
            "skipped output.patches validation because command compilation failed"
        )
    else:
        assert book_after_commands is not None
        current_book = book_after_commands
        for i, patch in enumerate(output.patches):
            try:
                current_book = apply_book_patch(current_book, patch)
            except PatchError as e:
                errors.append(f"patches[{i}] ({patch.patch_id}): {e.reason}")

    # §5.6 + §5.7 — scope and kind permission checks on ALL patches
    all_patches_for_check = compiled_patches + list(output.patches)
    errors.extend(_check_patches_permissions(all_patches_for_check, output, "patches"))

    # §5.5 — MemoryPatch UID reference validation
    for i, mp in enumerate(output.memory_patches):
        # Check chapter_status UIDs
        for status in mp.chapter_status:
            if status.chapter_uid not in {ch.uid for ch in book.chapters}:
                errors.append(
                    f"memory_patches[{i}].chapter_status: "
                    f"chapter_uid not found: {status.chapter_uid}"
                )

        # Check open_questions context UIDs within MemoryPatch
        for q in mp.open_questions:
            for uid in q.context_uids:
                if not _uid_exists(uid, book):
                    errors.append(
                        f"memory_patches[{i}].open_questions[q_id={q.q_id}]: "
                        f"context_uid not found: {uid}"
                    )

        # Check convention evidence_uids
        for conv in mp.conventions:
            for uid in conv.evidence_uids:
                if not _uid_exists(uid, book):
                    errors.append(
                        f"memory_patches[{i}].conventions[key={conv.canonical_key}]: "
                        f"evidence_uid not found: {uid}"
                    )

        # Check pattern affected_uids
        for pattern in mp.patterns:
            for uid in pattern.affected_uids:
                if not _uid_exists(uid, book):
                    errors.append(
                        f"memory_patches[{i}].patterns[key={pattern.canonical_key}]: "
                        f"affected_uid not found: {uid}"
                    )

    # §5.7 — scanner must include read_passes update for its chapter
    if output.kind == "scanner" and output.chapter_uid is not None:
        has_read_pass_update = any(
            cs.chapter_uid == output.chapter_uid and cs.read_passes > 0
            for mp in output.memory_patches
            for cs in mp.chapter_status
        )
        if not has_read_pass_update:
            errors.append(
                f"scanner output must include a chapter_status entry for "
                f"chapter_uid={output.chapter_uid!r} with read_passes > 0"
            )

    # §5.8 — OpenQuestion context_uid existence (output.open_questions)
    for i, q in enumerate(output.open_questions):
        for uid in q.context_uids:
            if not _uid_exists(uid, book):
                errors.append(
                    f"open_questions[{i}] (q_id={q.q_id}): context_uid not found: {uid}"
                )

    # Phase 8: evidence_refs validation
    if paths is not None:
        from epubforge.editor.vlm_evidence import validate_evidence_refs

        # Output-level evidence_refs
        ref_errors = validate_evidence_refs(output.evidence_refs, paths)
        errors.extend(ref_errors)

        # Per-patch evidence_refs
        for i, patch in enumerate(output.patches):
            if patch.evidence_refs:
                patch_ref_errors = validate_evidence_refs(patch.evidence_refs, paths)
                for err in patch_ref_errors:
                    errors.append(f"patches[{i}]: {err}")

        # Per-compiled-patch evidence_refs (from PatchCommand compilation)
        for i, patch in enumerate(compiled_patches):
            if patch.evidence_refs:
                patch_ref_errors = validate_evidence_refs(patch.evidence_refs, paths)
                for err in patch_ref_errors:
                    errors.append(f"compiled_patches[{i}]: {err}")

    return AgentOutputValidationResult(
        errors=errors,
        compiled_patches=compiled_patches,
        book_after_commands=book_after_commands,
    )


# ---------------------------------------------------------------------------
# Semantic validation (public API)
# ---------------------------------------------------------------------------


def validate_agent_output(
    output: AgentOutput,
    book: Book,
    *,
    paths: EditorPaths | None = None,
) -> list[str]:
    """Full semantic validation of an AgentOutput against the current Book.

    Returns a list of error strings. Empty list means the output is valid.
    Does not fail-fast — collects all errors before returning.
    """
    result = _validate_agent_output_impl(output, book, paths=paths)
    return result.errors


# ---------------------------------------------------------------------------
# Sequential apply helpers
# ---------------------------------------------------------------------------


def apply_patches_sequentially(
    patches: list[BookPatch],
    book: Book,
) -> tuple[Book, str | None]:
    """Apply each BookPatch in order.

    Returns (updated_book, error_message).
    error_message is None on success.
    If any patch fails, returns the original book (unchanged) and an error message.
    """
    current = book
    for patch in patches:
        try:
            current = apply_book_patch(current, patch)
        except PatchError as e:
            return book, f"patch {patch.patch_id} failed: {e.reason}"
    return current, None


def apply_memory_patches_sequentially(
    memory_patches: list[MemoryPatch],
    memory: EditMemory,
    *,
    agent_id: str,
    now: str,
) -> tuple[EditMemory, list[MemoryMergeDecision]]:
    """Apply each MemoryPatch in order to the evolving memory state.

    Returns (final_memory, all_decisions_accumulated).
    Each merge uses the output of the previous merge as its input memory.
    """
    current_memory = memory
    all_decisions: list[MemoryMergeDecision] = []
    for mp in memory_patches:
        result = merge_edit_memory(
            current_memory,
            mp,
            updated_at=now,
            updated_by=agent_id,
        )
        current_memory = result.memory
        all_decisions.extend(result.decisions)
    return current_memory, all_decisions


# ---------------------------------------------------------------------------
# Archive helper
# ---------------------------------------------------------------------------


def archive_agent_output(
    paths: EditorPaths, output: AgentOutput, submitted_at: str
) -> Path:
    """Atomically archive a submitted AgentOutput and remove the in-progress file.

    Returns the path of the created archive file.
    """
    submitted_compact = (
        submitted_at.replace(":", "").replace("-", "").replace("T", "-").rstrip("Z")
    )
    archive_name = f"{output.output_id}_{submitted_compact}.json"
    archive_path = paths.agent_outputs_archives_dir / archive_name
    paths.agent_outputs_archives_dir.mkdir(parents=True, exist_ok=True)

    # Read the current in-progress file content
    src = paths.agent_outputs_dir / f"{output.output_id}.json"
    content = src.read_text(encoding="utf-8")

    # Atomically write to archive (temp → os.replace), then remove source
    atomic_write_text(archive_path, content)
    src.unlink()

    return archive_path


# ---------------------------------------------------------------------------
# submit_agent_output
# ---------------------------------------------------------------------------


def submit_agent_output(
    output: AgentOutput,
    book: Book,
    memory: EditMemory,
    paths: EditorPaths,
    *,
    now: str,
) -> SubmitResult:
    """Validate → compile commands → apply patches → apply memory → save → archive.

    Returns a SubmitResult. Raises CommandError on validation or apply failure.
    """
    # Step 1: validate (uses shared impl — includes command compilation)
    validation = _validate_agent_output_impl(output, book, paths=paths)
    if validation.errors:
        return SubmitResult(
            submitted=False,
            output_id=output.output_id,
            patches_applied=0,
            memory_patches_applied=0,
            archive_path="",
            memory_decisions=[],
            errors=validation.errors,
        )

    # Step 2: combine compiled + direct patches (already validated)
    all_patches = list(validation.compiled_patches) + list(output.patches)

    # Step 3: apply patches sequentially
    new_book, patch_error = apply_patches_sequentially(all_patches, book)
    if patch_error is not None:
        raise CommandError(
            patch_error,
            exit_code=1,
            payload={"error": patch_error},
        )

    # Step 4: apply memory patches sequentially
    try:
        new_memory, all_decisions = apply_memory_patches_sequentially(
            output.memory_patches,
            memory,
            agent_id=output.agent_id,
            now=now,
        )
    except Exception as exc:
        raise CommandError(
            f"memory merge failed: {exc}",
            exit_code=1,
            payload={"error": f"memory merge failed: {exc}"},
        ) from exc

    # Step 5: save book
    atomic_write_model(paths.book_path, new_book)

    # Step 6: save memory
    save_memory(paths, new_memory)

    # Step 7: archive output
    archive_path = archive_agent_output(paths, output, submitted_at=now)

    append_audit_event(
        paths.edit_state_dir,
        kind="agent_output_submitted",
        ts=now,
        payload={
            "output_id": output.output_id,
            "agent_id": output.agent_id,
            "kind": output.kind,
            "chapter_uid": output.chapter_uid,
            "patch_ids": [patch.patch_id for patch in all_patches],
            "patches_applied": len(all_patches),
            "memory_patches_applied": len(output.memory_patches),
            "archive_path": str(archive_path),
        },
    )

    return SubmitResult(
        submitted=True,
        output_id=output.output_id,
        patches_applied=len(all_patches),
        memory_patches_applied=len(output.memory_patches),
        archive_path=str(archive_path),
        memory_decisions=[d.model_dump(mode="json") for d in all_decisions],
    )


def stage_agent_output(
    output: AgentOutput,
    book: Book,
    paths: EditorPaths,
    *,
    now: str,
) -> StageResult:
    """Validate and archive an AgentOutput without mutating book.json or memory.json."""

    validation = _validate_agent_output_impl(output, book, paths=paths)
    if validation.errors:
        return StageResult(
            staged=False,
            output_id=output.output_id,
            patches_validated=0,
            archive_path="",
            errors=validation.errors,
        )

    all_patches = list(validation.compiled_patches) + list(output.patches)
    archive_path = archive_agent_output(paths, output, submitted_at=now)
    append_audit_event(
        paths.edit_state_dir,
        kind="agent_output_staged",
        ts=now,
        payload={
            "output_id": output.output_id,
            "agent_id": output.agent_id,
            "kind": output.kind,
            "chapter_uid": output.chapter_uid,
            "patch_ids": [patch.patch_id for patch in all_patches],
            "patches_validated": len(all_patches),
            "memory_patches_validated": len(output.memory_patches),
            "archive_path": str(archive_path),
        },
    )
    return StageResult(
        staged=True,
        output_id=output.output_id,
        patches_validated=len(all_patches),
        archive_path=str(archive_path),
    )


__all__ = [
    "AgentKind",
    "AgentOutput",
    "StageResult",
    "SubmitResult",
    "apply_memory_patches_sequentially",
    "apply_patches_sequentially",
    "archive_agent_output",
    "load_agent_output",
    "save_agent_output",
    "stage_agent_output",
    "submit_agent_output",
    "validate_agent_output",
]
