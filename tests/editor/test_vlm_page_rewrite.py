"""Tests for the rewritten run_vlm_page / _run_vlm_page_core (Phase 8B)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from epubforge.editor.vlm_evidence import VLMFinding, VLMPageAnalysis, VLMObservation
from epubforge.editor.state import (
    Stage3EditorMeta,
    initialize_book_state,
    resolve_editor_paths,
    write_initial_state,
)
from epubforge.editor.memory import EditMemory
from epubforge.io import save_book
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _make_book(page: int = 5) -> Book:
    """Minimal Book IR with two chapters, each having a block on *page*."""
    return Book(
        title="Test Book",
        authors=["Test Author"],
        chapters=[
            Chapter(
                uid="ch-001",
                title="Chapter One",
                blocks=[
                    Paragraph(
                        uid="blk-001",
                        text="Paragraph one on the page.",
                        role="body",
                        provenance=_prov(page),
                    ),
                    Paragraph(
                        uid="blk-002",
                        text="Paragraph two on the page.",
                        role="body",
                        provenance=_prov(page),
                    ),
                ],
            ),
            Chapter(
                uid="ch-002",
                title="Chapter Two",
                blocks=[
                    Paragraph(
                        uid="blk-003",
                        text="Chapter two paragraph.",
                        role="body",
                        provenance=_prov(page),
                    ),
                ],
            ),
        ],
    )


def _make_stage3_meta(work_dir: Path, source_pdf: Path) -> Stage3EditorMeta:
    """Minimal Stage3EditorMeta for testing."""
    return Stage3EditorMeta(
        mode="docling",
        manifest_path="stage3_manifest.json",
        manifest_sha256="a" * 64,
        artifact_id="art-001",
        selected_pages=[5],
        complex_pages=[],
        source_pdf=str(source_pdf.relative_to(work_dir)),
        evidence_index_path="",
        extraction_warnings_path="",
    )


def _setup_editor_state(tmp_path: Path, page: int = 5):
    """Set up a minimal initialized editor state; return (paths, work_dir, source_pdf)."""
    work_dir = tmp_path / "work" / "testbook"
    work_dir.mkdir(parents=True)

    # Create a dummy source PDF placeholder (just needs to exist for path checks)
    source_dir = work_dir / "source"
    source_dir.mkdir()
    source_pdf = source_dir / "source.pdf"
    source_pdf.write_bytes(b"%PDF-1.4 dummy")

    raw_book = _make_book(page=page)
    book = initialize_book_state(raw_book, initialized_at="2026-04-25T12:00:00Z")

    paths = resolve_editor_paths(work_dir)
    stage3 = _make_stage3_meta(work_dir, source_pdf)

    memory = EditMemory.create(
        book_id=work_dir.name,
        updated_at="2026-04-25T12:00:00Z",
        updated_by="test.setup",
        chapter_uids=["ch-001", "ch-002"],
    )
    write_initial_state(paths, book=book, memory=memory, stage3=stage3)
    # save_book(book, path) — path can be work_dir (resolves to edit_state/book.json)
    save_book(book, work_dir)

    return paths, work_dir, source_pdf


def _make_vlm_page_analysis(
    page: int = 5,
    findings: list[VLMFinding] | None = None,
) -> VLMPageAnalysis:
    return VLMPageAnalysis(
        page=page,
        findings=findings or [],
        summary="Test summary.",
    )


def _make_finding(
    finding_type: str = "text_mismatch",
    severity: str = "warning",
    block_uids: list[str] | None = None,
    description: str = "Test finding.",
) -> VLMFinding:
    return VLMFinding(
        finding_type=finding_type,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        block_uids=block_uids or [],
        description=description,
    )


# ---------------------------------------------------------------------------
# Tests: _run_vlm_page_core returns VLMObservation with correct fields
# ---------------------------------------------------------------------------


def test_core_returns_vlm_observation(tmp_path: Path) -> None:
    """_run_vlm_page_core returns a VLMObservation with correct structural fields."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    analysis = _make_vlm_page_analysis(page=5, findings=[_make_finding()])

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm-model"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        obs, _warn = _run_vlm_page_core(
            paths=paths,
            page=5,
            dpi=200,
            cfg=cfg,
        )

    assert isinstance(obs, VLMObservation)
    assert obs.page == 5
    assert obs.dpi == 200
    assert obs.model == "test-vlm-model"
    assert len(obs.image_sha256) == 64
    assert len(obs.prompt_sha256) == 64
    assert obs.chapter_uid is None
    assert len(obs.findings) == 1
    assert obs.observation_id  # non-empty UUID


def test_core_observation_saved_to_index(tmp_path: Path) -> None:
    """_run_vlm_page_core saves the observation and updates the index."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.editor.vlm_evidence import load_vlm_observation_index
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    analysis = _make_vlm_page_analysis(page=5)
    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
        obs, _warn = _run_vlm_page_core(paths=paths, page=5, dpi=200, cfg=cfg)

    index = load_vlm_observation_index(paths)
    assert obs.observation_id in index.entries
    entry = index.entries[obs.observation_id]
    assert entry.page == 5
    assert entry.model == "test-vlm"


# ---------------------------------------------------------------------------
# Tests: hallucinated block_uid filtering
# ---------------------------------------------------------------------------


def test_hallucinated_block_uids_filtered(tmp_path: Path) -> None:
    """Finding block_uids that don't exist in scope_blocks are removed."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    # Findings include both real and hallucinated UIDs
    findings = [
        _make_finding(
            block_uids=["blk-001", "hallucinated-uid-999"],
            description="Mixed real and hallucinated.",
        ),
        _make_finding(
            block_uids=["blk-002"],
            description="All real.",
        ),
        _make_finding(
            block_uids=["ghost-uid"],
            description="All hallucinated.",
        ),
    ]
    analysis = _make_vlm_page_analysis(page=5, findings=findings)

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff")
        obs, _warn = _run_vlm_page_core(paths=paths, page=5, dpi=200, cfg=cfg)

    assert len(obs.findings) == 3
    # First finding: only real UID kept
    assert obs.findings[0].block_uids == ["blk-001"]
    # Second finding: real UID kept
    assert obs.findings[1].block_uids == ["blk-002"]
    # Third finding: all hallucinated, so empty list
    assert obs.findings[2].block_uids == []


def test_filtering_with_no_block_uids(tmp_path: Path) -> None:
    """Findings with empty block_uids remain unaffected by filtering."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    findings = [_make_finding(block_uids=[], description="No blocks, missing content.")]
    analysis = _make_vlm_page_analysis(page=5, findings=findings)

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff")
        obs, _warn = _run_vlm_page_core(paths=paths, page=5, dpi=200, cfg=cfg)

    assert obs.findings[0].block_uids == []


# ---------------------------------------------------------------------------
# Tests: run_vlm_page wrapper emits JSON with observation_id
# ---------------------------------------------------------------------------


def test_run_vlm_page_emits_observation_id(tmp_path: Path, capsys) -> None:
    """run_vlm_page emits JSON containing observation_id field."""
    from epubforge.editor.tool_surface import run_vlm_page
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    analysis = _make_vlm_page_analysis(page=5, findings=[_make_finding()])
    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff")
        exit_code = run_vlm_page(work_dir, 5, 200, None, cfg)

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "observation_id" in payload
    assert payload["page"] == 5
    assert "findings_count" in payload
    assert "model" in payload


def test_run_vlm_page_writes_legacy_out(tmp_path: Path, capsys) -> None:
    """When out= is provided, run_vlm_page writes a legacy JSON file at that path."""
    from epubforge.editor.tool_surface import run_vlm_page
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)
    out_file = tmp_path / "legacy_output.json"

    analysis = _make_vlm_page_analysis(page=5)
    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff")
        run_vlm_page(work_dir, 5, 200, out_file, cfg)

    assert out_file.exists()
    legacy = json.loads(out_file.read_text(encoding="utf-8"))
    assert "observation_id" in legacy
    assert legacy["page"] == 5


def test_run_vlm_page_backward_compat_signature(tmp_path: Path, capsys) -> None:
    """Old positional/keyword signature run_vlm_page(work, page, dpi, out, cfg) still works."""
    from epubforge.editor.tool_surface import run_vlm_page
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    analysis = _make_vlm_page_analysis(page=5)
    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff")
        # Call with old positional signature (no chapter/blocks)
        result = run_vlm_page(work_dir, 5, 200, None, cfg)

    assert result == 0


# ---------------------------------------------------------------------------
# Tests: --chapter scoping
# ---------------------------------------------------------------------------


def test_chapter_scoping_limits_blocks(tmp_path: Path) -> None:
    """When chapter= is provided, only blocks from that chapter are in scope."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    # Finding references blk-003 which is in ch-002, not ch-001
    # When scoped to ch-001, blk-003 should be treated as hallucinated
    findings = [
        _make_finding(
            block_uids=["blk-001", "blk-003"],  # blk-003 is in ch-002
            description="Cross-chapter ref.",
        ),
    ]
    analysis = _make_vlm_page_analysis(page=5, findings=findings)

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff")
        obs, _warn = _run_vlm_page_core(
            paths=paths,
            page=5,
            dpi=200,
            cfg=cfg,
            chapter="ch-001",
        )

    assert obs.chapter_uid == "ch-001"
    # blk-001 is in ch-001; blk-003 is in ch-002 and should be filtered out
    assert obs.findings[0].block_uids == ["blk-001"]
    # scope is limited to ch-001 blocks
    assert "blk-001" in obs.related_block_uids
    assert "blk-002" in obs.related_block_uids
    assert "blk-003" not in obs.related_block_uids


def test_chapter_scoping_invalid_chapter_raises(tmp_path: Path) -> None:
    """chapter= with a non-existent chapter UID raises CommandError."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.editor.cli_support import CommandError
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    with pytest.raises(CommandError, match="chapter not found"):
        _run_vlm_page_core(
            paths=paths,
            page=5,
            dpi=200,
            cfg=cfg,
            chapter="ch-nonexistent",
        )


# ---------------------------------------------------------------------------
# Tests: --blocks scoping
# ---------------------------------------------------------------------------


def test_blocks_scoping_limits_to_given_uids(tmp_path: Path) -> None:
    """When blocks= is provided, only those block UIDs are in scope."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    # Scope to blk-001 only; blk-002 should be out of scope
    findings = [
        _make_finding(
            block_uids=["blk-001", "blk-002"],
            description="Both blocks mentioned.",
        ),
    ]
    analysis = _make_vlm_page_analysis(page=5, findings=findings)

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm"
    mock_llm.chat_parsed.return_value = analysis

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image") as mock_render,
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        mock_render.side_effect = lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff")
        obs, _warn = _run_vlm_page_core(
            paths=paths,
            page=5,
            dpi=200,
            cfg=cfg,
            blocks=["blk-001"],
        )

    # Only blk-001 is in scope; blk-002 filtered as "hallucinated"
    assert obs.findings[0].block_uids == ["blk-001"]
    assert obs.related_block_uids == ["blk-001"]


def test_blocks_scoping_invalid_uid_raises(tmp_path: Path) -> None:
    """blocks= with a non-existent block UID raises CommandError."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.editor.cli_support import CommandError
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    with pytest.raises(CommandError, match="block UIDs not found"):
        _run_vlm_page_core(
            paths=paths,
            page=5,
            dpi=200,
            cfg=cfg,
            blocks=["blk-001", "nonexistent-uid"],
        )


# ---------------------------------------------------------------------------
# Tests: invalid page raises CommandError
# ---------------------------------------------------------------------------


def test_invalid_page_not_in_selected_pages(tmp_path: Path) -> None:
    """A page not in selected_pages raises CommandError."""
    from epubforge.editor.tool_surface import _run_vlm_page_core
    from epubforge.editor.cli_support import CommandError
    from epubforge.config import Config

    paths, work_dir, source_pdf = _setup_editor_state(tmp_path, page=5)
    cfg = MagicMock(spec=Config)

    with pytest.raises(CommandError, match="not in selected pages"):
        _run_vlm_page_core(
            paths=paths,
            page=99,  # not a selected page
            dpi=200,
            cfg=cfg,
        )
