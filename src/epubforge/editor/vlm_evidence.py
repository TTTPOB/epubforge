"""VLM observation models and storage helpers for the editor package.

Observations are the evidence units produced by a VLM page-analysis pass.
They can be referenced by AgentOutput.evidence_refs and BookPatch.evidence_refs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from epubforge.editor._validators import (
    StrictModel,
    require_non_empty,
    validate_utc_iso_timestamp,
    validate_uuid4,
)


class VLMFinding(StrictModel):
    """Single structured finding from a VLM observation."""

    finding_type: Literal[
        "missing_block",   # VLM sees content on the image that is absent from IR
        "extra_block",     # IR has content not visible on the image
        "text_mismatch",   # IR block text differs from what appears on the image
        "role_mismatch",   # block role/kind classification is wrong
        "layout_issue",    # layout problem (columns, reading order, etc.)
        "table_error",     # table structure or content error
        "footnote_error",  # footnote matching or content error
        "figure_issue",    # image/caption problem
        "heading_issue",   # heading level or content problem
        "quality_ok",      # VLM confirms current state is correct
        "other",           # other issue
    ]
    severity: Literal["info", "warning", "error"]
    block_uids: list[str] = Field(default_factory=list)
    """Block UIDs this finding refers to (may be empty if the issue is about missing content)."""
    description: str
    """Human-readable description of the finding."""
    suggested_fix: str | None = None
    """Optional suggestion for how to fix this issue."""

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return require_non_empty(value, field_name="description")


class VLMPageAnalysis(StrictModel):
    """VLM response format for page-level analysis.

    This is the response_format passed to LLMClient.chat_parsed().
    It is parsed from VLM output, then converted to VLMObservation.
    """

    page: int
    findings: list[VLMFinding] = Field(default_factory=list)
    summary: str = ""
    """Brief overall assessment of extraction quality for this page."""


class VLMObservation(StrictModel):
    """Stored VLM observation with full provenance metadata.

    This is the evidence unit that can be referenced by
    AgentOutput.evidence_refs and BookPatch.evidence_refs.
    """

    observation_id: str
    """UUID4, unique identifier for this observation."""
    page: int
    """1-based page number analyzed."""
    chapter_uid: str | None = None
    """Chapter UID scope (if provided at invocation time)."""
    related_block_uids: list[str] = Field(default_factory=list)
    """Block UIDs in scope (if provided at invocation time)."""
    model: str
    """VLM model identifier used for this observation."""
    image_sha256: str
    """SHA-256 hex digest of the rendered page JPEG."""
    prompt_sha256: str
    """SHA-256 hex digest of the serialized prompt messages."""
    findings: list[VLMFinding] = Field(default_factory=list)
    """Structured findings from the VLM analysis."""
    raw_text: str | None = None
    """Raw VLM response text (if available)."""
    created_at: str
    """ISO-8601 UTC timestamp."""
    dpi: int = 200
    """DPI used for rendering the page image."""
    source_pdf: str = ""
    """Workdir-relative path to the source PDF."""

    @field_validator("observation_id")
    @classmethod
    def _validate_observation_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="observation_id")

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: str) -> str:
        return validate_utc_iso_timestamp(value, field_name="created_at")

    @field_validator("image_sha256", "prompt_sha256")
    @classmethod
    def _validate_sha256(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "sha256")
        value = require_non_empty(value, field_name=field_name)
        if len(value) != 64:
            raise ValueError(
                f"{field_name} must be a 64-character hex SHA-256 digest"
            )
        try:
            int(value, 16)
        except ValueError:
            raise ValueError(
                f"{field_name} must contain only hexadecimal characters"
            )
        return value


class VLMObservationIndexEntry(StrictModel):
    """Summary entry in the observation index for quick lookup."""

    observation_id: str
    page: int
    chapter_uid: str | None = None
    findings_count: int
    created_at: str
    model: str


class VLMObservationIndex(StrictModel):
    """Index mapping observation_id to metadata for quick lookup.

    Stored at edit_state/vlm_observation_index.json.
    """

    schema_version: int = 1
    entries: dict[str, VLMObservationIndexEntry] = Field(default_factory=dict)
    """observation_id -> VLMObservationIndexEntry"""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _generate_observation_id() -> str:
    """Generate a new UUID4 observation ID."""
    return str(uuid4())


def _compute_sha256_bytes(data: bytes) -> str:
    """Compute hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _compute_sha256_str(data: str) -> str:
    """Compute hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def observation_path(paths: object, observation_id: str) -> Path:
    """Return the storage path for a single observation JSON."""
    vlm_dir: Path = paths.vlm_observations_dir  # type: ignore[attr-defined]
    return vlm_dir / f"{observation_id}.json"


def save_vlm_observation(paths: object, obs: VLMObservation) -> Path:
    """Atomically write a VLMObservation and update the index.

    Returns the path of the written observation file.
    """
    from epubforge.editor.state import atomic_write_model

    vlm_dir: Path = paths.vlm_observations_dir  # type: ignore[attr-defined]
    index_path: Path = paths.vlm_observation_index_path  # type: ignore[attr-defined]
    vlm_dir.mkdir(parents=True, exist_ok=True)

    obs_path = observation_path(paths, obs.observation_id)
    atomic_write_model(obs_path, obs)

    # Update index
    index = load_vlm_observation_index(paths)
    index.entries[obs.observation_id] = VLMObservationIndexEntry(
        observation_id=obs.observation_id,
        page=obs.page,
        chapter_uid=obs.chapter_uid,
        findings_count=len(obs.findings),
        created_at=obs.created_at,
        model=obs.model,
    )
    atomic_write_model(index_path, index)

    return obs_path


def load_vlm_observation_index(paths: object) -> VLMObservationIndex:
    """Load the observation index, returning an empty index if not found."""
    index_path: Path = paths.vlm_observation_index_path  # type: ignore[attr-defined]
    if not index_path.exists():
        return VLMObservationIndex()
    return VLMObservationIndex.model_validate_json(
        index_path.read_text(encoding="utf-8")
    )


def load_vlm_observation(paths: object, observation_id: str) -> VLMObservation:
    """Load a single VLMObservation by ID.

    Raises FileNotFoundError if the observation does not exist.
    """
    obs_path = observation_path(paths, observation_id)
    if not obs_path.exists():
        raise FileNotFoundError(f"VLM observation not found: {observation_id}")
    return VLMObservation.model_validate_json(
        obs_path.read_text(encoding="utf-8")
    )


def validate_evidence_refs(
    evidence_refs: list[str],
    paths: object,
) -> list[str]:
    """Validate that all evidence_refs exist in the observation index.

    Returns a list of error strings. An empty list means all refs are valid.
    """
    if not evidence_refs:
        return []

    index = load_vlm_observation_index(paths)
    errors: list[str] = []
    for ref in evidence_refs:
        if ref not in index.entries:
            errors.append(
                f"evidence_ref {ref!r} not found in VLM observation index"
            )
    return errors


__all__ = [
    "VLMFinding",
    "VLMObservation",
    "VLMObservationIndex",
    "VLMObservationIndexEntry",
    "VLMPageAnalysis",
    "_compute_sha256_bytes",
    "_compute_sha256_str",
    "_generate_observation_id",
    "load_vlm_observation",
    "load_vlm_observation_index",
    "observation_path",
    "save_vlm_observation",
    "validate_evidence_refs",
]
