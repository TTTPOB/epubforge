"""Tests for pipeline simplification — docling-only extraction.

Verifies:
1. _settings_for_artifact returns docling settings
2. run_extract always calls extract_skip_vlm (never the VLM extractor)
3. mode="docling" in result and activated manifest
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
) -> Config:
    return Config(
        runtime=RuntimeSettings(work_dir=tmp_path / "work"),
        extract=ExtractSettings(),
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
    """_settings_for_artifact must return fixed docling settings."""

    def test_returns_docling_settings(
        self, tmp_path: Path
    ) -> None:
        cfg = _make_cfg(tmp_path)
        from epubforge.pipeline import _settings_for_artifact

        settings = _settings_for_artifact(cfg)

        assert settings["enable_book_memory"] is False
        assert settings["contract_version"] == 3
        # Removed fields must not be present
        assert "skip_vlm" not in settings
        assert "vlm_dpi" not in settings
        assert "max_vlm_batch_pages" not in settings
        assert "vlm_model" not in settings
        assert "vlm_base_url" not in settings


# ---------------------------------------------------------------------------
# 2. run_extract always calls extract_skip_vlm
# ---------------------------------------------------------------------------


class TestRunExtractAlwaysDocling:
    def test_always_calls_extract_skip_vlm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_extract must always use extract_skip_vlm."""
        cfg = _make_cfg(tmp_path)
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
        """Log must mention 'Docling evidence draft'."""
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

        assert "provider_required=False" in caplog.text
