"""State and path helpers for the editor tool surface."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from epubforge.editor.log import CURRENT_LOG, resolve_edit_log_paths
from epubforge.editor.memory import EditMemory
from epubforge.io import load_book
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
AUDIT_DIRNAME = "audit"
DOCTOR_REPORT_FILENAME = "doctor_report.json"
DOCTOR_CONTEXT_FILENAME = "doctor_context.json"
SCRATCH_DIRNAME = "scratch"
AGENT_OUTPUTS_DIRNAME = "agent_outputs"
AGENT_OUTPUTS_ARCHIVES_DIRNAME = "archives"


@dataclass(frozen=True)
class EditorPaths:
    work_dir: Path
    edit_state_dir: Path
    book_path: Path
    meta_path: Path
    memory_path: Path
    audit_dir: Path
    doctor_report_path: Path
    doctor_context_path: Path
    scratch_dir: Path
    current_log_path: Path
    agent_outputs_dir: Path
    agent_outputs_archives_dir: Path
    vlm_observations_dir: Path        # edit_state / "vlm_observations"
    vlm_observation_index_path: Path  # edit_state / "vlm_observation_index.json"


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
        audit_dir=edit_state_dir / AUDIT_DIRNAME,
        doctor_report_path=edit_state_dir / AUDIT_DIRNAME / DOCTOR_REPORT_FILENAME,
        doctor_context_path=edit_state_dir / AUDIT_DIRNAME / DOCTOR_CONTEXT_FILENAME,
        scratch_dir=edit_state_dir / SCRATCH_DIRNAME,
        current_log_path=edit_state_dir / CURRENT_LOG,
        agent_outputs_dir=edit_state_dir / AGENT_OUTPUTS_DIRNAME,
        agent_outputs_archives_dir=edit_state_dir
        / AGENT_OUTPUTS_DIRNAME
        / AGENT_OUTPUTS_ARCHIVES_DIRNAME,
        vlm_observations_dir=edit_state_dir / "vlm_observations",
        vlm_observation_index_path=edit_state_dir / "vlm_observation_index.json",
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
        raise FileExistsError(
            f"edit_state contains partial state and refuses overwrite: {paths.edit_state_dir}"
        )


def ensure_initialized(paths: EditorPaths) -> None:
    missing: list[str] = []
    for required in (
        paths.meta_path,
        paths.book_path,
        paths.memory_path,
        paths.current_log_path,
    ):
        if not required.exists():
            missing.append(str(required))
    if missing:
        preview = ", ".join(missing)
        raise FileNotFoundError(f"editor state is not initialized: {preview}")


def default_init_source(paths: EditorPaths) -> Path:
    """Return path to the best available semantic source that matches the active Stage 3 manifest.

    Preference order: 05_semantic.json → 05_semantic_raw.json.
    Both are validated against the active manifest when a manifest pointer exists.
    If neither exists (or neither matches), raises FileNotFoundError with actionable guidance.
    """
    from epubforge.stage3_artifacts import (
        Stage3ContractError,
        load_active_stage3_manifest,
    )

    candidates = [
        paths.work_dir / "05_semantic.json",
        paths.work_dir / "05_semantic_raw.json",
    ]

    # Try to load active manifest for validation; if none exists fall back to simple file check.
    try:
        pointer, manifest = load_active_stage3_manifest(paths.work_dir)
        active_artifact_id = pointer.active_artifact_id
        active_sha = pointer.manifest_sha256
    except Stage3ContractError:
        # No active manifest — just return first existing candidate.
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"No semantic source found in {paths.work_dir}. "
            "Run `epubforge assemble` or `epubforge run` first."
        )

    # Manifest exists: find a candidate whose Book.extraction metadata matches.
    from epubforge.ir.semantic import Book as _Book

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            raw = candidate.read_text(encoding="utf-8")
            book = _Book.model_validate_json(raw)
        except Exception:
            continue
        if (
            book.extraction.artifact_id == active_artifact_id
            and book.extraction.stage3_manifest_sha256 == active_sha
        ):
            return candidate

    # No matching candidate found.
    raise FileNotFoundError(
        f"No semantic source in {paths.work_dir} matches the active Stage 3 artifact "
        f"(artifact_id={active_artifact_id}). "
        "Run `epubforge assemble` or `epubforge run` to regenerate it."
    )


def _block_text_head(block: Block) -> str:
    if isinstance(block, Paragraph | Heading | Footnote):
        return block.text
    if isinstance(block, Table):
        return " ".join(
            part for part in (block.table_title, block.caption, block.html) if part
        )
    if isinstance(block, Figure):
        return block.caption or block.image_ref or "figure"
    if isinstance(block, Equation):
        return block.latex or block.image_ref or "equation"
    return block.kind


def initialize_book_state(
    book: Book, *, initialized_at: str, uid_seed: str | None = None
) -> Book:
    seed = uid_seed or secrets.token_hex(8)
    chapters: list[Chapter] = []
    for ch_pos, chapter in enumerate(book.chapters):
        next_chapter = chapter.model_copy(deep=True)
        next_chapter.uid = next_chapter.uid or compute_chapter_uid_init(
            seed, ch_pos, next_chapter.title
        )
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
            "initialized_at": initialized_at,
            "uid_seed": seed,
            "chapters": chapters,
        }
    )


def load_editable_book(paths: EditorPaths) -> Book:
    return load_book(paths.book_path)


class Stage3EditorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["vlm", "skip_vlm", "docling", "unknown"]
    skipped_vlm: bool  # DEPRECATED: always True for new workdirs
    manifest_path: str
    manifest_sha256: str
    artifact_id: str
    selected_pages: list[int]
    complex_pages: list[int]
    source_pdf: str
    evidence_index_path: str
    extraction_warnings_path: str


class EditorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initialized_at: str
    uid_seed: str
    stage3: Stage3EditorMeta | None = None


def build_editor_meta(
    book: Book, *, stage3: Stage3EditorMeta | None = None
) -> EditorMeta:
    return EditorMeta(
        initialized_at=book.initialized_at, uid_seed=book.uid_seed, stage3=stage3
    )


def load_editor_meta(paths: EditorPaths) -> EditorMeta:
    return EditorMeta.model_validate_json(paths.meta_path.read_text(encoding="utf-8"))


def load_editor_memory(paths: EditorPaths) -> EditMemory:
    return EditMemory.model_validate_json(paths.memory_path.read_text(encoding="utf-8"))


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
    stage3: Stage3EditorMeta | None = None,
) -> None:
    """Create directory skeleton and write meta/memory/log.

    Does NOT write book.json — the caller is responsible for calling save_book
    after this function returns.
    """
    paths.edit_state_dir.mkdir(parents=True, exist_ok=True)
    paths.audit_dir.mkdir(parents=True, exist_ok=True)
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    paths.agent_outputs_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_model(paths.meta_path, build_editor_meta(book, stage3=stage3))
    atomic_write_model(paths.memory_path, memory)
    atomic_write_text(paths.current_log_path, "")


def save_memory(paths: EditorPaths, memory: EditMemory) -> None:
    atomic_write_model(paths.memory_path, memory)


def log_root(paths: EditorPaths) -> Path:
    return resolve_edit_log_paths(paths.edit_state_dir).root
