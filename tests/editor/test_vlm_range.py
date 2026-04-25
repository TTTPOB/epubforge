"""Tests for run_vlm_range (Phase 8C) — range VLM analysis."""

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
# Fixtures / helpers (mirrored from test_vlm_page_rewrite.py)
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _make_book(pages: list[int]) -> Book:
    """Minimal Book IR with one chapter having one block per provided page."""
    blocks = [
        Paragraph(
            uid=f"blk-{page:03d}",
            text=f"Paragraph on page {page}.",
            role="body",
            provenance=_prov(page),
        )
        for page in pages
    ]
    return Book(
        title="Test Book",
        authors=["Test Author"],
        chapters=[
            Chapter(
                uid="ch-001",
                title="Chapter One",
                blocks=blocks,
            )
        ],
    )


def _make_stage3_meta(
    work_dir: Path,
    source_pdf: Path,
    selected_pages: list[int],
) -> Stage3EditorMeta:
    return Stage3EditorMeta(
        mode="docling",
        manifest_path="stage3_manifest.json",
        manifest_sha256="a" * 64,
        artifact_id="art-001",
        selected_pages=selected_pages,
        complex_pages=[],
        source_pdf=str(source_pdf.relative_to(work_dir)),
        evidence_index_path="",
        extraction_warnings_path="",
    )


def _setup_editor_state(
    tmp_path: Path,
    selected_pages: list[int],
) -> tuple:
    """Set up a minimal initialized editor state; return (paths, work_dir, source_pdf)."""
    work_dir = tmp_path / "work" / "testbook"
    work_dir.mkdir(parents=True)

    source_dir = work_dir / "source"
    source_dir.mkdir()
    source_pdf = source_dir / "source.pdf"
    source_pdf.write_bytes(b"%PDF-1.4 dummy")

    raw_book = _make_book(pages=selected_pages)
    book = initialize_book_state(raw_book, initialized_at="2026-04-25T12:00:00Z")

    paths = resolve_editor_paths(work_dir)
    stage3 = _make_stage3_meta(work_dir, source_pdf, selected_pages)

    memory = EditMemory.create(
        book_id=work_dir.name,
        updated_at="2026-04-25T12:00:00Z",
        updated_by="test.setup",
        chapter_uids=["ch-001"],
    )
    write_initial_state(paths, book=book, memory=memory, stage3=stage3)
    save_book(book, work_dir)

    return paths, work_dir, source_pdf


def _make_vlm_page_analysis(page: int = 5) -> VLMPageAnalysis:
    return VLMPageAnalysis(
        page=page,
        findings=[],
        summary="Test summary.",
    )


def _make_mock_llm(page: int = 5) -> MagicMock:
    mock_llm = MagicMock()
    mock_llm.model = "test-vlm-model"
    mock_llm.chat_parsed.return_value = _make_vlm_page_analysis(page=page)
    return mock_llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_vlm_range_basic(tmp_path: Path, capsys) -> None:
    """3 selected pages produce 3 observations and return exit code 0."""
    from epubforge.editor.tool_surface import run_vlm_range
    from epubforge.config import Config

    selected = [3, 5, 7]
    _paths, work_dir, _pdf = _setup_editor_state(tmp_path, selected_pages=selected)
    cfg = MagicMock(spec=Config)

    call_counter = {"n": 0}

    def fake_render(pdf, pg, dpi, out):
        out.write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)

    def fake_chat_parsed(*args, **kwargs):
        # Determine page from the call counter to return correct analysis
        n = call_counter["n"]
        call_counter["n"] += 1
        return _make_vlm_page_analysis(page=selected[n] if n < len(selected) else selected[-1])

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm-model"
    mock_llm.chat_parsed.side_effect = fake_chat_parsed

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image", side_effect=fake_render),
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        exit_code = run_vlm_range(work_dir, start_page=3, end_page=7, dpi=200, cfg=cfg)

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["pages_analyzed"] == 3
    assert len(payload["observation_ids"]) == 3
    assert len(payload["per_page"]) == 3


def test_vlm_range_filters_non_selected(tmp_path: Path, capsys) -> None:
    """Pages not in selected_pages are skipped — only selected pages in range are analyzed."""
    from epubforge.editor.tool_surface import run_vlm_range
    from epubforge.config import Config

    # selected_pages = [5, 7]; range [4, 8] should only analyze pages 5 and 7
    selected = [5, 7]
    _paths, work_dir, _pdf = _setup_editor_state(tmp_path, selected_pages=selected)
    cfg = MagicMock(spec=Config)

    call_counter = {"n": 0}

    def fake_render(pdf, pg, dpi, out):
        out.write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)

    def fake_chat_parsed(*args, **kwargs):
        n = call_counter["n"]
        call_counter["n"] += 1
        return _make_vlm_page_analysis(page=selected[n] if n < len(selected) else selected[-1])

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm-model"
    mock_llm.chat_parsed.side_effect = fake_chat_parsed

    with (
        patch("epubforge.editor.tool_surface._render_pdf_page_image", side_effect=fake_render),
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        exit_code = run_vlm_range(work_dir, start_page=4, end_page=8, dpi=200, cfg=cfg)

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["pages_analyzed"] == 2
    analyzed_pages = [r["page"] for r in payload["per_page"]]
    assert analyzed_pages == [5, 7]


def test_vlm_range_invalid_range(tmp_path: Path) -> None:
    """start_page > end_page raises CommandError."""
    from epubforge.editor.tool_surface import run_vlm_range
    from epubforge.editor.cli_support import CommandError
    from epubforge.config import Config

    _paths, work_dir, _pdf = _setup_editor_state(tmp_path, selected_pages=[5])
    cfg = MagicMock(spec=Config)

    with pytest.raises(CommandError, match="start_page"):
        run_vlm_range(work_dir, start_page=10, end_page=5, dpi=200, cfg=cfg)


def test_vlm_range_no_pages_in_range(tmp_path: Path) -> None:
    """No selected pages in the given range raises CommandError."""
    from epubforge.editor.tool_surface import run_vlm_range
    from epubforge.editor.cli_support import CommandError
    from epubforge.config import Config

    # selected_pages = [5]; range [10, 20] has no selected pages
    _paths, work_dir, _pdf = _setup_editor_state(tmp_path, selected_pages=[5])
    cfg = MagicMock(spec=Config)

    with pytest.raises(CommandError, match="no selected pages in range"):
        run_vlm_range(work_dir, start_page=10, end_page=20, dpi=200, cfg=cfg)


def test_vlm_range_single_page(tmp_path: Path, capsys) -> None:
    """start_page == end_page analyzes exactly one page successfully."""
    from epubforge.editor.tool_surface import run_vlm_range
    from epubforge.config import Config

    selected = [5]
    _paths, work_dir, _pdf = _setup_editor_state(tmp_path, selected_pages=selected)
    cfg = MagicMock(spec=Config)

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm-model"
    mock_llm.chat_parsed.return_value = _make_vlm_page_analysis(page=5)

    with (
        patch(
            "epubforge.editor.tool_surface._render_pdf_page_image",
            side_effect=lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff"),
        ),
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        exit_code = run_vlm_range(work_dir, start_page=5, end_page=5, dpi=200, cfg=cfg)

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["pages_analyzed"] == 1
    assert len(payload["observation_ids"]) == 1
    assert payload["per_page"][0]["page"] == 5


def test_vlm_range_emits_correct_json(tmp_path: Path, capsys) -> None:
    """Emitted JSON has the correct top-level structure."""
    from epubforge.editor.tool_surface import run_vlm_range
    from epubforge.config import Config

    selected = [5, 6]
    _paths, work_dir, _pdf = _setup_editor_state(tmp_path, selected_pages=selected)
    cfg = MagicMock(spec=Config)

    call_counter = {"n": 0}

    def fake_chat_parsed(*args, **kwargs):
        n = call_counter["n"]
        call_counter["n"] += 1
        return _make_vlm_page_analysis(page=selected[n] if n < len(selected) else selected[-1])

    mock_llm = MagicMock()
    mock_llm.model = "test-vlm-model"
    mock_llm.chat_parsed.side_effect = fake_chat_parsed

    with (
        patch(
            "epubforge.editor.tool_surface._render_pdf_page_image",
            side_effect=lambda pdf, pg, dpi, out: out.write_bytes(b"\xff\xd8\xff"),
        ),
        patch("epubforge.llm.client.LLMClient", return_value=mock_llm),
    ):
        run_vlm_range(work_dir, start_page=5, end_page=6, dpi=200, cfg=cfg)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    # Verify top-level keys
    assert "observation_ids" in payload
    assert "pages_analyzed" in payload
    assert "total_findings" in payload
    assert "per_page" in payload

    # Verify per_page entry structure
    assert isinstance(payload["observation_ids"], list)
    assert payload["pages_analyzed"] == 2
    assert isinstance(payload["total_findings"], int)

    for entry in payload["per_page"]:
        assert "observation_id" in entry
        assert "page" in entry
        assert "findings_count" in entry
        assert isinstance(entry["findings_count"], int)
