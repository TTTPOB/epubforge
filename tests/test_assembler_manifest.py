"""Tests for assembler.assemble_from_manifest and pipeline.run_assemble freshness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest

from epubforge.assembler import assemble_from_manifest, UNIT_SOURCE
from epubforge.ir.semantic import (
    Book,
    ExtractionMetadata,
    Footnote,
    Paragraph,
    Table,
)
from epubforge.stage3_artifacts import (
    Stage3ActivePointer,
    Stage3ContractError,
    Stage3Manifest,
    activate_manifest_atomic,
    write_artifact_manifest_atomic,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BASE_SETTINGS_SKIP: dict[str, Any] = {
    "skip_vlm": True,
    "contract_version": 3,
    "vlm_dpi": None,
    "max_vlm_batch_pages": None,
    "enable_book_memory": False,
    "vlm_model": None,
    "vlm_base_url": None,
}

_BASE_SETTINGS_VLM: dict[str, Any] = {
    "skip_vlm": False,
    "contract_version": 3,
    "vlm_dpi": 150,
    "max_vlm_batch_pages": 4,
    "enable_book_memory": False,
    "vlm_model": "google/gemini-flash-3",
    "vlm_base_url": None,
}


def _write_docling_unit(path: Path, pno: int, blocks: list[dict[str, Any]]) -> None:
    """Write a minimal docling_page unit file."""
    data = {
        "unit": {
            "kind": "docling_page",
            "pages": [pno],
            "page_kinds": ["simple"],
            "extractor": "skip_vlm",
            "contract_version": 3,
        },
        "draft_blocks": blocks,
        "evidence_refs": [],
        "candidate_edges": {},
        "audit_notes": [],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_vlm_unit(path: Path, pages: list[int], blocks: list[dict[str, Any]]) -> None:
    """Write a minimal vlm_batch unit file."""
    data = {
        "unit": {
            "kind": "vlm_batch",
            "pages": pages,
            "page_kinds": ["simple"] * len(pages),
            "extractor": "vlm",
            "contract_version": 3,
        },
        "blocks": blocks,
        "audit_notes": [],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_manifest_and_activate(
    work_dir: Path,
    *,
    mode: Literal["vlm", "skip_vlm"] = "skip_vlm",
    artifact_id: str = "abcd1234abcd1234",
    unit_file_paths: list[Path] | None = None,
) -> tuple[Stage3ActivePointer, Stage3Manifest]:
    """Create, write, and activate a Stage3Manifest. Returns (pointer, manifest)."""
    art_dir = work_dir / "03_extract" / "artifacts" / artifact_id
    art_dir.mkdir(parents=True, exist_ok=True)

    if unit_file_paths is None:
        unit_file_paths = []

    unit_files_rel = [str(p.relative_to(work_dir).as_posix()) for p in unit_file_paths]
    sidecar_dir = art_dir

    # Write stub sidecar files
    for name in ("audit_notes.json", "book_memory.json", "evidence_index.json"):
        (sidecar_dir / name).write_text("{}", encoding="utf-8")

    sidecars = {
        "audit_notes": str(
            (sidecar_dir / "audit_notes.json").relative_to(work_dir).as_posix()
        ),
        "book_memory": str(
            (sidecar_dir / "book_memory.json").relative_to(work_dir).as_posix()
        ),
        "evidence_index": str(
            (sidecar_dir / "evidence_index.json").relative_to(work_dir).as_posix()
        ),
    }

    manifest = Stage3Manifest(
        mode=mode,
        artifact_id=artifact_id,
        artifact_dir=str(art_dir.relative_to(work_dir).as_posix()),
        created_at="2026-04-24T00:00:00Z",
        raw_sha256="aabbcc" * 10 + "aabb",
        pages_sha256="ddeeff" * 10 + "ddee",
        source_pdf="source/source.pdf",
        source_pdf_sha256="112233" * 10 + "1122",
        selected_pages=[1, 2],
        toc_pages=[],
        complex_pages=[],
        page_filter=None,
        unit_files=unit_files_rel,
        sidecars=sidecars,
        settings=_BASE_SETTINGS_SKIP if mode == "skip_vlm" else _BASE_SETTINGS_VLM,
    )

    write_artifact_manifest_atomic(work_dir, manifest)
    activate_manifest_atomic(work_dir, manifest)

    from epubforge.stage3_artifacts import load_active_stage3_manifest

    pointer, _ = load_active_stage3_manifest(work_dir)
    return pointer, manifest


def _make_book_extraction(
    artifact_id: str,
    manifest_sha256: str,
    mode: Literal["vlm", "skip_vlm"] = "skip_vlm",
) -> ExtractionMetadata:
    return ExtractionMetadata(
        stage3_mode=mode,
        stage3_manifest_path="/some/path/manifest.json",
        stage3_manifest_sha256=manifest_sha256,
        artifact_id=artifact_id,
        selected_pages=[1, 2],
        complex_pages=[],
        source_pdf="source/source.pdf",
        evidence_index_path="03_extract/artifacts/xxx/evidence_index.json",
    )


# ---------------------------------------------------------------------------
# 1-4. Freshness tests — run_assemble
# ---------------------------------------------------------------------------


class TestRunAssembleFreshness:
    """run_assemble skips only when artifact_id AND manifest_sha256 match.

    cfg.book_work_dir(pdf_path) returns work_dir / pdf_path.stem.
    So for pdf = work_dir / "mybook" / "book.pdf", the computed work dir is
    cfg.runtime.work_dir / "book".  We use a fake pdf stem to control this:
    the PDF file passed is <work_dir> / "book.pdf" and cfg work_dir is the
    parent of <work_dir>, so cfg.book_work_dir returns <work_dir>.
    """

    def _setup_work_dir(self, tmp_path: Path, stem: str = "book") -> tuple[Path, Path]:
        """Create work_dir and return (cfg_work_dir, work).

        cfg_work_dir: passed as RuntimeSettings(work_dir=...)
        work: cfg_work_dir / stem  (i.e. cfg.book_work_dir(pdf))
        """
        cfg_work = tmp_path / "wrk"
        work = cfg_work / stem
        work.mkdir(parents=True, exist_ok=True)
        source_dir = work / "source"
        source_dir.mkdir()
        (source_dir / "source.pdf").write_bytes(b"fake pdf")
        (work / "01_raw.json").write_text("{}", encoding="utf-8")
        (work / "02_pages.json").write_text(
            json.dumps({"pages": [{"page": 1, "kind": "simple"}]}),
            encoding="utf-8",
        )
        return cfg_work, work

    def _make_cfg(self, cfg_work: Path) -> Any:
        from epubforge.config import Config, RuntimeSettings

        return Config(runtime=RuntimeSettings(work_dir=cfg_work))

    def test_matching_sha_allows_skip(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When artifact_id and manifest_sha256 both match, assemble is skipped."""
        cfg_work, work = self._setup_work_dir(tmp_path, stem="book")
        unit_path = work / "03_extract" / "artifacts" / "art001" / "unit_0000.json"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        _write_docling_unit(
            unit_path,
            1,
            [
                {
                    "kind": "paragraph",
                    "text": "hello",
                    "provenance": {"page": 1, "source": "docling"},
                }
            ],
        )

        pointer, manifest = _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id="art001", unit_file_paths=[unit_path]
        )

        # Write a fresh 05_semantic_raw.json with matching extraction metadata
        out = work / "05_semantic_raw.json"
        book = Book(title="Test")
        book.extraction = _make_book_extraction("art001", pointer.manifest_sha256)
        out.write_text(book.model_dump_json(indent=2), encoding="utf-8")

        # Monkeypatch assemble_from_manifest to detect if it was called
        called = []
        import epubforge.assembler as asm_mod

        original = asm_mod.assemble_from_manifest
        monkeypatch.setattr(
            asm_mod,
            "assemble_from_manifest",
            lambda *a, **kw: called.append(1) or original(*a, **kw),
        )

        from epubforge import pipeline

        cfg = self._make_cfg(cfg_work)
        # pdf stem is "book" → book_work_dir returns cfg_work / "book" == work
        pipeline.run_assemble(cfg_work / "book.pdf", cfg)

        assert not called, (
            "assemble_from_manifest should not be called when output is fresh"
        )

    def test_vlm_to_skip_vlm_causes_reassemble(self, tmp_path: Path) -> None:
        """Switching from VLM to skip-VLM (different artifact_id) causes reassemble."""
        cfg_work, work = self._setup_work_dir(tmp_path, stem="book")
        unit_path = work / "03_extract" / "artifacts" / "art002skip" / "unit_0000.json"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        _write_docling_unit(
            unit_path,
            1,
            [
                {
                    "kind": "paragraph",
                    "text": "skip content",
                    "provenance": {"page": 1, "source": "docling"},
                }
            ],
        )

        pointer, manifest = _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id="art002skip", unit_file_paths=[unit_path]
        )

        # Write stale output pretending it was generated by old VLM artifact "art002vlm"
        out = work / "05_semantic_raw.json"
        book = Book(title="Test")
        book.extraction = _make_book_extraction(
            "art002vlm", "old_sha_from_vlm", mode="vlm"
        )
        out.write_text(book.model_dump_json(indent=2), encoding="utf-8")

        from epubforge import pipeline

        cfg = self._make_cfg(cfg_work)
        pipeline.run_assemble(cfg_work / "book.pdf", cfg)

        # Output should be regenerated and contain the new skip_vlm extraction metadata
        result = Book.model_validate_json(out.read_text(encoding="utf-8"))
        assert result.extraction.artifact_id == "art002skip"
        assert result.extraction.stage3_mode == "skip_vlm"
        assert result.extraction.stage3_manifest_sha256 == pointer.manifest_sha256

    def test_skip_vlm_to_vlm_causes_reassemble(self, tmp_path: Path) -> None:
        """Switching from skip-VLM to VLM (different artifact_id) causes reassemble."""
        cfg_work, work = self._setup_work_dir(tmp_path, stem="book")
        unit_path = work / "03_extract" / "artifacts" / "art003vlm" / "unit_0000.json"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        _write_vlm_unit(unit_path, [1], [{"kind": "paragraph", "text": "vlm content"}])

        pointer, manifest = _make_manifest_and_activate(
            work, mode="vlm", artifact_id="art003vlm", unit_file_paths=[unit_path]
        )

        # Write stale output pretending it was generated by old skip-VLM artifact
        out = work / "05_semantic_raw.json"
        book = Book(title="Test")
        book.extraction = _make_book_extraction(
            "art003skip", "old_sha_from_skip", mode="skip_vlm"
        )
        out.write_text(book.model_dump_json(indent=2), encoding="utf-8")

        from epubforge import pipeline

        cfg = self._make_cfg(cfg_work)
        pipeline.run_assemble(cfg_work / "book.pdf", cfg)

        result = Book.model_validate_json(out.read_text(encoding="utf-8"))
        assert result.extraction.artifact_id == "art003vlm"
        assert result.extraction.stage3_mode == "vlm"

    def test_page_filter_change_causes_reassemble(self, tmp_path: Path) -> None:
        """After page filter change the active artifact_id changes → reassemble."""
        cfg_work, work = self._setup_work_dir(tmp_path, stem="book")
        unit_path = work / "03_extract" / "artifacts" / "art004new" / "unit_0000.json"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        _write_docling_unit(
            unit_path,
            1,
            [
                {
                    "kind": "paragraph",
                    "text": "filtered",
                    "provenance": {"page": 1, "source": "docling"},
                }
            ],
        )

        pointer, _ = _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id="art004new", unit_file_paths=[unit_path]
        )

        # Stale output from a different artifact_id
        out = work / "05_semantic_raw.json"
        book = Book(title="Test")
        book.extraction = _make_book_extraction("art004old", "old_sha")
        out.write_text(book.model_dump_json(indent=2), encoding="utf-8")

        from epubforge import pipeline

        cfg = self._make_cfg(cfg_work)
        pipeline.run_assemble(cfg_work / "book.pdf", cfg)

        result = Book.model_validate_json(out.read_text(encoding="utf-8"))
        assert result.extraction.artifact_id == "art004new"

    def test_missing_output_causes_assemble(self, tmp_path: Path) -> None:
        """Missing 05_semantic_raw.json always triggers assembly."""
        cfg_work, work = self._setup_work_dir(tmp_path, stem="book")
        unit_path = work / "03_extract" / "artifacts" / "art005" / "unit_0000.json"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        _write_docling_unit(
            unit_path,
            1,
            [
                {
                    "kind": "paragraph",
                    "text": "first run",
                    "provenance": {"page": 1, "source": "docling"},
                }
            ],
        )

        _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id="art005", unit_file_paths=[unit_path]
        )

        out = work / "05_semantic_raw.json"
        assert not out.exists()

        from epubforge import pipeline

        cfg = self._make_cfg(cfg_work)
        pipeline.run_assemble(cfg_work / "book.pdf", cfg)

        assert out.exists()
        result = Book.model_validate_json(out.read_text(encoding="utf-8"))
        assert result.extraction.artifact_id == "art005"

    def test_damaged_output_causes_reassemble(self, tmp_path: Path) -> None:
        """Damaged (unparseable) 05_semantic_raw.json triggers reassembly."""
        cfg_work, work = self._setup_work_dir(tmp_path, stem="book")
        unit_path = work / "03_extract" / "artifacts" / "art006" / "unit_0000.json"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        _write_docling_unit(
            unit_path,
            1,
            [
                {
                    "kind": "paragraph",
                    "text": "recovered",
                    "provenance": {"page": 1, "source": "docling"},
                }
            ],
        )

        _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id="art006", unit_file_paths=[unit_path]
        )

        out = work / "05_semantic_raw.json"
        out.write_text("THIS IS NOT JSON {{{", encoding="utf-8")

        from epubforge import pipeline

        cfg = self._make_cfg(cfg_work)
        pipeline.run_assemble(cfg_work / "book.pdf", cfg)

        result = Book.model_validate_json(out.read_text(encoding="utf-8"))
        assert result.extraction.artifact_id == "art006"


# ---------------------------------------------------------------------------
# 6. Legacy: no active manifest → fail
# ---------------------------------------------------------------------------


class TestLegacyFallbackFails:
    def test_no_active_manifest_raises(self, tmp_path: Path) -> None:
        """run_assemble fails fast when there is no active Stage 3 manifest."""
        cfg_work = tmp_path / "wrk"
        work = cfg_work / "book"
        work.mkdir(parents=True, exist_ok=True)

        # Create old-style root unit file (legacy) — should be ignored
        extract_dir = work / "03_extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        _write_docling_unit(
            extract_dir / "unit_0000.json",
            1,
            [{"kind": "paragraph", "text": "old unit"}],
        )

        from epubforge import pipeline
        from epubforge.config import Config, RuntimeSettings

        cfg = Config(runtime=RuntimeSettings(work_dir=cfg_work))

        with pytest.raises(Stage3ContractError):
            pipeline.run_assemble(cfg_work / "book.pdf", cfg)


# ---------------------------------------------------------------------------
# 7. No auto heuristics
# ---------------------------------------------------------------------------


class TestNoAutoHeuristics:
    def _make_work_and_manifest(
        self,
        tmp_path: Path,
        units: list[tuple[str, list[dict[str, Any]]]],
        mode: Literal["vlm", "skip_vlm"] = "skip_vlm",
    ) -> tuple[Path, Stage3Manifest]:
        """Create work dir and manifest with given units.

        units: list of (unit_kind, blocks_list)
        Returns (work_dir, manifest).
        """
        work = tmp_path / "work" / "book"
        work.mkdir(parents=True, exist_ok=True)
        art_id = "testheur001"
        art_dir = work / "03_extract" / "artifacts" / art_id
        art_dir.mkdir(parents=True, exist_ok=True)

        unit_paths: list[Path] = []
        for idx, (kind, blocks) in enumerate(units):
            p = art_dir / f"unit_{idx:04d}.json"
            if kind == "docling_page":
                _write_docling_unit(p, idx + 1, blocks)
            else:
                _write_vlm_unit(p, [idx + 1], blocks)
            unit_paths.append(p)

        _, manifest = _make_manifest_and_activate(
            work, mode=mode, artifact_id=art_id, unit_file_paths=unit_paths
        )
        return work, manifest

    def test_no_footnote_pairing(self, tmp_path: Path) -> None:
        """Assembled Book has no paired footnotes (pairing heuristic not applied)."""
        blocks = [
            {
                "kind": "paragraph",
                "text": "Main text with callout \x01①\x01.",
                "provenance": {"page": 1, "source": "docling"},
            },
            {
                "kind": "footnote",
                "callout": "①",
                "text": "Footnote body.",
                "provenance": {"page": 1, "source": "docling"},
            },
        ]
        work, manifest = self._make_work_and_manifest(
            tmp_path, [("docling_page", blocks)]
        )

        book = assemble_from_manifest(work, manifest)

        footnotes = [
            b for ch in book.chapters for b in ch.blocks if isinstance(b, Footnote)
        ]
        assert len(footnotes) == 1
        assert not footnotes[0].paired, (
            "No footnote pairing should happen during assemble"
        )

    def test_no_empty_callout_merge(self, tmp_path: Path) -> None:
        """Empty-callout footnotes are NOT merged into preceding footnotes."""
        blocks = [
            {
                "kind": "footnote",
                "callout": "①",
                "text": "First footnote incomplete",
                "provenance": {"page": 1, "source": "docling"},
            },
            {
                "kind": "footnote",
                "callout": "",
                "text": " continuation here.",
                "provenance": {"page": 1, "source": "docling"},
            },
        ]
        work, manifest = self._make_work_and_manifest(
            tmp_path, [("docling_page", blocks)]
        )

        book = assemble_from_manifest(work, manifest)

        footnotes = [
            b for ch in book.chapters for b in ch.blocks if isinstance(b, Footnote)
        ]
        # Both footnotes should be present separately — no merge
        assert len(footnotes) == 2

    def test_no_continued_table_merge(self, tmp_path: Path) -> None:
        """Table blocks with continuation=True are NOT merged into preceding tables."""
        blocks = [
            {
                "kind": "table",
                "html": "<table><tr><td>A</td></tr></table>",
                "continuation": False,
                "provenance": {"page": 1, "source": "docling"},
            },
            {
                "kind": "table",
                "html": "<table><tr><td>B</td></tr></table>",
                "continuation": True,
                "provenance": {"page": 2, "source": "docling"},
            },
        ]
        work, manifest = self._make_work_and_manifest(
            tmp_path, [("docling_page", blocks)]
        )

        book = assemble_from_manifest(work, manifest)

        tables = [b for ch in book.chapters for b in ch.blocks if isinstance(b, Table)]
        assert len(tables) == 2, "continuation tables should NOT be merged"
        assert tables[1].continuation is True  # flag preserved

    def test_no_heading_chapter_split(self, tmp_path: Path) -> None:
        """H1 headings do NOT split chapters — only one chapter is produced."""
        blocks = [
            {
                "kind": "heading",
                "text": "Chapter 1",
                "level": 1,
                "provenance": {"page": 1, "source": "docling"},
            },
            {
                "kind": "paragraph",
                "text": "Chapter 1 content.",
                "provenance": {"page": 1, "source": "docling"},
            },
            {
                "kind": "heading",
                "text": "Chapter 2",
                "level": 1,
                "provenance": {"page": 2, "source": "docling"},
            },
            {
                "kind": "paragraph",
                "text": "Chapter 2 content.",
                "provenance": {"page": 2, "source": "docling"},
            },
        ]
        work, manifest = self._make_work_and_manifest(
            tmp_path, [("docling_page", blocks)]
        )

        book = assemble_from_manifest(work, manifest)

        assert len(book.chapters) == 1, "should produce exactly one chapter"
        assert book.chapters[0].title == "Draft extraction"


# ---------------------------------------------------------------------------
# 8. Single chapter
# ---------------------------------------------------------------------------


class TestSingleChapter:
    def test_docling_produces_single_chapter(self, tmp_path: Path) -> None:
        work = tmp_path / "work" / "book"
        work.mkdir(parents=True, exist_ok=True)
        art_dir = work / "03_extract" / "artifacts" / "sc001"
        art_dir.mkdir(parents=True, exist_ok=True)
        u = art_dir / "unit_0000.json"
        _write_docling_unit(
            u,
            1,
            [
                {
                    "kind": "paragraph",
                    "text": "para",
                    "provenance": {"page": 1, "source": "docling"},
                }
            ],
        )
        _, manifest = _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id="sc001", unit_file_paths=[u]
        )

        book = assemble_from_manifest(work, manifest)

        assert len(book.chapters) == 1
        assert book.chapters[0].title == "Draft extraction"

    def test_vlm_produces_single_chapter(self, tmp_path: Path) -> None:
        work = tmp_path / "work" / "book"
        work.mkdir(parents=True, exist_ok=True)
        art_dir = work / "03_extract" / "artifacts" / "sc002"
        art_dir.mkdir(parents=True, exist_ok=True)
        u = art_dir / "unit_0000.json"
        _write_vlm_unit(u, [1], [{"kind": "paragraph", "text": "vlm para"}])
        _, manifest = _make_manifest_and_activate(
            work, mode="vlm", artifact_id="sc002", unit_file_paths=[u]
        )

        book = assemble_from_manifest(work, manifest)

        assert len(book.chapters) == 1
        assert book.chapters[0].title == "Draft extraction"


# ---------------------------------------------------------------------------
# 9. Provenance source mapping
# ---------------------------------------------------------------------------


class TestProvenanceMapping:
    def _run(
        self,
        tmp_path: Path,
        unit_kind: str,
        write_fn: Any,
        blocks: list[dict[str, Any]],
    ) -> Book:
        work = tmp_path / "work" / "book"
        work.mkdir(parents=True, exist_ok=True)
        art_id = f"prov_{unit_kind}"
        art_dir = work / "03_extract" / "artifacts" / art_id
        art_dir.mkdir(parents=True, exist_ok=True)
        u = art_dir / "unit_0000.json"
        write_fn(u, blocks)
        mode: Literal["vlm", "skip_vlm"] = (
            "skip_vlm" if unit_kind == "docling_page" else "vlm"
        )
        _, manifest = _make_manifest_and_activate(
            work, mode=mode, artifact_id=art_id, unit_file_paths=[u]
        )
        return assemble_from_manifest(work, manifest)

    def test_docling_page_source_is_docling(self, tmp_path: Path) -> None:
        blocks = [
            {
                "kind": "paragraph",
                "text": "hi",
                "provenance": {"page": 1, "source": "docling"},
            }
        ]
        book = self._run(
            tmp_path,
            "docling_page",
            lambda path, blks: _write_docling_unit(path, 1, blks),
            blocks,
        )
        para = book.chapters[0].blocks[0]
        assert isinstance(para, Paragraph)
        assert para.provenance.source == "docling"

    def test_vlm_batch_source_is_vlm(self, tmp_path: Path) -> None:
        blocks = [{"kind": "paragraph", "text": "vlm text"}]
        book = self._run(
            tmp_path,
            "vlm_batch",
            lambda path, blks: _write_vlm_unit(path, [1], blks),
            blocks,
        )
        para = book.chapters[0].blocks[0]
        assert isinstance(para, Paragraph)
        assert para.provenance.source == "vlm"


# ---------------------------------------------------------------------------
# 10. Unknown unit kind fails fast
# ---------------------------------------------------------------------------


class TestUnknownUnitKind:
    def test_unknown_unit_kind_raises(self, tmp_path: Path) -> None:
        work = tmp_path / "work" / "book"
        work.mkdir(parents=True, exist_ok=True)
        art_dir = work / "03_extract" / "artifacts" / "unk001"
        art_dir.mkdir(parents=True, exist_ok=True)
        u = art_dir / "unit_0000.json"

        # Write unit with unknown kind
        data = {
            "unit": {
                "kind": "llm_group",  # legacy / unknown kind
                "pages": [1],
                "page_kinds": ["simple"],
                "extractor": "legacy",
                "contract_version": 2,
            },
            "blocks": [{"kind": "paragraph", "text": "old"}],
            "audit_notes": [],
        }
        u.write_text(json.dumps(data), encoding="utf-8")

        _, manifest = _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id="unk001", unit_file_paths=[u]
        )

        with pytest.raises(ValueError, match="Unknown unit kind"):
            assemble_from_manifest(work, manifest)


# ---------------------------------------------------------------------------
# 11. Book.extraction metadata written correctly
# ---------------------------------------------------------------------------


class TestExtractionMetadataWritten:
    def test_extraction_metadata_fields(self, tmp_path: Path) -> None:
        cfg_work = tmp_path / "wrk"
        work = cfg_work / "book"
        work.mkdir(parents=True, exist_ok=True)

        art_id = "meta001"
        art_dir = work / "03_extract" / "artifacts" / art_id
        art_dir.mkdir(parents=True, exist_ok=True)
        u = art_dir / "unit_0000.json"
        _write_docling_unit(
            u,
            1,
            [
                {
                    "kind": "paragraph",
                    "text": "meta test",
                    "provenance": {"page": 1, "source": "docling"},
                }
            ],
        )

        pointer, manifest = _make_manifest_and_activate(
            work, mode="skip_vlm", artifact_id=art_id, unit_file_paths=[u]
        )

        from epubforge import pipeline
        from epubforge.config import Config, RuntimeSettings

        cfg = Config(runtime=RuntimeSettings(work_dir=cfg_work))
        pipeline.run_assemble(cfg_work / "book.pdf", cfg)

        out = work / "05_semantic_raw.json"
        book = Book.model_validate_json(out.read_text(encoding="utf-8"))

        assert book.extraction.artifact_id == art_id
        assert book.extraction.stage3_mode == "skip_vlm"
        assert book.extraction.stage3_manifest_sha256 == pointer.manifest_sha256
        assert book.extraction.selected_pages == manifest.selected_pages
        assert book.extraction.complex_pages == manifest.complex_pages
        assert book.extraction.source_pdf == manifest.source_pdf
        assert (
            book.extraction.evidence_index_path == manifest.sidecars["evidence_index"]
        )
        assert book.extraction.stage3_manifest_path is not None


# ---------------------------------------------------------------------------
# 12. UNIT_SOURCE constant
# ---------------------------------------------------------------------------


class TestUnitSourceConstant:
    def test_docling_page_maps_to_docling(self) -> None:
        assert UNIT_SOURCE["docling_page"] == "docling"

    def test_vlm_batch_maps_to_vlm(self) -> None:
        assert UNIT_SOURCE["vlm_batch"] == "vlm"

    def test_no_legacy_llm_group(self) -> None:
        assert "llm_group" not in UNIT_SOURCE

    def test_no_legacy_vlm_group(self) -> None:
        assert "vlm_group" not in UNIT_SOURCE
