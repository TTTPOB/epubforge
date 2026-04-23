"""State and path helpers for the editor tool surface."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from epubforge.editor.leases import LeaseState
from epubforge.editor.log import CURRENT_LOG, resolve_edit_log_paths
from epubforge.editor.memory import EditMemory
from epubforge.editor.ops import OpEnvelope
from epubforge.io import load_book, save_book
from epubforge.ir.semantic import (
    Block,
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Table,
    compute_block_uid_init,
    compute_chapter_uid_init,
)


EDIT_STATE_DIRNAME = "edit_state"
META_FILENAME = "meta.json"
MEMORY_FILENAME = "memory.json"
LEASES_FILENAME = "leases.json"
STAGING_FILENAME = "staging.jsonl"
AUDIT_DIRNAME = "audit"
DOCTOR_REPORT_FILENAME = "doctor_report.json"
DOCTOR_CONTEXT_FILENAME = "doctor_context.json"
SCRATCH_DIRNAME = "scratch"
SNAPSHOTS_DIRNAME = "snapshots"


@dataclass(frozen=True)
class EditorPaths:
    work_dir: Path
    edit_state_dir: Path
    book_path: Path
    meta_path: Path
    memory_path: Path
    leases_path: Path
    staging_path: Path
    audit_dir: Path
    doctor_report_path: Path
    doctor_context_path: Path
    scratch_dir: Path
    snapshots_dir: Path
    current_log_path: Path


def resolve_editor_paths(path: str | Path) -> EditorPaths:
    candidate = Path(path).expanduser()
    if candidate.name == EDIT_STATE_DIRNAME:
        work_dir = candidate.parent
        edit_state_dir = candidate
    else:
        work_dir = candidate
        edit_state_dir = candidate / EDIT_STATE_DIRNAME

    return EditorPaths(
        work_dir=work_dir,
        edit_state_dir=edit_state_dir,
        book_path=edit_state_dir / "book.json",
        meta_path=edit_state_dir / META_FILENAME,
        memory_path=edit_state_dir / MEMORY_FILENAME,
        leases_path=edit_state_dir / LEASES_FILENAME,
        staging_path=edit_state_dir / STAGING_FILENAME,
        audit_dir=edit_state_dir / AUDIT_DIRNAME,
        doctor_report_path=edit_state_dir / AUDIT_DIRNAME / DOCTOR_REPORT_FILENAME,
        doctor_context_path=edit_state_dir / AUDIT_DIRNAME / DOCTOR_CONTEXT_FILENAME,
        scratch_dir=edit_state_dir / SCRATCH_DIRNAME,
        snapshots_dir=edit_state_dir / SNAPSHOTS_DIRNAME,
        current_log_path=edit_state_dir / CURRENT_LOG,
    )


def book_id_from_paths(paths: EditorPaths) -> str:
    return paths.work_dir.name


def chapter_uids(book: Book) -> list[str]:
    return [chapter.uid for chapter in book.chapters if chapter.uid]


def ensure_work_dir(paths: EditorPaths) -> None:
    if not paths.work_dir.exists() or not paths.work_dir.is_dir():
        raise FileNotFoundError(f"work dir does not exist: {paths.work_dir}")


def ensure_uninitialized(paths: EditorPaths) -> None:
    if paths.meta_path.exists():
        raise FileExistsError(f"edit_state already initialized: {paths.edit_state_dir}")
    if paths.edit_state_dir.exists() and any(paths.edit_state_dir.iterdir()):
        raise FileExistsError(f"edit_state contains partial state and refuses overwrite: {paths.edit_state_dir}")


def ensure_initialized(paths: EditorPaths) -> None:
    missing: list[str] = []
    for required in (paths.meta_path, paths.book_path, paths.memory_path, paths.leases_path, paths.current_log_path):
        if not required.exists():
            missing.append(str(required))
    if missing:
        preview = ", ".join(missing)
        raise FileNotFoundError(f"editor state is not initialized: {preview}")


def source_artifact_path(paths: EditorPaths, artifact: str | Path) -> Path:
    candidate = Path(artifact).expanduser()
    if not candidate.is_absolute():
        candidate = paths.work_dir / candidate
    return candidate


def default_init_source(paths: EditorPaths) -> Path:
    return paths.work_dir / "05_semantic.json"


def _block_text_head(block: Block) -> str:
    if isinstance(block, Paragraph | Heading | Footnote):
        return block.text
    if isinstance(block, Table):
        return " ".join(part for part in (block.table_title, block.caption, block.html) if part)
    if isinstance(block, Figure):
        return block.caption or block.image_ref or "figure"
    if isinstance(block, Equation):
        return block.latex or block.image_ref or "equation"
    return block.kind


def initialize_book_state(book: Book, *, initialized_at: str, uid_seed: str | None = None) -> Book:
    seed = uid_seed or secrets.token_hex(8)
    chapters: list[Chapter] = []
    for ch_pos, chapter in enumerate(book.chapters):
        next_chapter = chapter.model_copy(deep=True)
        next_chapter.uid = next_chapter.uid or compute_chapter_uid_init(seed, ch_pos, next_chapter.title)
        blocks: list[Block] = []
        for block_pos, block in enumerate(next_chapter.blocks):
            next_block = block.model_copy(deep=True)
            next_block.uid = next_block.uid or compute_block_uid_init(
                seed,
                ch_pos,
                block_pos,
                next_block.kind,
                _block_text_head(next_block),
                next_block.provenance.page,
            )
            blocks.append(next_block)
        next_chapter.blocks = blocks
        chapters.append(next_chapter)
    return book.model_copy(
        update={
            "version": 0,
            "initialized_at": initialized_at,
            "uid_seed": seed,
            "chapters": chapters,
        }
    )


def load_editable_book(paths: EditorPaths) -> Book:
    return load_book(paths.book_path)


class EditorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initialized_at: str
    uid_seed: str


def build_editor_meta(book: Book) -> EditorMeta:
    return EditorMeta(initialized_at=book.initialized_at, uid_seed=book.uid_seed)


def load_editor_memory(paths: EditorPaths) -> EditMemory:
    return EditMemory.model_validate_json(paths.memory_path.read_text(encoding="utf-8"))


def load_lease_state(paths: EditorPaths) -> LeaseState:
    return LeaseState.model_validate_json(paths.leases_path.read_text(encoding="utf-8"))


def read_staging(paths: EditorPaths) -> list[OpEnvelope]:
    if not paths.staging_path.exists():
        return []
    entries: list[OpEnvelope] = []
    with paths.staging_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                entries.append(OpEnvelope.model_validate_json(line))
    return entries


def append_staging(paths: EditorPaths, envelopes: list[OpEnvelope]) -> None:
    paths.staging_path.parent.mkdir(parents=True, exist_ok=True)
    with paths.staging_path.open("a", encoding="utf-8") as fh:
        for envelope in envelopes:
            fh.write(envelope.model_dump_json())
            fh.write("\n")


def clear_staging(paths: EditorPaths) -> None:
    atomic_write_text(paths.staging_path, "")


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def atomic_write_model(path: Path, model: Any) -> None:
    if hasattr(model, "model_dump_json"):
        atomic_write_text(path, model.model_dump_json(indent=2))
        return
    atomic_write_json(path, model)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def write_initial_state(
    paths: EditorPaths,
    *,
    book: Book,
    memory: EditMemory,
    leases: LeaseState | None = None,
) -> None:
    paths.edit_state_dir.mkdir(parents=True, exist_ok=True)
    paths.audit_dir.mkdir(parents=True, exist_ok=True)
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    paths.snapshots_dir.mkdir(parents=True, exist_ok=True)
    save_book(book, paths.work_dir)
    atomic_write_model(paths.meta_path, build_editor_meta(book))
    atomic_write_model(paths.memory_path, memory)
    atomic_write_model(paths.leases_path, leases or LeaseState())
    atomic_write_text(paths.current_log_path, "")
    atomic_write_text(paths.staging_path, "")


def save_memory(paths: EditorPaths, memory: EditMemory) -> None:
    atomic_write_model(paths.memory_path, memory)


def save_leases(paths: EditorPaths, lease_state: LeaseState) -> None:
    atomic_write_model(paths.leases_path, lease_state)


def log_root(paths: EditorPaths) -> Path:
    return resolve_edit_log_paths(paths.edit_state_dir).root
