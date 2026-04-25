"""Tests for evidence_refs validation integration (Phase 8D)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from epubforge.editor.agent_output import (
    AgentOutput,
    save_agent_output,
    submit_agent_output,
    validate_agent_output,
)
from epubforge.editor.patches import BookPatch, PatchScope
from epubforge.editor.state import (
    load_editor_memory,
    resolve_editor_paths,
)
from epubforge.editor.vlm_evidence import VLMObservation, save_vlm_observation
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov() -> Provenance:
    return Provenance(page=1, bbox=None, source="passthrough")


def _now_ts() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_book() -> Book:
    return Book(
        title="Test Book",
        chapters=[
            Chapter(
                uid="ch-001",
                title="Chapter 1",
                blocks=[
                    Paragraph(
                        uid="blk-001",
                        text="Hello world",
                        role="body",
                        provenance=_prov(),
                    ),
                ],
            ),
        ],
    )


def _make_agent_output(
    kind: str = "fixer",
    agent_id: str = "fixer-1",
    evidence_refs: list[str] | None = None,
    patches: list[BookPatch] | None = None,
    **kwargs,
) -> AgentOutput:
    now = _now_ts()
    return AgentOutput(
        output_id=str(uuid4()),
        kind=kind,  # type: ignore[arg-type]
        agent_id=agent_id,
        created_at=now,
        updated_at=now,
        evidence_refs=evidence_refs or [],
        patches=patches or [],
        **kwargs,
    )


def _make_observation(page: int = 1) -> VLMObservation:
    return VLMObservation(
        observation_id=str(uuid4()),
        page=page,
        model="test-vlm",
        image_sha256="a" * 64,
        prompt_sha256="b" * 64,
        findings=[],
        created_at="2026-04-25T12:00:00Z",
    )


def _make_patch(agent_id: str = "fixer-1", evidence_refs: list[str] | None = None) -> BookPatch:
    return BookPatch(
        patch_id=str(uuid4()),
        agent_id=agent_id,
        scope=PatchScope(chapter_uid="ch-001"),
        changes=[],
        rationale="test patch",
        evidence_refs=evidence_refs or [],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def editor_paths(tmp_path: Path):
    work = tmp_path / "book"
    work.mkdir(parents=True)
    return resolve_editor_paths(work)


@pytest.fixture
def initialized_paths(tmp_path: Path):
    """Return (paths, book) with fully initialized editor state for submit tests."""
    from epubforge.config import Config
    from epubforge.editor.tool_surface import run_init
    from epubforge.io import load_book

    work = tmp_path / "book"
    work.mkdir(parents=True)
    book = _make_book()
    (work / "05_semantic.json").write_text(
        book.model_dump_json(indent=2), encoding="utf-8"
    )
    run_init(work=work, cfg=Config())
    paths = resolve_editor_paths(work)
    actual_book = load_book(paths.book_path)
    return paths, actual_book


# ---------------------------------------------------------------------------
# Test 1: output with empty evidence_refs -> no errors
# ---------------------------------------------------------------------------


def test_validate_no_evidence_refs(editor_paths) -> None:
    book = _make_book()
    output = _make_agent_output(evidence_refs=[])

    errors = validate_agent_output(output, book, paths=editor_paths)
    assert errors == []


# ---------------------------------------------------------------------------
# Test 2: valid evidence_refs (refs exist in index) -> no errors
# ---------------------------------------------------------------------------


def test_validate_valid_evidence_refs(editor_paths) -> None:
    book = _make_book()
    obs = _make_observation(page=1)
    save_vlm_observation(editor_paths, obs)

    output = _make_agent_output(evidence_refs=[obs.observation_id])

    errors = validate_agent_output(output, book, paths=editor_paths)
    assert errors == []


# ---------------------------------------------------------------------------
# Test 3: invalid evidence_refs (refs do not exist) -> errors
# ---------------------------------------------------------------------------


def test_validate_invalid_evidence_refs(editor_paths) -> None:
    book = _make_book()
    fake_id = str(uuid4())
    output = _make_agent_output(evidence_refs=[fake_id])

    errors = validate_agent_output(output, book, paths=editor_paths)
    assert len(errors) == 1
    assert fake_id in errors[0]


# ---------------------------------------------------------------------------
# Test 4: BookPatch with valid evidence_refs -> no errors
# ---------------------------------------------------------------------------


def test_validate_patch_evidence_refs_valid(editor_paths) -> None:
    book = _make_book()
    obs = _make_observation(page=2)
    save_vlm_observation(editor_paths, obs)

    patch = _make_patch(evidence_refs=[obs.observation_id])
    output = _make_agent_output(patches=[patch])

    errors = validate_agent_output(output, book, paths=editor_paths)
    assert errors == []


# ---------------------------------------------------------------------------
# Test 5: BookPatch with invalid evidence_refs -> errors
# ---------------------------------------------------------------------------


def test_validate_patch_evidence_refs_invalid(editor_paths) -> None:
    book = _make_book()
    fake_id = str(uuid4())

    patch = _make_patch(evidence_refs=[fake_id])
    output = _make_agent_output(patches=[patch])

    errors = validate_agent_output(output, book, paths=editor_paths)
    assert len(errors) == 1
    assert "patches[0]" in errors[0]
    assert fake_id in errors[0]


# ---------------------------------------------------------------------------
# Test 6: validate with paths=None skips evidence_refs check (backward compat)
# ---------------------------------------------------------------------------


def test_validate_without_paths_skips_check() -> None:
    book = _make_book()
    fake_id = str(uuid4())
    output = _make_agent_output(evidence_refs=[fake_id])

    # No paths argument — evidence_refs validation must be skipped
    errors = validate_agent_output(output, book)
    assert errors == []


# ---------------------------------------------------------------------------
# Test 7: submit_agent_output with invalid refs -> errors in result
# ---------------------------------------------------------------------------


def test_submit_rejects_invalid_evidence_refs(initialized_paths) -> None:
    paths, book = initialized_paths
    fake_id = str(uuid4())

    output = _make_agent_output(
        kind="fixer",
        agent_id="fixer-1",
        evidence_refs=[fake_id],
    )
    save_agent_output(paths, output)

    memory = load_editor_memory(paths)
    result = submit_agent_output(
        output=output, book=book, memory=memory, paths=paths, now=_now_ts()
    )

    assert result.submitted is False
    assert any(fake_id in err for err in result.errors)
