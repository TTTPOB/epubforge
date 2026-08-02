"""Tests for pipeline.run_extract() — reuse, provider gating, activation, and failure modes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from epubforge.config import Config, ExtractSettings, RuntimeSettings
from epubforge.stage3_artifacts import (
    Stage3ExtractionResult,
    Stage3Manifest,
    activate_manifest_atomic,
    build_desired_stage3_manifest,
    load_active_stage3_manifest,
    write_artifact_manifest_atomic,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_cfg(
    tmp_path: Path,
    *,
    api_key: str | None = None,
) -> Config:
    return Config(
        runtime=RuntimeSettings(work_dir=tmp_path / "work"),
        extract=ExtractSettings(),
        llm={"api_key": api_key} if api_key else {},
    )


_PAGES_DATA_SIMPLE: dict[str, Any] = {
    "pages": [
        {"page": 1, "kind": "simple"},
        {"page": 2, "kind": "complex"},
        {"page": 3, "kind": "toc"},
    ]
}

_BASE_DOC_JSON: dict[str, Any] = {
    "schema_name": "DoclingDocument",
    "version": "1.3.0",
    "name": "test",
    "origin": None,
    "furniture": {
        "self_ref": "#/furniture",
        "parent": None,
        "children": [],
        "content_layer": "furniture",
        "name": "_root_",
        "label": "unspecified",
    },
    "body": {
        "self_ref": "#/body",
        "parent": None,
        "children": [],
        "content_layer": "body",
        "name": "_root_",
        "label": "unspecified",
    },
    "groups": [],
    "texts": [],
    "tables": [],
    "pictures": [],
    "key_value_items": [],
    "form_items": [],
    "field_items": [],
    "field_regions": [],
    "pages": {
        "1": {"size": {"width": 612, "height": 792}, "image": None, "page_no": 1},
        "2": {"size": {"width": 612, "height": 792}, "image": None, "page_no": 2},
    },
}


def _setup_work_dir(
    work: Path,
    *,
    pages_data: dict[str, Any] | None = None,
    raw_data: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    """Set up prerequisite files and return (source_pdf, raw, pages_json)."""
    (work / "source").mkdir(parents=True, exist_ok=True)
    source_pdf = work / "source" / "source.pdf"
    source_pdf.write_bytes(b"%PDF-1.7\nfake pdf content\n")

    raw = work / "01_raw.json"
    raw.write_text(
        json.dumps(raw_data or _BASE_DOC_JSON, ensure_ascii=False),
        encoding="utf-8",
    )

    pages_json = work / "02_pages.json"
    pages_json.write_text(
        json.dumps(pages_data or _PAGES_DATA_SIMPLE, ensure_ascii=False),
        encoding="utf-8",
    )
    return source_pdf, raw, pages_json


def _build_desired_id(work: Path) -> str:
    """Compute the expected artifact_id for the standard setup."""
    import hashlib

    source_pdf = work / "source" / "source.pdf"
    raw = work / "01_raw.json"
    pages_json = work / "02_pages.json"

    def sha256(p: Path) -> str:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    settings: dict[str, Any] = {
        "contract_version": 3,
        "enable_book_memory": False,
    }

    return build_desired_stage3_manifest(
        mode="docling",
        source_pdf_rel="source/source.pdf",
        source_pdf_sha256=sha256(source_pdf),
        raw_sha256=sha256(raw),
        pages_sha256=sha256(pages_json),
        selected_pages=[1, 2],  # non-toc pages
        toc_pages=[3],
        complex_pages=[2],
        page_filter=None,
        settings=settings,
    )


def _create_valid_active_artifact(
    work: Path,
    artifact_id: str,
    mode: str = "docling",
) -> None:
    """Create a valid artifact dir + manifest + active pointer for an artifact_id."""
    art_dir = work / "03_extract" / "artifacts" / artifact_id
    art_dir.mkdir(parents=True, exist_ok=True)

    # Create required files
    unit_file = art_dir / "unit_0000.json"
    unit_file.write_text("{}", encoding="utf-8")
    audit_file = art_dir / "audit_notes.json"
    audit_file.write_text("[]", encoding="utf-8")
    book_mem = art_dir / "book_memory.json"
    book_mem.write_text("{}", encoding="utf-8")
    evidence = art_dir / "evidence_index.json"
    evidence.write_text("{}", encoding="utf-8")

    art_dir_rel = f"03_extract/artifacts/{artifact_id}"
    manifest = Stage3Manifest(
        mode=mode,  # type: ignore[arg-type]
        artifact_id=artifact_id,
        artifact_dir=art_dir_rel,
        created_at="2026-04-24T00:00:00Z",
        raw_sha256="aabb",
        pages_sha256="ccdd",
        source_pdf="source/source.pdf",
        source_pdf_sha256="eeff",
        selected_pages=[1, 2],
        toc_pages=[3],
        complex_pages=[2],
        page_filter=None,
        unit_files=[f"{art_dir_rel}/unit_0000.json"],
        sidecars={
            "audit_notes": f"{art_dir_rel}/audit_notes.json",
            "book_memory": f"{art_dir_rel}/book_memory.json",
            "evidence_index": f"{art_dir_rel}/evidence_index.json",
        },
        settings={
            "contract_version": 3,
            "enable_book_memory": False,
        },
    )
    write_artifact_manifest_atomic(work, manifest)
    activate_manifest_atomic(work, manifest)


# ---------------------------------------------------------------------------
# Test 1: Reusable artifact bypasses provider validation
# ---------------------------------------------------------------------------


class TestReuseActiveArtifact:
    def test_reuse_bypasses_extractor_and_provider_validation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the active artifact matches desired, the extractor is not called."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        desired_id = _build_desired_id(work)
        _create_valid_active_artifact(work, desired_id, mode="docling")

        extract_calls: list[str] = []

        def fake_extract_skip_vlm(*args: Any, **kwargs: Any) -> None:
            extract_calls.append("extract_skip_vlm")

        def fake_require_llm(self: Any) -> None:
            raise AssertionError("require_llm should NOT be called on reuse")

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fake_extract_skip_vlm
        )
        monkeypatch.setattr(Config, "require_llm", fake_require_llm)

        from epubforge.pipeline import run_extract

        caplog.set_level(logging.INFO, logger="epubforge.pipeline")
        run_extract(tmp_path / "book.pdf", cfg)

        # Extractor must NOT have been called
        assert extract_calls == []
        # Log must mention reuse
        assert "reusing active artifact" in caplog.text

    def test_reuse_logs_provider_required_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        desired_id = _build_desired_id(work)
        _create_valid_active_artifact(work, desired_id, mode="docling")

        from epubforge.pipeline import run_extract

        caplog.set_level(logging.INFO, logger="epubforge.pipeline")
        run_extract(tmp_path / "book.pdf", cfg)

        assert "provider_required=False" in caplog.text

    def test_force_true_ignores_reusable_artifact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """force=True should bypass reuse even if the active artifact matches."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        desired_id = _build_desired_id(work)
        _create_valid_active_artifact(work, desired_id, mode="docling")

        extract_calls: list[str] = []

        def fake_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> Stage3ExtractionResult:
            extract_calls.append("called")
            # Return a minimal result
            unit = out_dir / "unit_0000.json"
            unit.write_text("{}", encoding="utf-8")
            audit = out_dir / "audit_notes.json"
            audit.write_text("[]", encoding="utf-8")
            bm = out_dir / "book_memory.json"
            bm.write_text("{}", encoding="utf-8")
            ei = out_dir / "evidence_index.json"
            ei.write_text("{}", encoding="utf-8")
            return Stage3ExtractionResult(
                mode="docling",
                unit_files=[unit],
                audit_notes_path=audit,
                book_memory_path=bm,
                evidence_index_path=ei,
                selected_pages=[1, 2],
                toc_pages=[3],
                complex_pages=[2],
            )

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fake_extract_skip_vlm
        )

        from epubforge.pipeline import run_extract

        run_extract(tmp_path / "book.pdf", cfg, force=True)
        assert extract_calls == ["called"]


# ---------------------------------------------------------------------------
# Test 2: Pipeline extraction behavior
# ---------------------------------------------------------------------------


class TestNoProviderRequired:
    def test_extraction_logs_provider_required_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """run_extract must log provider_required=False."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        def fake_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> Stage3ExtractionResult:
            unit = out_dir / "unit_0000.json"
            unit.write_text("{}", encoding="utf-8")
            audit = out_dir / "audit_notes.json"
            audit.write_text("[]", encoding="utf-8")
            bm = out_dir / "book_memory.json"
            bm.write_text("{}", encoding="utf-8")
            ei = out_dir / "evidence_index.json"
            ei.write_text("{}", encoding="utf-8")
            return Stage3ExtractionResult(
                mode="docling",
                unit_files=[unit],
                audit_notes_path=audit,
                book_memory_path=bm,
                evidence_index_path=ei,
                selected_pages=[1, 2],
                toc_pages=[3],
                complex_pages=[2],
            )

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fake_extract_skip_vlm
        )

        from epubforge.pipeline import run_extract

        caplog.set_level(logging.INFO, logger="epubforge.pipeline")
        run_extract(tmp_path / "book.pdf", cfg)

        assert "provider_required=False" in caplog.text


# ---------------------------------------------------------------------------
# Test 4: Failed extraction preserves old active pointer
# ---------------------------------------------------------------------------


class TestFailedExtractionPreservesOldPointer:
    def test_extractor_failure_leaves_old_pointer_intact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the extractor raises, the old active_manifest.json must not be replaced."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        # Set up old active artifact with a different (wrong) desired_id
        old_artifact_id = "aaaa0000aaaa0000"
        _create_valid_active_artifact(work, old_artifact_id, mode="docling")

        # Verify old pointer is active
        pointer, _ = load_active_stage3_manifest(work)
        assert pointer.active_artifact_id == old_artifact_id

        def exploding_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> None:
            raise RuntimeError("Simulated extraction failure")

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm",
            exploding_extract_skip_vlm,
        )

        from epubforge.pipeline import run_extract

        with pytest.raises(RuntimeError, match="Simulated extraction failure"):
            run_extract(tmp_path / "book.pdf", cfg)

        # Old pointer must still be active
        pointer_after, _ = load_active_stage3_manifest(work)
        assert pointer_after.active_artifact_id == old_artifact_id

    def test_manifest_not_written_on_extractor_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """manifest.json for new artifact must NOT exist if extraction failed."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        desired_id = _build_desired_id(work)

        def exploding_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> None:
            raise RuntimeError("Extraction bombed")

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm",
            exploding_extract_skip_vlm,
        )

        from epubforge.pipeline import run_extract

        with pytest.raises(RuntimeError):
            run_extract(tmp_path / "book.pdf", cfg)

        new_manifest = work / "03_extract" / "artifacts" / desired_id / "manifest.json"
        assert not new_manifest.exists()


# ---------------------------------------------------------------------------
# Test 5: reuse_only=True with mismatched artifact fails clearly
# ---------------------------------------------------------------------------


class TestReuseOnlyMismatch:
    def test_reuse_only_no_active_artifact_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """reuse_only=True with no active artifact must raise RuntimeError."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        from epubforge.pipeline import run_extract

        with pytest.raises(RuntimeError, match="no valid active artifact"):
            run_extract(tmp_path / "book.pdf", cfg, reuse_only=True)

    def test_reuse_only_different_mode_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """reuse_only=True when active artifact is mismatched must fail."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        old_artifact_id = "aaaa1111aaaa1111"
        _create_valid_active_artifact(work, old_artifact_id, mode="docling")

        from epubforge.pipeline import run_extract

        with pytest.raises(RuntimeError, match="no valid active artifact"):
            run_extract(tmp_path / "book.pdf", cfg, reuse_only=True)

    def test_reuse_only_error_message_is_helpful(
        self,
        tmp_path: Path,
    ) -> None:
        """The error message must include guidance for the user."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        from epubforge.pipeline import run_extract

        with pytest.raises(RuntimeError) as exc_info:
            run_extract(tmp_path / "book.pdf", cfg, reuse_only=True)

        msg = str(exc_info.value)
        # Must mention how to fix
        assert "extract" in msg.lower() or "--from 3" in msg

    def test_reuse_only_matching_artifact_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reuse_only=True with a matching active artifact must succeed silently."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        desired_id = _build_desired_id(work)
        _create_valid_active_artifact(work, desired_id, mode="docling")

        def fail_if_called(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("Extractor should NOT be called in reuse_only mode")

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fail_if_called
        )

        from epubforge.pipeline import run_extract

        # Should not raise
        run_extract(tmp_path / "book.pdf", cfg, reuse_only=True)


# ---------------------------------------------------------------------------
# Test 6: Manifest is written and activated on successful extraction
# ---------------------------------------------------------------------------


class TestManifestActivationAfterExtraction:
    def test_active_manifest_written_after_docling_extraction(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        def fake_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> Stage3ExtractionResult:
            unit = out_dir / "unit_0000.json"
            unit.write_text("{}", encoding="utf-8")
            audit = out_dir / "audit_notes.json"
            audit.write_text("[]", encoding="utf-8")
            bm = out_dir / "book_memory.json"
            bm.write_text("{}", encoding="utf-8")
            ei = out_dir / "evidence_index.json"
            ei.write_text("{}", encoding="utf-8")
            return Stage3ExtractionResult(
                mode="docling",
                unit_files=[unit],
                audit_notes_path=audit,
                book_memory_path=bm,
                evidence_index_path=ei,
                selected_pages=[1, 2],
                toc_pages=[3],
                complex_pages=[2],
            )

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fake_extract_skip_vlm
        )

        from epubforge.pipeline import run_extract

        run_extract(tmp_path / "book.pdf", cfg)

        # active_manifest.json must exist and be readable
        pointer_path = work / "03_extract" / "active_manifest.json"
        assert pointer_path.exists()

        pointer, manifest = load_active_stage3_manifest(work)
        assert manifest.mode == "docling"
        assert len(manifest.artifact_id) == 16

    def test_active_manifest_log_includes_activated_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        def fake_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> Stage3ExtractionResult:
            unit = out_dir / "unit_0000.json"
            unit.write_text("{}", encoding="utf-8")
            audit = out_dir / "audit_notes.json"
            audit.write_text("[]", encoding="utf-8")
            bm = out_dir / "book_memory.json"
            bm.write_text("{}", encoding="utf-8")
            ei = out_dir / "evidence_index.json"
            ei.write_text("{}", encoding="utf-8")
            return Stage3ExtractionResult(
                mode="docling",
                unit_files=[unit],
                audit_notes_path=audit,
                book_memory_path=bm,
                evidence_index_path=ei,
                selected_pages=[1, 2],
                toc_pages=[3],
                complex_pages=[2],
            )

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fake_extract_skip_vlm
        )

        from epubforge.pipeline import run_extract

        caplog.set_level(logging.INFO, logger="epubforge.pipeline")
        run_extract(tmp_path / "book.pdf", cfg)

        assert "activated artifact_id=" in caplog.text


# ---------------------------------------------------------------------------
# Test 7: run_all forwards five-stage force semantics
# ---------------------------------------------------------------------------


class TestRunAllFiveStages:
    @pytest.mark.parametrize("from_stage", range(1, 6))
    @pytest.mark.parametrize("force", [False, True])
    def test_run_all_from_force_matrix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        from_stage: int,
        force: bool,
    ) -> None:
        """run_all applies --force-rerun from the selected stage onward."""
        cfg = _make_cfg(tmp_path)
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")

        stage_calls: list[tuple[str, bool]] = []
        for name in ("parse", "normalize", "segment", "prepare", "revise"):
            monkeypatch.setattr(
                "epubforge.pipeline.run_" + name,
                lambda *args, _name=name, **kwargs: stage_calls.append(
                    (_name, kwargs["force"])
                ),
            )

        from epubforge.pipeline import run_all

        run_all(pdf, cfg, force=force, from_stage=from_stage)

        assert stage_calls == [
            (name, force and index >= from_stage)
            for index, name in enumerate(
                ("parse", "normalize", "segment", "prepare", "revise"),
                start=1,
            )
        ]

    def test_run_all_rejects_invalid_from_stage(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Direct callers receive the same five-stage range contract as CLI."""
        cfg = _make_cfg(tmp_path)
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")

        from epubforge.pipeline import run_all

        with pytest.raises(ValueError, match="between 1 and 5"):
            run_all(pdf, cfg, from_stage=6)


# ---------------------------------------------------------------------------
# Test 8: Pages filter affects artifact_id
# ---------------------------------------------------------------------------


class TestPagesFilter:
    def test_pages_filter_changes_artifact_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Passing pages={1} vs pages=None produces different artifact_ids."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        artifact_ids_seen: list[str] = []

        def fake_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> Stage3ExtractionResult:
            artifact_ids_seen.append(out_dir.name)
            unit = out_dir / "unit_0000.json"
            unit.write_text("{}", encoding="utf-8")
            audit = out_dir / "audit_notes.json"
            audit.write_text("[]", encoding="utf-8")
            bm = out_dir / "book_memory.json"
            bm.write_text("{}", encoding="utf-8")
            ei = out_dir / "evidence_index.json"
            ei.write_text("{}", encoding="utf-8")
            pages_out = [p for p in [1, 2] if page_filter is None or p in page_filter]
            return Stage3ExtractionResult(
                mode="docling",
                unit_files=[unit],
                audit_notes_path=audit,
                book_memory_path=bm,
                evidence_index_path=ei,
                selected_pages=pages_out,
                toc_pages=[3],
                complex_pages=[
                    p for p in [2] if page_filter is None or p in page_filter
                ],
            )

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fake_extract_skip_vlm
        )

        from epubforge.pipeline import run_extract

        # No filter
        run_extract(tmp_path / "book.pdf", cfg)
        # With filter
        run_extract(tmp_path / "book.pdf", cfg, force=True, pages={1})

        assert len(artifact_ids_seen) == 2
        assert artifact_ids_seen[0] != artifact_ids_seen[1]

    def test_manifest_page_lists_are_filtered_when_pages_used(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When --pages filter is applied, manifest.selected_pages, toc_pages,
        and complex_pages must only contain pages within the filter set."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        # Set up pages data with pages 1 (simple), 2 (complex), 3 (toc)
        _setup_work_dir(work)

        def fake_extract_skip_vlm(
            raw_path: Path,
            pages_path: Path,
            out_dir: Path,
            *,
            force: bool = False,
            page_filter: Any = None,
            **kwargs: Any,
        ) -> Stage3ExtractionResult:
            unit = out_dir / "unit_0000.json"
            unit.write_text("{}", encoding="utf-8")
            audit = out_dir / "audit_notes.json"
            audit.write_text("[]", encoding="utf-8")
            bm = out_dir / "book_memory.json"
            bm.write_text("{}", encoding="utf-8")
            ei = out_dir / "evidence_index.json"
            ei.write_text("{}", encoding="utf-8")
            # Extractor returns ALL pages (unfiltered) — pipeline must filter them
            return Stage3ExtractionResult(
                mode="docling",
                unit_files=[unit],
                audit_notes_path=audit,
                book_memory_path=bm,
                evidence_index_path=ei,
                selected_pages=[1, 2],
                toc_pages=[3],
                complex_pages=[2],
            )

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm", fake_extract_skip_vlm
        )

        from epubforge.pipeline import run_extract

        # Filter to only page 1 (simple); pages 2 (complex) and 3 (toc) should be excluded
        run_extract(tmp_path / "book.pdf", cfg, pages={1})

        _, manifest = load_active_stage3_manifest(work)

        assert manifest.selected_pages == [1], (
            f"expected [1], got {manifest.selected_pages}"
        )
        assert manifest.toc_pages == [], f"expected [], got {manifest.toc_pages}"
        assert manifest.complex_pages == [], (
            f"expected [], got {manifest.complex_pages}"
        )
        # page_filter itself must be recorded
        assert manifest.page_filter == [1]
