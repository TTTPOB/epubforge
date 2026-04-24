"""Stage 3 artifact manifests, active pointer, and helper APIs.

This module is intentionally dependency-free within the project — it only
imports standard library modules and pydantic.  Do NOT add imports from
extract.py, assembler.py, or any other epubforge submodule.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class Stage3ContractError(RuntimeError):
    """Raised when a Stage 3 artifact fails contract validation."""


# ---------------------------------------------------------------------------
# Warning model
# ---------------------------------------------------------------------------


class Stage3Warning(BaseModel):
    message: str
    page: int | None = None
    item_ref: str | None = None
    severity: Literal["warning", "error"] = "warning"


# ---------------------------------------------------------------------------
# Manifest and pointer models
# ---------------------------------------------------------------------------


class Stage3Manifest(BaseModel):
    """Immutable artifact manifest written once after successful extraction."""

    schema_version: int = 3
    stage: int = 3
    mode: Literal["vlm", "skip_vlm"]
    artifact_id: str
    artifact_dir: str
    """Workdir-relative POSIX path to the artifact directory."""
    created_at: str
    """ISO-8601 UTC timestamp."""
    raw_sha256: str
    pages_sha256: str
    source_pdf: str
    """Workdir-relative POSIX path to the source PDF."""
    source_pdf_sha256: str
    selected_pages: list[int]
    toc_pages: list[int]
    complex_pages: list[int]
    page_filter: list[int] | None
    """Sorted list of page numbers that were filtered, or null for no filter."""
    unit_files: list[str]
    """Workdir-relative POSIX paths to unit JSON files."""
    sidecars: dict[str, str]
    """Named sidecar files: audit_notes, book_memory, evidence_index."""
    settings: dict[str, Any]
    """Mode-specific settings snapshot including null entries for inapplicable keys."""


class Stage3ActivePointer(BaseModel):
    """Small pointer file that references the currently active artifact."""

    schema_version: int = 3
    active_artifact_id: str
    manifest_path: str
    """Workdir-relative POSIX path to the artifact manifest.json."""
    manifest_sha256: str
    activated_at: str
    """ISO-8601 UTC timestamp."""


# ---------------------------------------------------------------------------
# Extraction result
# ---------------------------------------------------------------------------


class Stage3ExtractionResult(BaseModel):
    """Returned by both VLM and skip-VLM extractors before manifest activation."""

    mode: Literal["vlm", "skip_vlm"]
    unit_files: list[Path]
    audit_notes_path: Path
    book_memory_path: Path
    evidence_index_path: Path
    warnings_path: Path | None = None
    selected_pages: list[int]
    toc_pages: list[int]
    complex_pages: list[int]
    warnings: list[Stage3Warning] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Evidence index schema
# ---------------------------------------------------------------------------


class EvidenceIndex(BaseModel):
    """Schema for evidence_index.json written by every Stage 3 extractor."""

    schema_version: int = 3
    artifact_id: str
    mode: Literal["vlm", "skip_vlm"]
    source_pdf: str
    pages: dict[str, Any] = Field(default_factory=dict)
    """Keys are str(page_number); values follow the per-page evidence schema."""
    refs: dict[str, Any] = Field(default_factory=dict)
    """Ref string → {page, item_index} lookup."""


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Compute the hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_str(s: str) -> str:
    """Compute the hex SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel_posix(work_dir: Path, path: Path) -> str:
    """Return a workdir-relative POSIX string for *path*."""
    return path.relative_to(work_dir).as_posix()


# ---------------------------------------------------------------------------
# Artifact-id computation
# ---------------------------------------------------------------------------


def build_desired_stage3_manifest(
    *,
    mode: Literal["vlm", "skip_vlm"],
    source_pdf_rel: str,
    source_pdf_sha256: str,
    raw_sha256: str,
    pages_sha256: str,
    selected_pages: list[int],
    toc_pages: list[int],
    complex_pages: list[int],
    page_filter: list[int] | None,
    settings: dict[str, Any],
) -> str:
    """Compute and return the *artifact_id* for the described extraction.

    The artifact_id is the first 16 hex characters of the SHA-256 of the
    canonical JSON serialisation of all inputs that determine the extraction
    output.  Only the ID is returned; callers are responsible for constructing
    the full manifest after extraction succeeds.
    """
    payload: dict[str, Any] = {
        "schema_version": 3,
        "mode": mode,
        "source_pdf": source_pdf_rel,
        "source_pdf_sha256": source_pdf_sha256,
        "raw_sha256": raw_sha256,
        "pages_sha256": pages_sha256,
        "selected_pages": sorted(selected_pages),
        "toc_pages": sorted(toc_pages),
        "complex_pages": sorted(complex_pages),
        "page_filter": sorted(page_filter) if page_filter is not None else None,
        "settings": settings,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _active_pointer_path(work_dir: Path) -> Path:
    return work_dir / "03_extract" / "active_manifest.json"


def _artifact_dir_path(work_dir: Path, artifact_id: str) -> Path:
    return work_dir / "03_extract" / "artifacts" / artifact_id


def _manifest_path(work_dir: Path, artifact_id: str) -> Path:
    return _artifact_dir_path(work_dir, artifact_id) / "manifest.json"


# ---------------------------------------------------------------------------
# Active-pointer helpers
# ---------------------------------------------------------------------------


def active_manifest_matches_desired(work_dir: Path, desired_artifact_id: str) -> bool:
    """Return True iff the active pointer exists and points to *desired_artifact_id*.

    Does NOT validate the manifest or listed files — call
    :func:`validate_stage3_artifact` separately if you need full validation.
    """
    pointer_path = _active_pointer_path(work_dir)
    if not pointer_path.exists():
        return False
    try:
        raw = pointer_path.read_text(encoding="utf-8")
        pointer = Stage3ActivePointer.model_validate_json(raw)
    except Exception:
        return False
    return pointer.active_artifact_id == desired_artifact_id


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_stage3_artifact(work_dir: Path, manifest: Stage3Manifest) -> None:
    """Validate that the artifact described by *manifest* is fully intact.

    Checks:
    - The artifact directory exists.
    - manifest.json exists and its SHA-256 matches the active pointer's
      ``manifest_sha256`` (when an active pointer is present).
    - All unit_files listed in the manifest exist.
    - All sidecar files listed in ``manifest.sidecars`` exist.

    Raises :class:`Stage3ContractError` on any failure.
    """
    artifact_dir = work_dir / PurePosixPath(manifest.artifact_dir)
    if not artifact_dir.is_dir():
        raise Stage3ContractError(
            f"Artifact directory missing: {artifact_dir}"
        )

    manifest_file = artifact_dir / "manifest.json"
    if not manifest_file.exists():
        raise Stage3ContractError(
            f"manifest.json missing in artifact dir: {manifest_file}"
        )

    # Validate manifest sha against active pointer when present.
    pointer_path = _active_pointer_path(work_dir)
    if pointer_path.exists():
        try:
            raw_pointer = pointer_path.read_text(encoding="utf-8")
            pointer = Stage3ActivePointer.model_validate_json(raw_pointer)
        except Exception as exc:
            raise Stage3ContractError(f"Cannot parse active_manifest.json: {exc}") from exc

        if pointer.active_artifact_id == manifest.artifact_id:
            actual_sha = _sha256_str(manifest_file.read_text(encoding="utf-8"))
            if actual_sha != pointer.manifest_sha256:
                raise Stage3ContractError(
                    f"manifest.json SHA-256 mismatch for artifact {manifest.artifact_id}: "
                    f"expected {pointer.manifest_sha256}, got {actual_sha}"
                )

    # All unit files must exist.
    for rel in manifest.unit_files:
        path = work_dir / PurePosixPath(rel)
        if not path.exists():
            raise Stage3ContractError(f"Unit file missing: {path}")

    # All sidecar files must exist.
    for key, rel in manifest.sidecars.items():
        path = work_dir / PurePosixPath(rel)
        if not path.exists():
            raise Stage3ContractError(f"Sidecar '{key}' missing: {path}")


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def write_artifact_manifest_atomic(work_dir: Path, manifest: Stage3Manifest) -> Path:
    """Serialise *manifest* and write it to the artifact directory.

    The artifact directory is created if it does not exist.  Returns the path
    of the written manifest.json.

    The write is performed atomically (temp file + os.replace) so a crash
    during write does not leave a partial file that would be mistaken for a
    valid manifest.
    """
    artifact_dir = work_dir / PurePosixPath(manifest.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "manifest.json"

    serialised = manifest.model_dump_json(indent=2)

    fd, tmp_name = tempfile.mkstemp(
        dir=artifact_dir, prefix=".manifest_tmp_", suffix=".json"
    )
    try:
        os.write(fd, serialised.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_name, manifest_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return manifest_path


def activate_manifest_atomic(work_dir: Path, manifest: Stage3Manifest) -> None:
    """Atomically replace active_manifest.json to point to *manifest*.

    Reads the manifest.json that was already written (by
    :func:`write_artifact_manifest_atomic`) to compute the sha256.  Writes the
    active pointer to a temp file then renames it over the previous pointer.
    """
    artifact_dir = work_dir / PurePosixPath(manifest.artifact_dir)
    manifest_file = artifact_dir / "manifest.json"
    if not manifest_file.exists():
        raise Stage3ContractError(
            f"Cannot activate: manifest.json not found at {manifest_file}. "
            "Call write_artifact_manifest_atomic first."
        )

    manifest_text = manifest_file.read_text(encoding="utf-8")
    manifest_sha = _sha256_str(manifest_text)

    pointer = Stage3ActivePointer(
        schema_version=3,
        active_artifact_id=manifest.artifact_id,
        manifest_path=_rel_posix(work_dir, manifest_file),
        manifest_sha256=manifest_sha,
        activated_at=_now_utc_iso(),
    )

    pointer_dir = work_dir / "03_extract"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = pointer_dir / "active_manifest.json"

    serialised = pointer.model_dump_json(indent=2)

    fd, tmp_name = tempfile.mkstemp(
        dir=pointer_dir, prefix=".active_manifest_tmp_", suffix=".json"
    )
    try:
        os.write(fd, serialised.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_name, pointer_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------


def load_active_stage3_manifest(
    work_dir: Path,
) -> tuple[Stage3ActivePointer, Stage3Manifest]:
    """Load and return the active pointer and the manifest it references.

    Raises :class:`Stage3ContractError` if:
    - active_manifest.json does not exist or cannot be parsed.
    - The referenced manifest.json does not exist or cannot be parsed.
    - The manifest SHA-256 does not match.
    """
    pointer_path = _active_pointer_path(work_dir)
    if not pointer_path.exists():
        raise Stage3ContractError(
            f"No active manifest pointer found at {pointer_path}"
        )

    try:
        raw_pointer = pointer_path.read_text(encoding="utf-8")
        pointer = Stage3ActivePointer.model_validate_json(raw_pointer)
    except Exception as exc:
        raise Stage3ContractError(
            f"Cannot parse active_manifest.json: {exc}"
        ) from exc

    manifest_abs = work_dir / PurePosixPath(pointer.manifest_path)
    if not manifest_abs.exists():
        raise Stage3ContractError(
            f"Manifest referenced by active pointer not found: {manifest_abs}"
        )

    manifest_text = manifest_abs.read_text(encoding="utf-8")
    actual_sha = _sha256_str(manifest_text)
    if actual_sha != pointer.manifest_sha256:
        raise Stage3ContractError(
            f"manifest.json SHA-256 mismatch: expected {pointer.manifest_sha256}, "
            f"got {actual_sha}"
        )

    try:
        manifest = Stage3Manifest.model_validate_json(manifest_text)
    except Exception as exc:
        raise Stage3ContractError(
            f"Cannot parse manifest.json at {manifest_abs}: {exc}"
        ) from exc

    return pointer, manifest


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_manifest_paths(
    work_dir: Path, manifest: Stage3Manifest
) -> dict[str, Path]:
    """Resolve all relative POSIX paths in *manifest* to absolute :class:`Path` objects.

    Returns a dict with keys:
    - ``artifact_dir``
    - ``source_pdf``
    - ``unit_files``  (list stored under this key as a list[Path])
    - sidecar keys from ``manifest.sidecars``
    """
    result: dict[str, Any] = {
        "artifact_dir": work_dir / PurePosixPath(manifest.artifact_dir),
        "source_pdf": work_dir / PurePosixPath(manifest.source_pdf),
        "unit_files": [
            work_dir / PurePosixPath(rel) for rel in manifest.unit_files
        ],
    }
    for key, rel in manifest.sidecars.items():
        result[key] = work_dir / PurePosixPath(rel)
    return result
