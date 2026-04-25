"""Tests for Phase 9A pipeline simplification — docling-only extraction.

Verifies:
1. _settings_for_artifact always returns docling settings regardless of cfg.extract.skip_vlm
2. run_extract always calls extract_skip_vlm (never the VLM extractor)
3. mode="docling" in result and activated manifest
4. --skip-vlm CLI option is accepted but its value is ignored
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from epubforge.config import Config, ExtractSettings, RuntimeSettings
from epubforge.stage3_artifacts import (
    Stage3ExtractionResult,
    load_active_stage3_manifest,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_cfg(
    tmp_path: Path,
    *,
    skip_vlm: bool = False,
) -> Config:
    return Config(
        runtime=RuntimeSettings(work_dir=tmp_path / "work"),
        extract=ExtractSettings(skip_vlm=skip_vlm),
    )


_PAGES_DATA: dict[str, Any] = {
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


def _setup_work_dir(work: Path) -> None:
    (work / "source").mkdir(parents=True, exist_ok=True)
    (work / "source" / "source.pdf").write_bytes(b"%PDF-1.7\nfake pdf content\n")
    (work / "01_raw.json").write_text(
        json.dumps(_BASE_DOC_JSON, ensure_ascii=False), encoding="utf-8"
    )
    (work / "02_pages.json").write_text(
        json.dumps(_PAGES_DATA, ensure_ascii=False), encoding="utf-8"
    )


def _fake_extract_skip_vlm(
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    *,
    force: bool = False,
    page_filter: Any = None,
    **kwargs: Any,
) -> Stage3ExtractionResult:
    """Minimal fake extractor that writes required sidecar files."""
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


# ---------------------------------------------------------------------------
# 1. _settings_for_artifact — always docling settings
# ---------------------------------------------------------------------------


class TestSettingsForArtifact:
    """_settings_for_artifact must return fixed docling settings regardless of config."""

    def test_returns_docling_settings_when_skip_vlm_false(
        self, tmp_path: Path
    ) -> None:
        """skip_vlm=False must still produce docling settings."""
        cfg = _make_cfg(tmp_path, skip_vlm=False)
        from epubforge.pipeline import _settings_for_artifact

        settings = _settings_for_artifact(cfg)

        assert settings["skip_vlm"] is True
        assert settings["vlm_dpi"] is None
        assert settings["max_vlm_batch_pages"] is None
        assert settings["enable_book_memory"] is False
        assert settings["vlm_model"] is None
        assert settings["vlm_base_url"] is None
        assert settings["contract_version"] == 3

    def test_returns_docling_settings_when_skip_vlm_true(
        self, tmp_path: Path
    ) -> None:
        """skip_vlm=True must also produce docling settings (identical output)."""
        cfg = _make_cfg(tmp_path, skip_vlm=True)
        from epubforge.pipeline import _settings_for_artifact

        settings = _settings_for_artifact(cfg)

        assert settings["skip_vlm"] is True
        assert settings["vlm_dpi"] is None
        assert settings["max_vlm_batch_pages"] is None
        assert settings["enable_book_memory"] is False
        assert settings["vlm_model"] is None
        assert settings["vlm_base_url"] is None

    def test_settings_identical_regardless_of_skip_vlm_flag(
        self, tmp_path: Path
    ) -> None:
        """Config.extract.skip_vlm has no effect on settings output."""
        cfg_true = _make_cfg(tmp_path, skip_vlm=True)
        cfg_false = _make_cfg(tmp_path, skip_vlm=False)
        from epubforge.pipeline import _settings_for_artifact

        assert _settings_for_artifact(cfg_true) == _settings_for_artifact(cfg_false)


# ---------------------------------------------------------------------------
# 2. run_extract always calls extract_skip_vlm
# ---------------------------------------------------------------------------


class TestRunExtractAlwaysDocling:
    def test_always_calls_extract_skip_vlm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_extract must always use extract_skip_vlm, never epubforge.extract.extract."""
        cfg = _make_cfg(tmp_path, skip_vlm=False)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        calls: list[str] = []

        def recording_extract_skip_vlm(*args: Any, **kwargs: Any) -> Stage3ExtractionResult:
            calls.append("extract_skip_vlm")
            return _fake_extract_skip_vlm(*args, **kwargs)

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm",
            recording_extract_skip_vlm,
        )

        from epubforge.pipeline import run_extract

        run_extract(tmp_path / "book.pdf", cfg)

        assert calls == ["extract_skip_vlm"]

    def test_skip_vlm_true_also_calls_extract_skip_vlm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """skip_vlm=True must also use extract_skip_vlm (same path)."""
        cfg = _make_cfg(tmp_path, skip_vlm=True)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        calls: list[str] = []

        def recording_extract_skip_vlm(*args: Any, **kwargs: Any) -> Stage3ExtractionResult:
            calls.append("extract_skip_vlm")
            return _fake_extract_skip_vlm(*args, **kwargs)

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm",
            recording_extract_skip_vlm,
        )

        from epubforge.pipeline import run_extract

        run_extract(tmp_path / "book.pdf", cfg)

        assert calls == ["extract_skip_vlm"]

    def test_result_mode_is_docling(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The activated manifest must record mode='docling'."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm",
            _fake_extract_skip_vlm,
        )

        from epubforge.pipeline import run_extract

        run_extract(tmp_path / "book.pdf", cfg)

        _, manifest = load_active_stage3_manifest(work)
        assert manifest.mode == "docling"

    def test_logs_docling_evidence_draft(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Log must mention 'Docling evidence draft' (not 'skip-VLM')."""
        cfg = _make_cfg(tmp_path)
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm",
            _fake_extract_skip_vlm,
        )

        from epubforge.pipeline import run_extract

        caplog.set_level(logging.INFO, logger="epubforge.pipeline")
        run_extract(tmp_path / "book.pdf", cfg)

        assert "Docling evidence draft" in caplog.text

    def test_provider_required_false_always_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """provider_required=False must be logged unconditionally."""
        cfg = _make_cfg(tmp_path, skip_vlm=False)  # even when skip_vlm=False
        work = cfg.book_work_dir(tmp_path / "book.pdf")
        _setup_work_dir(work)

        monkeypatch.setattr(
            "epubforge.extract_skip_vlm.extract_skip_vlm",
            _fake_extract_skip_vlm,
        )

        from epubforge.pipeline import run_extract

        caplog.set_level(logging.INFO, logger="epubforge.pipeline")
        run_extract(tmp_path / "book.pdf", cfg)

        assert "provider_required=False" in caplog.text


# ---------------------------------------------------------------------------
# 3. Backward compat: old artifacts with mode="vlm" or "skip_vlm" still load
# ---------------------------------------------------------------------------


class TestBackwardCompatMode:
    def test_manifest_with_vlm_mode_still_loadable(self, tmp_path: Path) -> None:
        """Stage3Manifest with mode='vlm' must parse without error."""
        from epubforge.stage3_artifacts import Stage3Manifest

        manifest = Stage3Manifest(
            mode="vlm",
            artifact_id="abcd1234abcd1234",
            artifact_dir="03_extract/artifacts/abcd1234abcd1234",
            created_at="2025-01-01T00:00:00Z",
            raw_sha256="aa",
            pages_sha256="bb",
            source_pdf="source/source.pdf",
            source_pdf_sha256="cc",
            selected_pages=[1],
            toc_pages=[],
            complex_pages=[],
            page_filter=None,
            unit_files=[],
            sidecars={},
            settings={},
        )
        assert manifest.mode == "vlm"

    def test_manifest_with_skip_vlm_mode_still_loadable(self, tmp_path: Path) -> None:
        """Stage3Manifest with mode='skip_vlm' must parse without error."""
        from epubforge.stage3_artifacts import Stage3Manifest

        manifest = Stage3Manifest(
            mode="skip_vlm",
            artifact_id="abcd1234abcd1234",
            artifact_dir="03_extract/artifacts/abcd1234abcd1234",
            created_at="2025-01-01T00:00:00Z",
            raw_sha256="aa",
            pages_sha256="bb",
            source_pdf="source/source.pdf",
            source_pdf_sha256="cc",
            selected_pages=[1],
            toc_pages=[],
            complex_pages=[],
            page_filter=None,
            unit_files=[],
            sidecars={},
            settings={},
        )
        assert manifest.mode == "skip_vlm"

    def test_extraction_result_with_vlm_mode_still_valid(self) -> None:
        """Stage3ExtractionResult with mode='vlm' must be accepted."""
        from epubforge.stage3_artifacts import Stage3ExtractionResult

        result = Stage3ExtractionResult(
            mode="vlm",
            unit_files=[],
            audit_notes_path=Path("/tmp/audit.json"),
            book_memory_path=Path("/tmp/book_memory.json"),
            evidence_index_path=Path("/tmp/evidence.json"),
            selected_pages=[],
            toc_pages=[],
            complex_pages=[],
        )
        assert result.mode == "vlm"

    def test_evidence_index_with_skip_vlm_mode_still_valid(self) -> None:
        """EvidenceIndex with mode='skip_vlm' must be accepted (old artifacts)."""
        from epubforge.stage3_artifacts import EvidenceIndex

        idx = EvidenceIndex(
            schema_version=3,
            artifact_id="abcd1234abcd1234",
            mode="skip_vlm",
            source_pdf="source/source.pdf",
        )
        assert idx.mode == "skip_vlm"

    def test_extraction_metadata_with_skip_vlm_mode_still_valid(self) -> None:
        """ExtractionMetadata.stage3_mode='skip_vlm' must parse (old Book JSON)."""
        from epubforge.ir.semantic import ExtractionMetadata

        meta = ExtractionMetadata(stage3_mode="skip_vlm")
        assert meta.stage3_mode == "skip_vlm"

    def test_stage3_editor_meta_with_vlm_mode_still_valid(self) -> None:
        """Stage3EditorMeta.mode='vlm' must parse (old editor state)."""
        from epubforge.editor.state import Stage3EditorMeta

        meta = Stage3EditorMeta(
            mode="vlm",
            skipped_vlm=False,
            manifest_path="03_extract/artifacts/x/manifest.json",
            manifest_sha256="aabb",
            artifact_id="abcd1234abcd1234",
            selected_pages=[1],
            complex_pages=[],
            source_pdf="source/source.pdf",
            evidence_index_path="03_extract/artifacts/x/evidence_index.json",
            extraction_warnings_path="03_extract/artifacts/x/warnings.json",
        )
        assert meta.mode == "vlm"


# ---------------------------------------------------------------------------
# 4. CLI --skip-vlm is accepted but ignored
# ---------------------------------------------------------------------------


class TestCliSkipVlmDeprecated:
    def _invoke_extract_cmd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        extra_args: list[str],
    ) -> Any:
        """Invoke the 'extract' CLI command with a fake pipeline."""
        from epubforge.cli import app

        # Provide a PDF file (content doesn't matter — pipeline is mocked)
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")

        monkeypatch.setattr(
            "epubforge.pipeline.run_extract",
            lambda *a, **k: None,
        )

        runner = CliRunner()
        return runner.invoke(
            app,
            ["extract", str(pdf)] + extra_args,
            catch_exceptions=False,
        )

    def test_skip_vlm_flag_accepted_without_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--skip-vlm must be accepted as a valid option (no error, no exit code 2)."""
        result = self._invoke_extract_cmd(tmp_path, monkeypatch, ["--skip-vlm"])
        assert result.exit_code != 2, f"CLI rejected --skip-vlm: {result.output}"

    def test_no_skip_vlm_flag_accepted_without_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-skip-vlm must be accepted as a valid option."""
        result = self._invoke_extract_cmd(tmp_path, monkeypatch, ["--no-skip-vlm"])
        assert result.exit_code != 2, f"CLI rejected --no-skip-vlm: {result.output}"

    def test_run_cmd_skip_vlm_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """'run' command --skip-vlm must also be accepted."""
        from epubforge.cli import app

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")

        monkeypatch.setattr("epubforge.pipeline.run_all", lambda *a, **k: None)

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["run", str(pdf), "--skip-vlm"],
            catch_exceptions=False,
        )
        assert result.exit_code != 2, f"CLI rejected --skip-vlm on run: {result.output}"

    def test_skip_vlm_help_mentions_deprecated(
        self,
        tmp_path: Path,
    ) -> None:
        """--help output for 'extract' must mention DEPRECATED for --skip-vlm."""
        from epubforge.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["extract", "--help"])
        assert "DEPRECATED" in result.output, (
            "--skip-vlm help text must mention DEPRECATED"
        )
