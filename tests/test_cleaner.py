"""Unit tests for Stage 3 extract — unit grouping logic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from docling_core.types.doc import DocItemLabel
from docling_core.types.doc.base import BoundingBox

from epubforge.extract import VLMGroupUnit, _AnchorItem, _build_units
from epubforge.ir.book_memory import BookMemory


def _anchor(label: DocItemLabel, t: float, b: float | None = None) -> _AnchorItem:
    return {
        "label": label,
        "text": "",
        "bbox": BoundingBox(l=50.0, t=t, r=300.0, b=b if b is not None else t - 20.0),
    }


def _pages(page_nos: list[int], kind: str = "simple") -> list[dict]:
    return [{"page": p, "kind": kind} for p in page_nos]


class TestBuildUnitsSimple:
    def test_simple_pages_batch_together(self) -> None:
        data = _pages([1, 2, 3])
        units = _build_units(data, {})
        assert len(units) == 1
        assert units[0].pages == [1, 2, 3]

    def test_simple_batch_cap(self) -> None:
        data = _pages(list(range(1, 10)))  # 9 simple pages
        units = _build_units(data, {}, max_vlm_batch=8)
        assert len(units) == 2
        assert units[0].pages == list(range(1, 9))
        assert units[1].pages == [9]

    def test_simple_non_adjacent_splits(self) -> None:
        data = _pages([1, 3])  # gap at page 2
        units = _build_units(data, {})
        assert len(units) == 2

    def test_cross_kind_batches_together_when_adjacent(self) -> None:
        # kind is no longer a split boundary; consecutive pages batch together
        data = [
            {"page": 1, "kind": "simple"},
            {"page": 2, "kind": "complex"},
            {"page": 3, "kind": "simple"},
        ]
        units = _build_units(data, {})
        assert len(units) == 1
        assert units[0].pages == [1, 2, 3]

    def test_all_units_are_vlm_batch(self) -> None:
        data = _pages([1, 2]) + [{"page": 3, "kind": "complex"}]
        units = _build_units(data, {})
        assert all(isinstance(u, VLMGroupUnit) for u in units)
        assert all(u.kind == "vlm_batch" for u in units)

    def test_unit_contract_version_is_3(self) -> None:
        data = _pages([1, 2])
        units = _build_units(data, {})
        assert all(u.contract_version == 3 for u in units)


class TestBuildUnitsComplex:
    def test_complex_adjacent_pages_batch_together(self) -> None:
        # Consecutive complex pages always batch together up to max_vlm_batch
        data = [{"page": 10, "kind": "complex"}, {"page": 11, "kind": "complex"}]
        units = _build_units(data, {})
        assert len(units) == 1
        assert units[0].pages == [10, 11]

    def test_non_adjacent_complex_not_grouped(self) -> None:
        data = [{"page": 10, "kind": "complex"}, {"page": 12, "kind": "complex"}]
        units = _build_units(data, {})
        assert len(units) == 2

    def test_complex_batch_cap(self) -> None:
        n = 15
        data = [{"page": p, "kind": "complex"} for p in range(1, n + 1)]
        units = _build_units(data, {}, max_vlm_batch=4)
        # With max_vlm_batch=4 and 15 pages: 4+4+4+3 = 4 units
        assert units[0].pages == [1, 2, 3, 4]
        assert units[1].pages == [5, 6, 7, 8]
        assert len(units) == 4


class TestTableLikeLabelsDoNotAffectBatching:
    """Table-like labels (TABLE, PICTURE) must not split or affect batching."""

    def test_table_label_does_not_split_batch(self) -> None:
        # Pages with TABLE anchors should still batch together
        data = _pages([1, 2, 3])
        anchors = {
            1: [_anchor(DocItemLabel.TABLE, t=200.0)],
            2: [_anchor(DocItemLabel.TEXT, t=500.0)],
            3: [_anchor(DocItemLabel.PICTURE, t=300.0)],
        }
        units = _build_units(data, anchors)
        assert len(units) == 1
        assert units[0].pages == [1, 2, 3]

    def test_picture_label_does_not_split_batch(self) -> None:
        data = _pages([5, 6])
        anchors = {
            5: [_anchor(DocItemLabel.PICTURE, t=100.0)],
            6: [_anchor(DocItemLabel.TEXT, t=400.0)],
        }
        units = _build_units(data, anchors)
        assert len(units) == 1

    def test_mixed_table_text_picture_all_batch_together(self) -> None:
        data = [
            {"page": 1, "kind": "simple"},
            {"page": 2, "kind": "complex"},
            {"page": 3, "kind": "simple"},
            {"page": 4, "kind": "complex"},
        ]
        anchors = {
            1: [_anchor(DocItemLabel.TEXT, t=700.0), _anchor(DocItemLabel.TABLE, t=200.0)],
            2: [_anchor(DocItemLabel.PICTURE, t=500.0)],
            3: [_anchor(DocItemLabel.FOOTNOTE, t=100.0)],
            4: [_anchor(DocItemLabel.LIST_ITEM, t=300.0)],
        }
        units = _build_units(data, anchors, max_vlm_batch=8)
        assert len(units) == 1
        assert units[0].pages == [1, 2, 3, 4]


class TestPagesGapsSplitBatches:
    """--pages gaps (non-adjacent page numbers) must force new chunks."""

    def test_single_gap_creates_two_units(self) -> None:
        # Pages [1, 3] — gap at page 2
        data = _pages([1, 3])
        units = _build_units(data, {})
        assert len(units) == 2
        assert units[0].pages == [1]
        assert units[1].pages == [3]

    def test_multiple_gaps_each_creates_new_unit(self) -> None:
        # Pages [1, 3, 5] — two gaps
        data = _pages([1, 3, 5])
        units = _build_units(data, {})
        assert len(units) == 3
        assert units[0].pages == [1]
        assert units[1].pages == [3]
        assert units[2].pages == [5]

    def test_gap_after_run_splits(self) -> None:
        # Pages [1, 2, 3, 10, 11] — run then gap
        data = _pages([1, 2, 3, 10, 11])
        units = _build_units(data, {})
        assert len(units) == 2
        assert units[0].pages == [1, 2, 3]
        assert units[1].pages == [10, 11]

    def test_adjacent_pages_with_gap_elsewhere(self) -> None:
        # Pages [5, 6, 7, 20] — three adjacent then gap
        data = _pages([5, 6, 7, 20])
        units = _build_units(data, {})
        assert len(units) == 2
        assert units[0].pages == [5, 6, 7]
        assert units[1].pages == [20]

    def test_no_gap_no_extra_split(self) -> None:
        data = _pages([1, 2, 3, 4])
        units = _build_units(data, {})
        assert len(units) == 1


class TestVLMPromptNoContinuationHints:
    """VLM prompts must not contain pending-tail or continuation hints."""

    def _build_all_messages(
        self,
        pages: list[int],
        page_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Helper: call _process_vlm_unit with a mock client to capture all messages."""
        from epubforge.extract import VLMGroupUnit, _process_vlm_unit

        if page_kinds is None:
            page_kinds = ["simple"] * len(pages)

        unit = VLMGroupUnit(pages=pages, page_kinds=page_kinds)

        captured_messages: list[Any] = []

        def fake_chat_parsed(messages: Any, *, response_format: Any) -> Any:
            captured_messages.extend(messages)
            # Return a minimal valid response
            from epubforge.ir.semantic import VLMGroupOutput, VLMPageOutput
            return VLMGroupOutput(
                pages=[VLMPageOutput(page=p, blocks=[]) for p in pages]
            )

        mock_client = MagicMock()
        mock_client.chat_parsed.side_effect = fake_chat_parsed

        # Mock fitz document: return a tiny pixmap for each page
        mock_fitz = MagicMock()
        mock_page = MagicMock()
        mock_pix = MagicMock()
        mock_pix.tobytes.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 16  # minimal JPEG-like
        mock_page.get_pixmap.return_value = mock_pix
        mock_fitz.__getitem__ = MagicMock(return_value=mock_page)

        _process_vlm_unit(
            unit=unit,
            fitz_doc=mock_fitz,
            anchors={},
            client=mock_client,
            book_memory=None,
            dpi=72,
        )

        assert len(captured_messages) > 0
        return captured_messages  # type: ignore[return-value]

    def _build_prompt_content(
        self,
        pages: list[int],
        page_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Helper: return user message content blocks (message index 1)."""
        messages = self._build_all_messages(pages, page_kinds)
        user_msg = messages[1]  # [0] is system, [1] is user
        return user_msg["content"]  # type: ignore[return-value]

    def test_no_pending_tail_in_single_page_prompt(self) -> None:
        content = self._build_prompt_content(pages=[5])
        text_blocks = [c["text"] for c in content if c.get("type") == "text"]
        full_text = "\n".join(text_blocks)
        assert "PENDING_TAIL" not in full_text
        assert "pending tail" not in full_text.lower()

    def test_no_pending_footnote_in_single_page_prompt(self) -> None:
        content = self._build_prompt_content(pages=[5])
        text_blocks = [c["text"] for c in content if c.get("type") == "text"]
        full_text = "\n".join(text_blocks)
        assert "PENDING_FOOTNOTE" not in full_text
        assert "pending footnote" not in full_text.lower()

    def test_no_continuing_table_hint_in_multi_page_prompt(self) -> None:
        content = self._build_prompt_content(pages=[10, 11])
        text_blocks = [c["text"] for c in content if c.get("type") == "text"]
        full_text = "\n".join(text_blocks)
        assert "may share a continuing table" not in full_text
        assert "share a continuing" not in full_text

    def test_multi_page_prompt_says_do_not_assume_continuation(self) -> None:
        content = self._build_prompt_content(pages=[10, 11])
        text_blocks = [c["text"] for c in content if c.get("type") == "text"]
        full_text = "\n".join(text_blocks)
        # Should say pages are selected adjacent and not to assume continuation
        assert "selected adjacent" in full_text.lower() or "adjacent pages" in full_text.lower()
        assert "do not assume" in full_text.lower() or "not assume" in full_text.lower()

    def test_single_page_prompt_says_judge_from_evidence(self) -> None:
        content = self._build_prompt_content(pages=[7])
        text_blocks = [c["text"] for c in content if c.get("type") == "text"]
        full_text = "\n".join(text_blocks)
        assert "do not assume" in full_text.lower() or "not assume" in full_text.lower()

    def test_system_prompt_no_pending_tail(self) -> None:
        messages = self._build_all_messages(pages=[5])
        sys_content = messages[0]["content"]
        assert "PENDING_TAIL" not in sys_content
        assert "pending tail" not in sys_content.lower()

    def test_system_prompt_no_pending_footnote(self) -> None:
        messages = self._build_all_messages(pages=[5])
        sys_content = messages[0]["content"]
        assert "PENDING_FOOTNOTE" not in sys_content
        assert "pending footnote" not in sys_content.lower()

    def test_system_prompt_no_continuing_table(self) -> None:
        messages = self._build_all_messages(pages=[5])
        sys_content = messages[0]["content"]
        assert "continuing table" not in sys_content.lower()

    def test_system_prompt_no_continuation_flag_fields(self) -> None:
        messages = self._build_all_messages(pages=[5])
        sys_content = messages[0]["content"]
        assert "first_block_continues_prev_tail" not in sys_content
        assert "first_footnote_continues_prev_footnote" not in sys_content


class TestSidecarsAlwaysWritten:
    """Sidecars (audit_notes.json, book_memory.json, evidence_index.json) must always be written."""

    def _run_extract_with_mock(
        self,
        tmp_path: Path,
        pages: list[int],
        enable_book_memory: bool = False,
    ) -> tuple[Any, Path]:
        """Run extract() with all external dependencies mocked."""
        from epubforge.extract import extract
        from epubforge.config import Config, RuntimeSettings, ExtractSettings

        out_dir = tmp_path / "03_extract"
        out_dir.mkdir(parents=True)

        # Create minimal raw.json
        raw_path = tmp_path / "01_raw.json"
        raw_path.write_text(json.dumps({
            "schema_name": "DoclingDocument",
            "version": "1.0.0",
            "name": "test",
            "origin": {"mimetype": "application/pdf", "binary_hash": 0, "filename": "test.pdf"},
            "furniture": {"self_ref": "#/furniture", "children": [], "content_layer": "furniture", "name": "_root_", "label": "unspecified"},
            "body": {"self_ref": "#/body", "children": [], "content_layer": "body", "name": "_root_", "label": "unspecified"},
            "texts": [],
            "pictures": [],
            "tables": [],
            "key_value_items": [],
            "form_items": [],
            "pages": {str(p): {"size": {"width": 595, "height": 842}, "image": None} for p in pages},
        }), encoding="utf-8")

        # Create pages.json
        pages_path = tmp_path / "02_pages.json"
        pages_path.write_text(json.dumps({
            "pages": [{"page": p, "kind": "simple"} for p in pages]
        }), encoding="utf-8")

        # Create minimal PDF
        pdf_path = tmp_path / "source.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")

        cfg = Config(
            runtime=RuntimeSettings(work_dir=tmp_path),
            extract=ExtractSettings(
                max_vlm_batch_pages=4,
                enable_book_memory=enable_book_memory,
                vlm_dpi=72,
            ),
        )

        with (
            patch("epubforge.extract.LLMClient") as mock_llm_cls,
            patch("epubforge.extract.fitz.open") as mock_fitz_open,
            patch("epubforge.extract.DoclingDocument.load_from_json") as mock_doc_load,
        ):
            # Setup mock VLM client
            mock_client = MagicMock()
            mock_llm_cls.return_value = mock_client

            from epubforge.ir.semantic import VLMGroupOutput, VLMPageOutput
            mock_client.chat_parsed.return_value = VLMGroupOutput(
                pages=[VLMPageOutput(page=p, blocks=[]) for p in pages]
            )

            # Setup mock fitz
            mock_fitz_doc = MagicMock()
            mock_fitz_open.return_value.__enter__ = MagicMock(return_value=mock_fitz_doc)
            mock_fitz_open.return_value = mock_fitz_doc
            mock_page = MagicMock()
            mock_pix = MagicMock()
            mock_pix.tobytes.return_value = b"\xff\xd8\xff" + b"\x00" * 32
            mock_page.get_pixmap.return_value = mock_pix
            mock_fitz_doc.__getitem__ = MagicMock(return_value=mock_page)
            mock_fitz_doc.close = MagicMock()

            # Setup mock Docling document
            mock_doc = MagicMock()
            mock_doc.texts = []
            mock_doc.tables = []
            mock_doc.pictures = []
            mock_doc.key_value_items = []
            mock_doc.form_items = []
            mock_doc_load.return_value = mock_doc

            result = extract(
                pdf_path=pdf_path,
                raw_path=raw_path,
                pages_path=pages_path,
                out_dir=out_dir,
                cfg=cfg,
            )

        return result, out_dir

    def test_audit_notes_always_written(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1, 2])
        assert (out_dir / "audit_notes.json").exists()
        data = json.loads((out_dir / "audit_notes.json").read_text())
        assert isinstance(data, list)

    def test_book_memory_always_written(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1, 2])
        assert (out_dir / "book_memory.json").exists()

    def test_evidence_index_always_written(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1, 2])
        assert (out_dir / "evidence_index.json").exists()
        data = json.loads((out_dir / "evidence_index.json").read_text())
        assert data["schema_version"] == 3
        assert data["mode"] == "vlm"
        assert "pages" in data
        assert "refs" in data

    def test_evidence_index_written_when_book_memory_disabled(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(
            tmp_path, pages=[1, 2], enable_book_memory=False
        )
        assert (out_dir / "evidence_index.json").exists()
        assert (out_dir / "audit_notes.json").exists()
        assert (out_dir / "book_memory.json").exists()

    def test_result_is_stage3_extraction_result(self, tmp_path: Path) -> None:
        from epubforge.stage3_artifacts import Stage3ExtractionResult
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1])
        assert isinstance(result, Stage3ExtractionResult)
        assert result.mode == "vlm"

    def test_result_has_correct_paths(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1, 2])
        assert result.audit_notes_path == out_dir / "audit_notes.json"
        assert result.book_memory_path == out_dir / "book_memory.json"
        assert result.evidence_index_path == out_dir / "evidence_index.json"

    def test_unit_files_written(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1, 2, 3])
        # With default max_vlm_batch=4, pages 1,2,3 form a single unit
        assert len(result.unit_files) == 1
        assert result.unit_files[0].exists()

    def test_unit_json_has_vlm_batch_kind(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1])
        unit_data = json.loads(result.unit_files[0].read_text())
        assert unit_data["unit"]["kind"] == "vlm_batch"
        assert unit_data["unit"]["contract_version"] == 3
        assert unit_data["unit"]["extractor"] == "vlm"

    def test_unit_json_no_legacy_continuation_flags(self, tmp_path: Path) -> None:
        result, out_dir = self._run_extract_with_mock(tmp_path, pages=[1])
        unit_data = json.loads(result.unit_files[0].read_text())
        # These fields must not appear in the unit output
        assert "first_block_continues_prev_tail" not in unit_data
        assert "first_footnote_continues_prev_footnote" not in unit_data


class TestBookMemory:
    def test_default_empty(self) -> None:
        m = BookMemory()
        assert m.footnote_callouts == []
        assert m.attribution_templates == []
        assert m.chapter_heading_style is None

    def test_roundtrip_json(self) -> None:
        m = BookMemory(
            footnote_callouts=["①", "②"],
            chapter_heading_style="第N章 标题",
        )
        m2 = BookMemory.model_validate_json(m.model_dump_json())
        assert m2.footnote_callouts == ["①", "②"]
        assert m2.chapter_heading_style == "第N章 标题"
