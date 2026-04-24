"""End-to-end no-API-key test for the skip-VLM pipeline.

This test simulates the full skip-VLM pipeline without any API keys or real
Docling/VLM calls.  It creates a minimal workdir with fake parse/classify
outputs, runs extraction (Stage 3) → assemble (Stage 4) → editor init →
build_epub in sequence and verifies each stage produces the expected outputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from epubforge.config import Config, ExtractSettings, RuntimeSettings
from epubforge.ir.semantic import Book, ExtractionMetadata, Paragraph, Provenance
from epubforge.stage3_artifacts import (
    Stage3ExtractionResult,
    Stage3Manifest,
    _sha256_str,
    activate_manifest_atomic,
    write_artifact_manifest_atomic,
)


# ---------------------------------------------------------------------------
# Minimal DoclingDocument JSON (same schema used in test_pipeline_extract.py)
# ---------------------------------------------------------------------------

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
        "children": [
            {"$ref": "#/texts/0"},
        ],
        "content_layer": "body",
        "name": "_root_",
        "label": "unspecified",
    },
    "groups": [],
    "texts": [
        {
            "self_ref": "#/texts/0",
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [{"page_no": 1, "bbox": {"l": 0, "t": 792, "r": 612, "b": 0, "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 10]}],
            "orig": "Hello world",
            "text": "Hello world",
        }
    ],
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

_PAGES_DATA: dict[str, Any] = {
    "pages": [
        {"page": 1, "kind": "simple"},
        {"page": 2, "kind": "simple"},
    ]
}

_BASE_SETTINGS: dict[str, Any] = {
    "skip_vlm": True,
    "contract_version": 3,
    "vlm_dpi": None,
    "max_vlm_batch_pages": None,
    "enable_book_memory": False,
    "vlm_model": None,
    "vlm_base_url": None,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path: Path) -> Config:
    """Config with skip_vlm=True and no API keys."""
    return Config(
        runtime=RuntimeSettings(work_dir=tmp_path / "work"),
        extract=ExtractSettings(skip_vlm=True),
        # no llm/vlm api_key → provider_required must never be True on this path
    )


def _setup_workdir(work: Path) -> None:
    """Populate the minimal stage 1+2 outputs that Stage 3 ingests."""
    (work / "source").mkdir(parents=True, exist_ok=True)
    (work / "source" / "source.pdf").write_bytes(b"%PDF-1.7\nfake pdf content\n")
    # Minimal source_meta.json (written by run_parse in real usage)
    (work / "source" / "source_meta.json").write_text(
        json.dumps({"source_sha256": "fakehash", "source_path": "source/source.pdf"}),
        encoding="utf-8",
    )
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
    """Stub extractor that writes minimal but valid Stage 3 artifact files."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write a minimal unit file with a docling_page unit
    unit_data = {
        "unit": {
            "kind": "docling_page",
            "pages": [1],
            "page_kinds": ["simple"],
            "extractor": "skip_vlm",
            "contract_version": 3,
        },
        "draft_blocks": [
            {
                "kind": "paragraph",
                "text": "Hello world.",
                "role": "docling_heading_candidate",
                "provenance": {
                    "page": 1,
                    "bbox": [0, 792, 612, 0],
                    "source": "docling",
                    "raw_ref": "#/texts/0",
                    "raw_label": "section_header",
                    "artifact_id": "fake-artifact",
                    "evidence_ref": "#/texts/0",
                },
            }
        ],
        "evidence_refs": [],
        "candidate_edges": {},
        "audit_notes": [],
    }
    unit_file = out_dir / "unit_0000.json"
    unit_file.write_text(json.dumps(unit_data, indent=2), encoding="utf-8")

    audit_file = out_dir / "audit_notes.json"
    audit_file.write_text("[]", encoding="utf-8")

    book_memory = out_dir / "book_memory.json"
    book_memory.write_text("{}", encoding="utf-8")

    evidence_index = out_dir / "evidence_index.json"
    evidence_index.write_text("{}", encoding="utf-8")

    warnings_file = out_dir / "warnings.json"
    warnings_file.write_text("[]", encoding="utf-8")

    return Stage3ExtractionResult(
        mode="skip_vlm",
        unit_files=[unit_file],
        audit_notes_path=audit_file,
        book_memory_path=book_memory,
        evidence_index_path=evidence_index,
        warnings_path=warnings_file,
        selected_pages=[1, 2],
        toc_pages=[],
        complex_pages=[],
    )


# ---------------------------------------------------------------------------
# The E2E test
# ---------------------------------------------------------------------------


def test_skip_vlm_e2e_no_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Simulate: run --skip-vlm reaches Stage 4, produces valid Book output.

    This test does NOT call actual Docling/VLM.  It creates a minimal workdir
    with fake parse/classify outputs and runs the pipeline from Stage 3 onward.

    Verifies:
    1. run_extract() with skip_vlm=True succeeds without API keys
    2. 05_semantic_raw.json contains Book.extraction.stage3_mode="skip_vlm"
       and a non-empty stage3_manifest_sha256
    3. editor init can initialize from the raw semantic output
    4. build_epub() can produce output from the editor book
    5. No API keys are required at any step
    """
    cfg = _make_cfg(tmp_path)
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    work = cfg.book_work_dir(pdf)
    _setup_workdir(work)

    # Patch the actual extractor so we don't need Docling/API
    monkeypatch.setattr(
        "epubforge.extract_skip_vlm.extract_skip_vlm", _fake_extract_skip_vlm
    )

    # Guard: require_vlm / require_llm must never be called on this path
    def _fail_require_vlm(self: Any) -> None:
        raise AssertionError("require_vlm called in skip-VLM E2E test — no API key available")

    def _fail_require_llm(self: Any) -> None:
        raise AssertionError("require_llm called in skip-VLM E2E test — no API key available")

    monkeypatch.setattr(Config, "require_vlm", _fail_require_vlm)
    monkeypatch.setattr(Config, "require_llm", _fail_require_llm)

    # -----------------------------------------------------------------------
    # Stage 3: extract
    # -----------------------------------------------------------------------
    from epubforge.pipeline import run_extract

    run_extract(pdf, cfg)

    # Active manifest must exist after extraction
    from epubforge.stage3_artifacts import load_active_stage3_manifest

    pointer, manifest = load_active_stage3_manifest(work)
    assert manifest.mode == "skip_vlm"
    assert len(manifest.artifact_id) == 16
    assert "warnings" in manifest.sidecars, "manifest must register warnings sidecar"

    # -----------------------------------------------------------------------
    # Stage 4: assemble → 05_semantic_raw.json
    # -----------------------------------------------------------------------
    from epubforge.pipeline import run_assemble

    run_assemble(pdf, cfg)

    semantic_raw = work / "05_semantic_raw.json"
    assert semantic_raw.exists(), "05_semantic_raw.json must be written by run_assemble"

    book = Book.model_validate_json(semantic_raw.read_text(encoding="utf-8"))

    # Verify extraction metadata
    assert book.extraction.stage3_mode == "skip_vlm", (
        f"Expected stage3_mode='skip_vlm', got {book.extraction.stage3_mode!r}"
    )
    assert book.extraction.artifact_id == manifest.artifact_id
    assert book.extraction.stage3_manifest_sha256 == pointer.manifest_sha256, (
        "05_semantic_raw.json must record the manifest sha256"
    )
    assert book.extraction.stage3_manifest_sha256, "manifest sha256 must not be empty"

    # Book must have at least one chapter with at least one block
    assert book.chapters, "Assembled book must have at least one chapter"

    # Verify role and provenance survive assembly
    first_block = book.chapters[0].blocks[0]
    assert isinstance(first_block, Paragraph)
    assert first_block.role == "docling_heading_candidate", f"Expected role preserved, got {first_block.role!r}"
    assert first_block.provenance.source == "docling"
    assert first_block.provenance.bbox == [0, 792, 612, 0]
    assert first_block.provenance.raw_ref == "#/texts/0"
    assert first_block.provenance.raw_label == "section_header"
    assert first_block.provenance.artifact_id == "fake-artifact"
    assert first_block.provenance.evidence_ref == "#/texts/0"

    # -----------------------------------------------------------------------
    # Editor init
    # -----------------------------------------------------------------------
    from epubforge.editor.tool_surface import run_init

    # run_init uses emit_json → capture stdout output
    import io
    import sys

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        exit_code = run_init(work, cfg)
    finally:
        sys.stdout = old_stdout

    assert exit_code == 0, "editor init must succeed"

    init_output = captured.getvalue().strip()
    init_payload = json.loads(init_output)
    assert "stage3" in init_payload, "editor init output must include stage3 metadata"
    assert init_payload["stage3"]["mode"] == "skip_vlm"
    assert init_payload["stage3"]["artifact_id"] == manifest.artifact_id

    # edit_state/book.json must exist
    from epubforge.editor.state import resolve_editor_paths

    paths = resolve_editor_paths(work)
    assert paths.book_path.exists(), "edit_state/book.json must be written by editor init"

    editor_book = Book.model_validate_json(paths.book_path.read_text(encoding="utf-8"))
    assert editor_book.initialized_at, "editor book must have initialized_at set"

    # -----------------------------------------------------------------------
    # Build EPUB from editor book
    # -----------------------------------------------------------------------
    from epubforge.epub_builder import build_epub

    epub_out = tmp_path / "output.epub"
    build_epub(paths.book_path, epub_out)

    assert epub_out.exists(), "build_epub must produce an .epub file"
    assert epub_out.stat().st_size > 0, "output .epub must not be empty"
