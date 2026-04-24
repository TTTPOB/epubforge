"""Tests for Stage 3 editor integration: meta, render-page, vlm-page, render-prompt context."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner, Result

from epubforge.cli import app
from epubforge.editor.state import (
    EditorMeta,
    Stage3EditorMeta,
    resolve_editor_paths,
)
from epubforge.ir.semantic import (
    Book,
    Chapter,
    ExtractionMetadata,
    Paragraph,
    Provenance,
)
from epubforge.stage3_artifacts import (
    Stage3Manifest,
    _sha256_str,
    activate_manifest_atomic,
    write_artifact_manifest_atomic,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


runner = CliRunner()


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="docling")


def _minimal_book(
    *,
    artifact_id: str | None = None,
    manifest_sha256: str | None = None,
    mode: str = "skip_vlm",
    pages: list[int] | None = None,
) -> Book:
    """Create a minimal book with optional extraction metadata."""
    blocks = []
    for p in pages or [1]:
        blocks.append(Paragraph(text=f"Para page {p}.", provenance=_prov(p)))
    book = Book(
        title="Test Book",
        chapters=[Chapter(title="Ch 1", blocks=blocks)],
    )
    if artifact_id is not None:
        book.extraction = ExtractionMetadata(
            stage3_mode=mode,  # type: ignore[arg-type]
            artifact_id=artifact_id,
            stage3_manifest_sha256=manifest_sha256,
            selected_pages=[1, 2],
            complex_pages=[2],
            source_pdf="source/source.pdf",
            evidence_index_path="",
        )
    return book


_BASE_SETTINGS: dict = {
    "skip_vlm": True,
    "contract_version": 3,
    "vlm_dpi": None,
    "max_vlm_batch_pages": None,
    "enable_book_memory": False,
    "vlm_model": None,
    "vlm_base_url": None,
}


def _make_manifest(
    tmp_path: Path,
    *,
    artifact_id: str = "aaaa1111bbbb2222",
    mode: str = "skip_vlm",
    selected_pages: list[int] | None = None,
    complex_pages: list[int] | None = None,
) -> Stage3Manifest:
    art_dir = f"03_extract/artifacts/{artifact_id}"
    return Stage3Manifest(
        mode=mode,  # type: ignore[arg-type]
        artifact_id=artifact_id,
        artifact_dir=art_dir,
        created_at="2026-04-24T00:00:00Z",
        raw_sha256="ddeeff",
        pages_sha256="112233",
        source_pdf="source/source.pdf",
        source_pdf_sha256="aabbcc",
        selected_pages=selected_pages or [1, 2],
        toc_pages=[],
        complex_pages=complex_pages or [2],
        page_filter=None,
        unit_files=[f"{art_dir}/unit_0000.json"],
        sidecars={
            "audit_notes": f"{art_dir}/audit_notes.json",
            "book_memory": f"{art_dir}/book_memory.json",
            "evidence_index": f"{art_dir}/evidence_index.json",
        },
        settings=_BASE_SETTINGS,
    )


def _create_artifact_files(work_dir: Path, manifest: Stage3Manifest) -> None:
    """Create all files referenced by the manifest so validation passes."""
    art_dir = work_dir / "03_extract" / "artifacts" / manifest.artifact_id
    art_dir.mkdir(parents=True, exist_ok=True)
    for rel in manifest.unit_files:
        p = work_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    for rel in manifest.sidecars.values():
        p = work_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")


def _setup_active_manifest(work_dir: Path, manifest: Stage3Manifest) -> str:
    """Write manifest + pointer and return manifest_sha256."""
    _create_artifact_files(work_dir, manifest)
    write_artifact_manifest_atomic(work_dir, manifest)
    activate_manifest_atomic(work_dir, manifest)
    # Return the sha of the manifest file for building ExtractionMetadata
    manifest_file = work_dir / manifest.artifact_dir / "manifest.json"
    return _sha256_str(manifest_file.read_text(encoding="utf-8"))


def _invoke(args: list[str]) -> Result:
    return runner.invoke(app, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# 7. Stage3EditorMeta validation (Pydantic model)
# ---------------------------------------------------------------------------


class TestStage3EditorMetaValidation:
    def test_valid_model_skip_vlm(self) -> None:
        m = Stage3EditorMeta(
            mode="skip_vlm",
            skipped_vlm=True,
            manifest_path="/work/03_extract/artifacts/abc/manifest.json",
            manifest_sha256="abcdef1234",
            artifact_id="abc",
            selected_pages=[1, 2, 3],
            complex_pages=[2],
            source_pdf="source/source.pdf",
            evidence_index_path="/work/03_extract/artifacts/abc/evidence_index.json",
            extraction_warnings_path="/work/03_extract/artifacts/abc/warnings.json",
        )
        assert m.mode == "skip_vlm"
        assert m.skipped_vlm is True

    def test_valid_model_vlm(self) -> None:
        m = Stage3EditorMeta(
            mode="vlm",
            skipped_vlm=False,
            manifest_path="/work/manifest.json",
            manifest_sha256="sha",
            artifact_id="xyz",
            selected_pages=[1],
            complex_pages=[],
            source_pdf="source/source.pdf",
            evidence_index_path="",
            extraction_warnings_path="",
        )
        assert m.mode == "vlm"
        assert m.skipped_vlm is False

    def test_unknown_mode(self) -> None:
        m = Stage3EditorMeta(
            mode="unknown",
            skipped_vlm=False,
            manifest_path="",
            manifest_sha256="",
            artifact_id="",
            selected_pages=[],
            complex_pages=[],
            source_pdf="",
            evidence_index_path="",
            extraction_warnings_path="",
        )
        assert m.mode == "unknown"

    def test_editor_meta_with_stage3(self) -> None:
        s3 = Stage3EditorMeta(
            mode="skip_vlm",
            skipped_vlm=True,
            manifest_path="",
            manifest_sha256="",
            artifact_id="abc",
            selected_pages=[],
            complex_pages=[],
            source_pdf="",
            evidence_index_path="",
            extraction_warnings_path="",
        )
        meta = EditorMeta(
            initialized_at="2026-01-01T00:00:00Z", uid_seed="seed", stage3=s3
        )
        assert meta.stage3 is not None
        assert meta.stage3.artifact_id == "abc"

    def test_editor_meta_without_stage3(self) -> None:
        meta = EditorMeta(initialized_at="2026-01-01T00:00:00Z", uid_seed="seed")
        assert meta.stage3 is None

    def test_roundtrip_json(self) -> None:
        s3 = Stage3EditorMeta(
            mode="skip_vlm",
            skipped_vlm=True,
            manifest_path="/tmp/manifest.json",
            manifest_sha256="abcd",
            artifact_id="efgh",
            selected_pages=[3, 5],
            complex_pages=[5],
            source_pdf="source/source.pdf",
            evidence_index_path="/tmp/evidence.json",
            extraction_warnings_path="/tmp/warnings.json",
        )
        meta = EditorMeta(
            initialized_at="2026-01-01T00:00:00Z", uid_seed="myseed", stage3=s3
        )
        serialised = meta.model_dump_json()
        restored = EditorMeta.model_validate_json(serialised)
        assert restored.stage3 is not None
        assert restored.stage3.selected_pages == [3, 5]


# ---------------------------------------------------------------------------
# 1. Editor init from matching 05_semantic_raw.json succeeds + writes stage3 meta
# ---------------------------------------------------------------------------


class TestEditorInitStage3:
    def test_init_from_matching_raw_succeeds_with_stage3_meta(
        self, tmp_path: Path
    ) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        manifest = _make_manifest(work_dir)
        sha256 = _setup_active_manifest(work_dir, manifest)

        # Write matching 05_semantic_raw.json
        book = _minimal_book(
            artifact_id=manifest.artifact_id,
            manifest_sha256=sha256,
            pages=[1, 2],
        )
        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )

        result = _invoke(["editor", "init", str(work_dir)])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.output)
        assert "stage3" in payload
        assert payload["stage3"]["artifact_id"] == manifest.artifact_id
        assert payload["stage3"]["mode"] == "skip_vlm"
        assert payload["stage3"]["skipped_vlm"] is True
        assert payload["stage3"]["selected_pages"] == [1, 2]

        paths = resolve_editor_paths(work_dir)
        meta_raw = paths.meta_path.read_text(encoding="utf-8")
        meta = EditorMeta.model_validate_json(meta_raw)
        assert meta.stage3 is not None
        assert meta.stage3.manifest_sha256 == sha256

    def test_init_prefers_05_semantic_over_raw(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        manifest = _make_manifest(work_dir)
        sha256 = _setup_active_manifest(work_dir, manifest)

        # Both exist and match; 05_semantic.json should win
        book = _minimal_book(
            artifact_id=manifest.artifact_id,
            manifest_sha256=sha256,
            pages=[1, 2],
        )
        (work_dir / "05_semantic.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )
        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )

        result = _invoke(["editor", "init", str(work_dir)])
        assert result.exit_code == 0, result.output

    def test_init_copies_audit_notes_to_edit_state(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        manifest = _make_manifest(work_dir)
        sha256 = _setup_active_manifest(work_dir, manifest)

        # Write actual content to audit_notes.json
        audit_notes_path = work_dir / manifest.sidecars["audit_notes"]
        audit_notes_path.write_text('[{"msg": "test note"}]', encoding="utf-8")

        book = _minimal_book(artifact_id=manifest.artifact_id, manifest_sha256=sha256)
        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )

        result = _invoke(["editor", "init", str(work_dir)])
        assert result.exit_code == 0, result.output

        paths = resolve_editor_paths(work_dir)
        extraction_notes = paths.audit_dir / "extraction_notes.json"
        assert extraction_notes.exists()
        content = json.loads(extraction_notes.read_text(encoding="utf-8"))
        assert content == [{"msg": "test note"}]


# ---------------------------------------------------------------------------
# 2. Editor init mismatch failure
# ---------------------------------------------------------------------------


class TestEditorInitMismatch:
    def test_init_fails_on_artifact_id_mismatch(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        manifest = _make_manifest(work_dir)
        _setup_active_manifest(work_dir, manifest)

        # Book has different artifact_id
        book = _minimal_book(
            artifact_id="WRONG_ARTIFACT_000",
            manifest_sha256="whatever",
        )
        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )

        result = _invoke(["editor", "init", str(work_dir)])
        # Should fail because the only raw file doesn't match
        assert result.exit_code != 0

    def test_init_fails_when_no_semantic_file_exists(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        manifest = _make_manifest(work_dir)
        _setup_active_manifest(work_dir, manifest)
        # No 05_semantic*.json files

        result = _invoke(["editor", "init", str(work_dir)])
        assert result.exit_code != 0

    def test_default_init_source_no_manifest_falls_back_to_file_check(
        self, tmp_path: Path
    ) -> None:
        """Without active manifest, default_init_source just checks file existence."""
        from epubforge.editor.state import default_init_source

        work_dir = tmp_path / "book"
        work_dir.mkdir()
        raw_path = work_dir / "05_semantic_raw.json"
        raw_path.write_text("{}", encoding="utf-8")

        paths = resolve_editor_paths(work_dir)
        # No active manifest → should return 05_semantic_raw.json (since 05_semantic.json absent)
        result = default_init_source(paths)
        assert result == raw_path


# ---------------------------------------------------------------------------
# 3. render-page from cwd unrelated to original PDF
# ---------------------------------------------------------------------------


class TestRenderPage:
    def _setup_initialized_work_dir(
        self,
        work_dir: Path,
        *,
        with_manifest: bool = True,
        with_pdf: bool = True,
    ) -> None:
        """Initialize an editor work dir with optional manifest and PDF."""
        if with_manifest:
            manifest = _make_manifest(work_dir)
            sha256 = _setup_active_manifest(work_dir, manifest)
            book = _minimal_book(
                artifact_id=manifest.artifact_id, manifest_sha256=sha256
            )
        else:
            book = _minimal_book()

        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )

        if with_pdf:
            source_dir = work_dir / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            # Create a real minimal 1-page PDF using pypdfium2
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument.new()
            doc.new_page(width=595, height=842)
            doc.save(str(source_dir / "source.pdf"))

        runner.invoke(app, ["editor", "init", str(work_dir)], catch_exceptions=False)

    def test_render_page_from_different_cwd(self, tmp_path: Path) -> None:
        """render-page must work even when called from a different working directory."""
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        self._setup_initialized_work_dir(work_dir)

        # Call from a different cwd by using absolute path
        result = runner.invoke(
            app,
            ["editor", "render-page", str(work_dir), "--page", "1"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["page"] == 1
        assert Path(payload["image_path"]).exists()

    def test_render_page_custom_out_path(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        self._setup_initialized_work_dir(work_dir)

        out_path = tmp_path / "custom_render.jpg"
        result = runner.invoke(
            app,
            [
                "editor",
                "render-page",
                str(work_dir),
                "--page",
                "1",
                "--out",
                str(out_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert Path(payload["image_path"]) == out_path
        assert out_path.exists()

    def test_render_page_default_output_path_format(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        self._setup_initialized_work_dir(work_dir)

        result = runner.invoke(
            app,
            ["editor", "render-page", str(work_dir), "--page", "1"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Default path should be edit_state/audit/page_images/page_0001.jpg
        expected = str(
            work_dir / "edit_state" / "audit" / "page_images" / "page_0001.jpg"
        )
        assert payload["image_path"] == expected


# ---------------------------------------------------------------------------
# 4. render-page with missing source.pdf → clear error
# ---------------------------------------------------------------------------


class TestRenderPageMissingPDF:
    def test_missing_source_pdf_gives_clear_error(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        # Setup with manifest but no PDF
        manifest = _make_manifest(work_dir)
        sha256 = _setup_active_manifest(work_dir, manifest)
        book = _minimal_book(artifact_id=manifest.artifact_id, manifest_sha256=sha256)
        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )
        runner.invoke(app, ["editor", "init", str(work_dir)], catch_exceptions=False)

        # Do not create source.pdf
        result = runner.invoke(
            app,
            ["editor", "render-page", str(work_dir), "--page", "1"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        output = json.loads(result.output)
        # Error should mention source.pdf and rerun parse
        assert (
            "source.pdf" in output.get("error", "")
            or "rerun" in output.get("error", "").lower()
        )

    def test_render_page_zero_page_rejected(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        result = runner.invoke(
            app,
            ["editor", "render-page", str(work_dir), "--page", "0"],
            catch_exceptions=False,
        )
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# 5. Mock vlm-page: verify output format and no book.json mutation
# ---------------------------------------------------------------------------


class TestVLMPage:
    def _setup_work_dir_with_pdf(self, work_dir: Path) -> tuple[Stage3Manifest, str]:
        """Initialize work dir fully with PDF and manifest."""
        manifest = _make_manifest(work_dir, selected_pages=[1, 2], complex_pages=[2])
        sha256 = _setup_active_manifest(work_dir, manifest)

        # Write evidence index with real evidence
        evidence_index = {
            "schema_version": 3,
            "artifact_id": manifest.artifact_id,
            "mode": "skip_vlm",
            "source_pdf": "source/source.pdf",
            "pages": {
                "1": {"items": [{"ref": "p1e1", "kind": "paragraph", "text": "hello"}]},
                "2": {
                    "items": [{"ref": "p2e1", "kind": "table", "text": "table data"}]
                },
            },
            "refs": {},
        }
        ev_path = work_dir / manifest.sidecars["evidence_index"]
        ev_path.write_text(json.dumps(evidence_index, indent=2), encoding="utf-8")

        book = _minimal_book(
            artifact_id=manifest.artifact_id, manifest_sha256=sha256, pages=[1, 2]
        )
        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )

        # Create PDF
        source_dir = work_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument.new()
        doc.new_page(width=595, height=842)
        doc.new_page(width=595, height=842)
        doc.save(str(source_dir / "source.pdf"))

        runner.invoke(app, ["editor", "init", str(work_dir)], catch_exceptions=False)
        return manifest, sha256

    def test_vlm_page_does_not_mutate_book_json(self, tmp_path: Path) -> None:
        """vlm-page must not modify book.json."""
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        self._setup_work_dir_with_pdf(work_dir)

        paths = resolve_editor_paths(work_dir)
        from epubforge.io import load_book

        book_before = load_book(paths.book_path)
        book_before_json = book_before.model_dump_json()

        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "page": 1,
            "issues": [],
            "suggestions": [],
            "notes": "ok",
        }

        with patch("epubforge.llm.client.LLMClient") as mock_llm_cls:
            mock_llm_instance = MagicMock()
            mock_llm_instance.chat_parsed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm_instance

            result = runner.invoke(
                app,
                ["editor", "vlm-page", str(work_dir), "--page", "1"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output

        # Verify book.json is unchanged
        from epubforge.io import load_book as lb2

        book_after = lb2(paths.book_path)
        assert book_after.model_dump_json() == book_before_json

    def test_vlm_page_writes_output_json(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        self._setup_work_dir_with_pdf(work_dir)

        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "page": 1,
            "issues": ["issue A"],
            "suggestions": [],
            "notes": "",
        }

        with patch("epubforge.llm.client.LLMClient") as mock_llm_cls:
            mock_llm_instance = MagicMock()
            mock_llm_instance.chat_parsed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm_instance

            result = runner.invoke(
                app,
                ["editor", "vlm-page", str(work_dir), "--page", "1"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "output_path" in payload
        out_path = Path(payload["output_path"])
        assert out_path.exists()
        output_data = json.loads(out_path.read_text(encoding="utf-8"))
        assert output_data["page"] == 1
        assert "vlm_result" in output_data

    def test_vlm_page_rejects_non_selected_page(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        self._setup_work_dir_with_pdf(work_dir)

        result = runner.invoke(
            app,
            ["editor", "vlm-page", str(work_dir), "--page", "99"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        output = json.loads(result.output)
        assert "99" in output.get("error", "")

    def test_vlm_page_custom_out_path(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        self._setup_work_dir_with_pdf(work_dir)

        out_path = tmp_path / "vlm_out.json"
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "page": 2,
            "issues": [],
            "suggestions": [],
            "notes": "",
        }

        with patch("epubforge.llm.client.LLMClient") as mock_llm_cls:
            mock_llm_instance = MagicMock()
            mock_llm_instance.chat_parsed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm_instance

            result = runner.invoke(
                app,
                [
                    "editor",
                    "vlm-page",
                    str(work_dir),
                    "--page",
                    "2",
                    "--out",
                    str(out_path),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert out_path.exists()


# ---------------------------------------------------------------------------
# 6. render-prompt includes extraction context and candidate-role guidance
# ---------------------------------------------------------------------------


class TestRenderPromptExtractionContext:
    def _setup_for_render_prompt(self, work_dir: Path) -> str:
        """Initialize editor state and return a chapter UID."""
        manifest = _make_manifest(work_dir, selected_pages=[1, 2], complex_pages=[2])
        sha256 = _setup_active_manifest(work_dir, manifest)

        book = _minimal_book(
            artifact_id=manifest.artifact_id, manifest_sha256=sha256, pages=[1, 2]
        )
        (work_dir / "05_semantic_raw.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )
        runner.invoke(app, ["editor", "init", str(work_dir)], catch_exceptions=False)

        paths = resolve_editor_paths(work_dir)
        from epubforge.io import load_book

        b = load_book(paths.book_path)
        uid = b.chapters[0].uid
        assert uid is not None
        return uid

    def test_render_prompt_contains_extraction_context(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        chapter_uid = self._setup_for_render_prompt(work_dir)

        result = runner.invoke(
            app,
            [
                "editor",
                "render-prompt",
                str(work_dir),
                "--kind",
                "scanner",
                "--chapter",
                chapter_uid,
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        prompt = result.output

        assert "Extraction context" in prompt
        assert "stage3" in prompt.lower() or "skip_vlm" in prompt
        assert "render-page" in prompt
        assert "vlm-page" in prompt

    def test_render_prompt_contains_candidate_role_guidance(
        self, tmp_path: Path
    ) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        chapter_uid = self._setup_for_render_prompt(work_dir)

        result = runner.invoke(
            app,
            [
                "editor",
                "render-prompt",
                str(work_dir),
                "--kind",
                "scanner",
                "--chapter",
                chapter_uid,
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        prompt = result.output

        assert "docling_*_candidate" in prompt or "candidate" in prompt

    def test_render_prompt_contains_page_coverage(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        chapter_uid = self._setup_for_render_prompt(work_dir)

        result = runner.invoke(
            app,
            [
                "editor",
                "render-prompt",
                str(work_dir),
                "--kind",
                "fixer",
                "--chapter",
                chapter_uid,
                "--issues",
                '["fix something"]',
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        prompt = result.output

        assert "page coverage" in prompt or "selected_pages" in prompt

    def test_render_prompt_uses_absolute_work_dir_in_commands(
        self, tmp_path: Path
    ) -> None:
        work_dir = tmp_path / "book"
        work_dir.mkdir()
        chapter_uid = self._setup_for_render_prompt(work_dir)

        result = runner.invoke(
            app,
            [
                "editor",
                "render-prompt",
                str(work_dir),
                "--kind",
                "reviewer",
                "--chapter",
                chapter_uid,
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        prompt = result.output

        # The prompt should contain absolute path to work_dir
        assert str(work_dir.resolve()) in prompt

    def test_render_prompt_without_stage3_has_no_extraction_context(
        self, tmp_path: Path
    ) -> None:
        """Legacy init (no active manifest) should produce prompt without extraction context."""
        work_dir = tmp_path / "book"
        work_dir.mkdir()

        # No manifest — minimal legacy setup
        book = _minimal_book()
        (work_dir / "05_semantic.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )
        runner.invoke(app, ["editor", "init", str(work_dir)], catch_exceptions=False)

        paths = resolve_editor_paths(work_dir)
        from epubforge.io import load_book

        b = load_book(paths.book_path)
        chapter_uid = b.chapters[0].uid
        assert chapter_uid is not None

        result = runner.invoke(
            app,
            [
                "editor",
                "render-prompt",
                str(work_dir),
                "--kind",
                "scanner",
                "--chapter",
                chapter_uid,
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        prompt = result.output

        # Should NOT contain extraction context section
        assert "Extraction context" not in prompt
        assert "render-page" not in prompt
