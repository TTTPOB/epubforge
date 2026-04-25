"""Tests for VLM tool references in prompts._build_extraction_context."""

from __future__ import annotations

from pathlib import Path

import pytest

from epubforge.editor.prompts import _extraction_context_block
from epubforge.editor.state import Stage3EditorMeta
from epubforge.ir.semantic import Chapter, Paragraph, Provenance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="docling")


def _make_chapter(pages: list[int] | None = None) -> Chapter:
    blocks = []
    for p in pages or [1, 2]:
        blocks.append(Paragraph(text=f"Para page {p}.", provenance=_prov(p)))
    return Chapter(title="Test Chapter", blocks=blocks)


def _make_stage3(
    *,
    mode: str = "docling",
    selected_pages: list[int] | None = None,
    complex_pages: list[int] | None = None,
) -> Stage3EditorMeta:
    return Stage3EditorMeta(
        mode=mode,  # type: ignore[arg-type]
        manifest_path="/work/03_extract/artifacts/abc/manifest.json",
        manifest_sha256="abcdef1234567890",
        artifact_id="abc",
        selected_pages=selected_pages or [1, 2],
        complex_pages=complex_pages or [2],
        source_pdf="source/source.pdf",
        evidence_index_path="/work/03_extract/artifacts/abc/evidence_index.json",
        extraction_warnings_path="/work/03_extract/artifacts/abc/warnings.json",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractionContextVLMTools:
    def test_vlm_range_appears_in_output(self, tmp_path: Path) -> None:
        """vlm-range command must appear in the extraction context."""
        chapter = _make_chapter(pages=[3, 4, 5])
        stage3 = _make_stage3(selected_pages=[3, 4, 5])
        result = _extraction_context_block(stage3, chapter, tmp_path)
        assert "vlm-range" in result

    def test_vlm_page_with_chapter_flag_appears(self, tmp_path: Path) -> None:
        """vlm-page command must include --chapter flag."""
        chapter = _make_chapter(pages=[1, 2])
        stage3 = _make_stage3()
        result = _extraction_context_block(stage3, chapter, tmp_path)
        assert "vlm-page" in result
        assert "--chapter" in result

    def test_vlm_observation_reference_appears(self, tmp_path: Path) -> None:
        """Output must mention VLMObservation or observation_id."""
        chapter = _make_chapter(pages=[1, 2])
        stage3 = _make_stage3()
        result = _extraction_context_block(stage3, chapter, tmp_path)
        assert "VLMObservation" in result or "observation_id" in result

    def test_deprecated_field_not_in_output(self, tmp_path: Path) -> None:
        """Removed skipped_vlm field must not appear in the prompt output."""
        chapter = _make_chapter(pages=[1, 2])
        stage3 = _make_stage3()
        result = _extraction_context_block(stage3, chapter, tmp_path)
        assert "skipped_vlm" not in result

    def test_vlm_range_uses_chapter_page_bounds(self, tmp_path: Path) -> None:
        """vlm-range command must use the chapter's first and last pages."""
        chapter = _make_chapter(pages=[7, 8, 9])
        stage3 = _make_stage3(selected_pages=[7, 8, 9], complex_pages=[8])
        result = _extraction_context_block(stage3, chapter, tmp_path)
        assert "--start-page 7" in result
        assert "--end-page 9" in result

    def test_vlm_range_single_page_chapter(self, tmp_path: Path) -> None:
        """Single-page chapter: start-page and end-page must be equal."""
        chapter = _make_chapter(pages=[5])
        stage3 = _make_stage3(selected_pages=[5], complex_pages=[])
        result = _extraction_context_block(stage3, chapter, tmp_path)
        assert "--start-page 5" in result
        assert "--end-page 5" in result

    def test_mode_appears_in_output(self, tmp_path: Path) -> None:
        """Mode field must appear in the output."""
        chapter = _make_chapter()
        stage3 = _make_stage3(mode="docling")
        result = _extraction_context_block(stage3, chapter, tmp_path)
        # mode value should appear
        assert "mode:" in result
        assert "docling" in result


def test_prompts_mention_doctor_tasks():
    """All agent prompts should mention doctor tasks."""
    from epubforge.editor.prompts import SCANNER_PROMPT, FIXER_PROMPT, REVIEWER_PROMPT
    for prompt in (SCANNER_PROMPT, FIXER_PROMPT, REVIEWER_PROMPT):
        assert "doctor" in prompt.lower()
        assert "tasks" in prompt.lower() or "task" in prompt.lower()
