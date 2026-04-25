"""Tests for vlm_evidence models and storage helpers."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from epubforge.editor.vlm_evidence import (
    VLMFinding,
    VLMObservation,
    VLMObservationIndex,
    VLMObservationIndexEntry,
    VLMPageAnalysis,
    load_vlm_observation,
    load_vlm_observation_index,
    observation_path,
    save_vlm_observation,
    validate_evidence_refs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def editor_paths(tmp_path: Path):
    from epubforge.editor.state import resolve_editor_paths

    work = tmp_path / "work" / "book"
    work.mkdir(parents=True)
    return resolve_editor_paths(work)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_observation(
    *,
    page: int = 1,
    chapter_uid: str | None = None,
    findings: list[VLMFinding] | None = None,
) -> VLMObservation:
    return VLMObservation(
        observation_id=str(uuid4()),
        page=page,
        chapter_uid=chapter_uid,
        model="test-vlm",
        image_sha256="a" * 64,
        prompt_sha256="b" * 64,
        findings=findings or [],
        created_at="2026-04-25T12:00:00Z",
    )


# ---------------------------------------------------------------------------
# VLMFinding tests
# ---------------------------------------------------------------------------


def test_vlm_finding_valid() -> None:
    finding = VLMFinding(
        finding_type="text_mismatch",
        severity="warning",
        block_uids=["uid-abc", "uid-def"],
        description="Text on image differs from IR block.",
        suggested_fix="Replace with corrected text.",
    )
    assert finding.finding_type == "text_mismatch"
    assert finding.severity == "warning"
    assert finding.block_uids == ["uid-abc", "uid-def"]
    assert finding.suggested_fix == "Replace with corrected text."


def test_vlm_finding_empty_description() -> None:
    with pytest.raises(ValidationError):
        VLMFinding(
            finding_type="other",
            severity="info",
            description="   ",  # only whitespace
        )


def test_vlm_finding_invalid_type() -> None:
    with pytest.raises(ValidationError):
        VLMFinding(
            finding_type="nonexistent_type",  # type: ignore[arg-type]
            severity="error",
            description="Some description.",
        )


# ---------------------------------------------------------------------------
# VLMPageAnalysis tests
# ---------------------------------------------------------------------------


def test_vlm_page_analysis_valid() -> None:
    finding = VLMFinding(
        finding_type="quality_ok",
        severity="info",
        description="Page looks correct.",
    )
    analysis = VLMPageAnalysis(
        page=3,
        findings=[finding],
        summary="All content matches.",
    )
    assert analysis.page == 3
    assert len(analysis.findings) == 1
    assert analysis.summary == "All content matches."


# ---------------------------------------------------------------------------
# VLMObservation tests
# ---------------------------------------------------------------------------


def test_vlm_observation_valid() -> None:
    obs = _make_observation(
        page=5,
        chapter_uid="ch-01",
        findings=[
            VLMFinding(
                finding_type="missing_block",
                severity="error",
                description="Paragraph missing from IR.",
            )
        ],
    )
    assert obs.page == 5
    assert obs.chapter_uid == "ch-01"
    assert len(obs.findings) == 1
    assert obs.dpi == 200
    assert obs.source_pdf == ""


def test_vlm_observation_invalid_id() -> None:
    with pytest.raises(ValidationError):
        VLMObservation(
            observation_id="not-a-uuid",
            page=1,
            model="test-vlm",
            image_sha256="a" * 64,
            prompt_sha256="b" * 64,
            created_at="2026-04-25T12:00:00Z",
        )


def test_vlm_observation_invalid_sha256() -> None:
    with pytest.raises(ValidationError):
        VLMObservation(
            observation_id=str(uuid4()),
            page=1,
            model="test-vlm",
            image_sha256="abc123",  # too short
            prompt_sha256="b" * 64,
            created_at="2026-04-25T12:00:00Z",
        )


def test_vlm_observation_invalid_sha256_hex():
    """Non-hex characters in sha256 should be rejected."""
    with pytest.raises(ValidationError, match="hexadecimal"):
        VLMObservation(
            observation_id=str(uuid4()),
            page=1,
            model="test",
            image_sha256="z" * 64,
            prompt_sha256="a" * 64,
            findings=[],
            created_at="2026-04-25T12:00:00Z",
        )


def test_vlm_observation_invalid_timestamp() -> None:
    with pytest.raises(ValidationError):
        VLMObservation(
            observation_id=str(uuid4()),
            page=1,
            model="test-vlm",
            image_sha256="a" * 64,
            prompt_sha256="b" * 64,
            created_at="2026-04-25T12:00:00",  # missing trailing Z
        )


# ---------------------------------------------------------------------------
# Storage round-trip tests
# ---------------------------------------------------------------------------


def test_save_load_observation_round_trip(editor_paths) -> None:
    obs = _make_observation(page=2)
    saved_path = save_vlm_observation(editor_paths, obs)

    assert saved_path.exists()

    loaded = load_vlm_observation(editor_paths, obs.observation_id)
    assert loaded.observation_id == obs.observation_id
    assert loaded.page == obs.page
    assert loaded.model == obs.model
    assert loaded.image_sha256 == obs.image_sha256
    assert loaded.prompt_sha256 == obs.prompt_sha256
    assert loaded.created_at == obs.created_at


def test_save_updates_index(editor_paths) -> None:
    obs = _make_observation(page=3, chapter_uid="ch-02")
    save_vlm_observation(editor_paths, obs)

    index = load_vlm_observation_index(editor_paths)
    assert obs.observation_id in index.entries

    entry = index.entries[obs.observation_id]
    assert entry.page == 3
    assert entry.chapter_uid == "ch-02"
    assert entry.findings_count == 0
    assert entry.model == "test-vlm"


def test_load_observation_not_found(editor_paths) -> None:
    with pytest.raises(FileNotFoundError, match="VLM observation not found"):
        load_vlm_observation(editor_paths, str(uuid4()))


def test_load_index_empty(editor_paths) -> None:
    # No index file should exist yet
    assert not editor_paths.vlm_observation_index_path.exists()

    index = load_vlm_observation_index(editor_paths)
    assert isinstance(index, VLMObservationIndex)
    assert index.schema_version == 1
    assert index.entries == {}


# ---------------------------------------------------------------------------
# validate_evidence_refs tests
# ---------------------------------------------------------------------------


def test_validate_evidence_refs_all_valid(editor_paths) -> None:
    obs1 = _make_observation(page=1)
    obs2 = _make_observation(page=2)
    save_vlm_observation(editor_paths, obs1)
    save_vlm_observation(editor_paths, obs2)

    errors = validate_evidence_refs(
        [obs1.observation_id, obs2.observation_id], editor_paths
    )
    assert errors == []


def test_validate_evidence_refs_some_invalid(editor_paths) -> None:
    obs = _make_observation(page=1)
    save_vlm_observation(editor_paths, obs)

    fake_id = str(uuid4())
    errors = validate_evidence_refs([obs.observation_id, fake_id], editor_paths)
    assert len(errors) == 1
    assert fake_id in errors[0]


def test_validate_evidence_refs_empty(editor_paths) -> None:
    errors = validate_evidence_refs([], editor_paths)
    assert errors == []


# ---------------------------------------------------------------------------
# Multiple observations index test
# ---------------------------------------------------------------------------


def test_multiple_observations_index(editor_paths) -> None:
    observations = [_make_observation(page=i + 1) for i in range(5)]
    for obs in observations:
        save_vlm_observation(editor_paths, obs)

    index = load_vlm_observation_index(editor_paths)
    assert len(index.entries) == 5

    for obs in observations:
        assert obs.observation_id in index.entries
        entry = index.entries[obs.observation_id]
        assert entry.page == obs.page
        assert entry.model == obs.model
