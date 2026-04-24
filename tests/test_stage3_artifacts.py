"""Tests for stage3_artifacts.py — manifest models, helpers, and file I/O."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from epubforge.stage3_artifacts import (
    EvidenceIndex,
    Stage3ActivePointer,
    Stage3ContractError,
    Stage3ExtractionResult,
    Stage3Manifest,
    Stage3Warning,
    activate_manifest_atomic,
    active_manifest_matches_desired,
    build_desired_stage3_manifest,
    load_active_stage3_manifest,
    resolve_manifest_paths,
    validate_stage3_artifact,
    write_artifact_manifest_atomic,
    _sha256_str,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_BASE_SETTINGS_VLM: dict = {
    "skip_vlm": False,
    "contract_version": 3,
    "vlm_dpi": 150,
    "max_vlm_batch_pages": 4,
    "enable_book_memory": True,
    "vlm_model": "gpt-4o",
    "vlm_base_url": None,
}

_BASE_SETTINGS_SKIP: dict = {
    "skip_vlm": True,
    "contract_version": 3,
    "vlm_dpi": None,
    "max_vlm_batch_pages": None,
    "enable_book_memory": False,
    "vlm_model": None,
    "vlm_base_url": None,
}


def _make_artifact_id(
    mode: Literal["vlm", "skip_vlm"] = "skip_vlm",
    source_pdf_sha256="aabbcc",
    raw_sha256="ddeeff",
    pages_sha256="112233",
    selected_pages=None,
    toc_pages=None,
    complex_pages=None,
    page_filter=None,
    settings=None,
) -> str:
    return build_desired_stage3_manifest(
        mode=mode,
        source_pdf_rel="source/source.pdf",
        source_pdf_sha256=source_pdf_sha256,
        raw_sha256=raw_sha256,
        pages_sha256=pages_sha256,
        selected_pages=selected_pages or [1, 2, 3],
        toc_pages=toc_pages or [],
        complex_pages=complex_pages or [],
        page_filter=page_filter,
        settings=settings or _BASE_SETTINGS_SKIP,
    )


def _make_manifest(
    tmp_path: Path,
    artifact_id: str = "abcd1234abcd1234",
    mode: Literal["vlm", "skip_vlm"] = "skip_vlm",
    unit_files: list[str] | None = None,
    sidecars: dict | None = None,
) -> Stage3Manifest:
    art_dir = f"03_extract/artifacts/{artifact_id}"
    if unit_files is None:
        unit_files = [f"{art_dir}/unit_0000.json"]
    if sidecars is None:
        sidecars = {
            "audit_notes": f"{art_dir}/audit_notes.json",
            "book_memory": f"{art_dir}/book_memory.json",
            "evidence_index": f"{art_dir}/evidence_index.json",
        }
    return Stage3Manifest(
        mode=mode,
        artifact_id=artifact_id,
        artifact_dir=art_dir,
        created_at="2026-04-24T00:00:00Z",
        raw_sha256="ddeeff",
        pages_sha256="112233",
        source_pdf="source/source.pdf",
        source_pdf_sha256="aabbcc",
        selected_pages=[1, 2, 3],
        toc_pages=[],
        complex_pages=[],
        page_filter=None,
        unit_files=unit_files,
        sidecars=sidecars,
        settings=_BASE_SETTINGS_SKIP,
    )


def _create_artifact_files(tmp_path: Path, manifest: Stage3Manifest) -> None:
    """Create all files referenced by the manifest so validation passes."""
    art_dir = tmp_path / "03_extract" / "artifacts" / manifest.artifact_id
    art_dir.mkdir(parents=True, exist_ok=True)
    for rel in manifest.unit_files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    for rel in manifest.sidecars.values():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: build_desired_stage3_manifest — artifact_id determinism and sensitivity
# ---------------------------------------------------------------------------


class TestBuildDesiredArtifactId:
    def test_deterministic_same_inputs(self) -> None:
        id1 = _make_artifact_id()
        id2 = _make_artifact_id()
        assert id1 == id2

    def test_length_is_16_hex_chars(self) -> None:
        aid = _make_artifact_id()
        assert len(aid) == 16
        assert all(c in "0123456789abcdef" for c in aid)

    def test_different_modes_produce_different_ids(self) -> None:
        vlm_id = _make_artifact_id(mode="vlm", settings=_BASE_SETTINGS_VLM)
        skip_id = _make_artifact_id(mode="skip_vlm", settings=_BASE_SETTINGS_SKIP)
        assert vlm_id != skip_id

    def test_different_page_filter_produce_different_ids(self) -> None:
        no_filter = _make_artifact_id(page_filter=None)
        with_filter = _make_artifact_id(page_filter=[1, 2, 3])
        assert no_filter != with_filter

    def test_different_page_filters_produce_different_ids(self) -> None:
        filter_a = _make_artifact_id(page_filter=[1, 2])
        filter_b = _make_artifact_id(page_filter=[3, 4])
        assert filter_a != filter_b

    def test_page_filter_order_does_not_matter(self) -> None:
        """Page filter should be sorted before serialisation."""
        id1 = _make_artifact_id(page_filter=[3, 1, 2])
        id2 = _make_artifact_id(page_filter=[1, 2, 3])
        assert id1 == id2

    def test_different_source_sha_produce_different_ids(self) -> None:
        id1 = _make_artifact_id(source_pdf_sha256="aaaaaa")
        id2 = _make_artifact_id(source_pdf_sha256="bbbbbb")
        assert id1 != id2

    def test_different_raw_sha_produce_different_ids(self) -> None:
        id1 = _make_artifact_id(raw_sha256="111111")
        id2 = _make_artifact_id(raw_sha256="222222")
        assert id1 != id2

    def test_different_pages_sha_produce_different_ids(self) -> None:
        id1 = _make_artifact_id(pages_sha256="aaaaaa")
        id2 = _make_artifact_id(pages_sha256="ffffff")
        assert id1 != id2

    def test_different_selected_pages_produce_different_ids(self) -> None:
        id1 = _make_artifact_id(selected_pages=[1, 2, 3])
        id2 = _make_artifact_id(selected_pages=[4, 5, 6])
        assert id1 != id2

    def test_different_toc_pages_produce_different_ids(self) -> None:
        id1 = _make_artifact_id(toc_pages=[])
        id2 = _make_artifact_id(toc_pages=[1])
        assert id1 != id2

    def test_different_complex_pages_produce_different_ids(self) -> None:
        id1 = _make_artifact_id(complex_pages=[])
        id2 = _make_artifact_id(complex_pages=[2])
        assert id1 != id2

    def test_different_settings_produce_different_ids(self) -> None:
        settings_a = dict(_BASE_SETTINGS_SKIP)
        settings_b = dict(_BASE_SETTINGS_SKIP, contract_version=99)
        id1 = _make_artifact_id(settings=settings_a)
        id2 = _make_artifact_id(settings=settings_b)
        assert id1 != id2

    def test_vlm_model_change_produces_different_id(self) -> None:
        s1 = dict(_BASE_SETTINGS_VLM, vlm_model="gpt-4o")
        s2 = dict(_BASE_SETTINGS_VLM, vlm_model="gemini-pro")
        id1 = build_desired_stage3_manifest(
            mode="vlm",
            source_pdf_rel="source/source.pdf",
            source_pdf_sha256="aa",
            raw_sha256="bb",
            pages_sha256="cc",
            selected_pages=[1],
            toc_pages=[],
            complex_pages=[],
            page_filter=None,
            settings=s1,
        )
        id2 = build_desired_stage3_manifest(
            mode="vlm",
            source_pdf_rel="source/source.pdf",
            source_pdf_sha256="aa",
            raw_sha256="bb",
            pages_sha256="cc",
            selected_pages=[1],
            toc_pages=[],
            complex_pages=[],
            page_filter=None,
            settings=s2,
        )
        assert id1 != id2


# ---------------------------------------------------------------------------
# Tests: write_artifact_manifest_atomic + activate_manifest_atomic +
#        load_active_stage3_manifest  roundtrip
# ---------------------------------------------------------------------------


class TestManifestRoundtrip:
    def test_write_creates_manifest_json(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        written = write_artifact_manifest_atomic(tmp_path, manifest)
        assert written.exists()
        assert written.name == "manifest.json"

    def test_manifest_json_is_valid_json(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        written = write_artifact_manifest_atomic(tmp_path, manifest)
        data = json.loads(written.read_text())
        assert data["schema_version"] == 3
        assert data["artifact_id"] == manifest.artifact_id

    def test_activate_writes_active_pointer(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)
        pointer_path = tmp_path / "03_extract" / "active_manifest.json"
        assert pointer_path.exists()

    def test_load_roundtrip(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        _create_artifact_files(tmp_path, manifest)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)

        pointer, loaded = load_active_stage3_manifest(tmp_path)
        assert pointer.active_artifact_id == manifest.artifact_id
        assert loaded.artifact_id == manifest.artifact_id
        assert loaded.mode == "skip_vlm"
        assert loaded.selected_pages == [1, 2, 3]

    def test_load_verifies_sha_matches(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)

        # Tamper with the manifest file.
        manifest_file = (
            tmp_path
            / "03_extract"
            / "artifacts"
            / manifest.artifact_id
            / "manifest.json"
        )
        manifest_file.write_text('{"tampered": true}', encoding="utf-8")

        with pytest.raises(Stage3ContractError, match="SHA-256 mismatch"):
            load_active_stage3_manifest(tmp_path)

    def test_activate_without_write_raises(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        # manifest.json was never written
        with pytest.raises(Stage3ContractError, match="manifest.json not found"):
            activate_manifest_atomic(tmp_path, manifest)


# ---------------------------------------------------------------------------
# Tests: active_manifest_matches_desired
# ---------------------------------------------------------------------------


class TestActiveManifestMatchesDesired:
    def test_no_pointer_returns_false(self, tmp_path: Path) -> None:
        assert not active_manifest_matches_desired(tmp_path, "any_id")

    def test_matching_id_returns_true(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)
        assert active_manifest_matches_desired(tmp_path, manifest.artifact_id)

    def test_different_id_returns_false(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)
        assert not active_manifest_matches_desired(tmp_path, "0000000000000000")

    def test_corrupt_pointer_returns_false(self, tmp_path: Path) -> None:
        pointer_path = tmp_path / "03_extract" / "active_manifest.json"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("not json", encoding="utf-8")
        assert not active_manifest_matches_desired(tmp_path, "any_id")


# ---------------------------------------------------------------------------
# Tests: validate_stage3_artifact
# ---------------------------------------------------------------------------


class TestValidateStage3Artifact:
    def test_valid_artifact_passes(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        _create_artifact_files(tmp_path, manifest)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)
        # Should not raise.
        validate_stage3_artifact(tmp_path, manifest)

    def test_missing_artifact_dir_raises(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        # No files created, no directory.
        with pytest.raises(Stage3ContractError, match="Artifact directory missing"):
            validate_stage3_artifact(tmp_path, manifest)

    def test_missing_manifest_json_raises(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        # Create artifact dir but no manifest.json.
        art_dir = tmp_path / "03_extract" / "artifacts" / manifest.artifact_id
        art_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(Stage3ContractError, match="manifest.json missing"):
            validate_stage3_artifact(tmp_path, manifest)

    def test_missing_unit_file_raises(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        _create_artifact_files(tmp_path, manifest)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)

        # Delete a unit file.
        unit_path = tmp_path / manifest.unit_files[0]
        unit_path.unlink()

        with pytest.raises(Stage3ContractError, match="Unit file missing"):
            validate_stage3_artifact(tmp_path, manifest)

    def test_missing_sidecar_raises(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        _create_artifact_files(tmp_path, manifest)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)

        # Delete a sidecar file.
        sidecar_path = tmp_path / manifest.sidecars["audit_notes"]
        sidecar_path.unlink()

        with pytest.raises(Stage3ContractError, match="Sidecar 'audit_notes' missing"):
            validate_stage3_artifact(tmp_path, manifest)

    def test_active_pointer_sha_mismatch_raises(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        _create_artifact_files(tmp_path, manifest)
        write_artifact_manifest_atomic(tmp_path, manifest)
        activate_manifest_atomic(tmp_path, manifest)

        # Tamper with manifest.json but preserve the directory structure.
        manifest_file = (
            tmp_path
            / "03_extract"
            / "artifacts"
            / manifest.artifact_id
            / "manifest.json"
        )
        original_text = manifest_file.read_text()
        # Append whitespace to change sha but keep valid JSON-ish
        manifest_file.write_text(original_text + " ", encoding="utf-8")

        with pytest.raises(Stage3ContractError, match="SHA-256 mismatch"):
            validate_stage3_artifact(tmp_path, manifest)


# ---------------------------------------------------------------------------
# Tests: atomicity — previous active pointer preserved on extraction failure
# ---------------------------------------------------------------------------


class TestActivationAtomicity:
    def test_previous_pointer_preserved_when_not_activated(
        self, tmp_path: Path
    ) -> None:
        """If a new manifest is written but activate_manifest_atomic is never
        called (simulating extraction failure), the old active pointer must be
        unchanged."""
        old_manifest = _make_manifest(tmp_path, artifact_id="aaaa0000aaaa0000")
        _create_artifact_files(tmp_path, old_manifest)
        write_artifact_manifest_atomic(tmp_path, old_manifest)
        activate_manifest_atomic(tmp_path, old_manifest)

        # Simulate a new extraction attempt: write new manifest but do NOT activate.
        new_manifest = _make_manifest(tmp_path, artifact_id="bbbb1111bbbb1111")
        _create_artifact_files(tmp_path, new_manifest)
        write_artifact_manifest_atomic(tmp_path, new_manifest)
        # Intentionally skip activate_manifest_atomic(tmp_path, new_manifest)

        pointer, loaded = load_active_stage3_manifest(tmp_path)
        assert pointer.active_artifact_id == "aaaa0000aaaa0000"
        assert loaded.artifact_id == "aaaa0000aaaa0000"

    def test_activate_replaces_old_pointer(self, tmp_path: Path) -> None:
        """Calling activate_manifest_atomic twice replaces the pointer each time."""
        first = _make_manifest(tmp_path, artifact_id="first00000000000")
        _create_artifact_files(tmp_path, first)
        write_artifact_manifest_atomic(tmp_path, first)
        activate_manifest_atomic(tmp_path, first)

        second = _make_manifest(tmp_path, artifact_id="second0000000000")
        _create_artifact_files(tmp_path, second)
        write_artifact_manifest_atomic(tmp_path, second)
        activate_manifest_atomic(tmp_path, second)

        pointer, loaded = load_active_stage3_manifest(tmp_path)
        assert pointer.active_artifact_id == "second0000000000"
        assert loaded.artifact_id == "second0000000000"


# ---------------------------------------------------------------------------
# Tests: half-written artifact is not loadable
# ---------------------------------------------------------------------------


class TestHalfWrittenArtifact:
    def test_no_manifest_json_raises(self, tmp_path: Path) -> None:
        """An artifact directory exists but manifest.json was never written."""
        artifact_id = "half0000half0000"
        art_dir = tmp_path / "03_extract" / "artifacts" / artifact_id
        art_dir.mkdir(parents=True, exist_ok=True)

        # Write a pointer that references this artifact but has a made-up sha.
        pointer = Stage3ActivePointer(
            active_artifact_id=artifact_id,
            manifest_path=f"03_extract/artifacts/{artifact_id}/manifest.json",
            manifest_sha256="0" * 64,
            activated_at="2026-04-24T00:00:00Z",
        )
        pointer_path = tmp_path / "03_extract" / "active_manifest.json"
        pointer_path.write_text(pointer.model_dump_json(), encoding="utf-8")

        with pytest.raises(Stage3ContractError):
            load_active_stage3_manifest(tmp_path)

    def test_no_active_pointer_raises(self, tmp_path: Path) -> None:
        with pytest.raises(Stage3ContractError, match="No active manifest pointer"):
            load_active_stage3_manifest(tmp_path)


# ---------------------------------------------------------------------------
# Tests: resolve_manifest_paths
# ---------------------------------------------------------------------------


class TestResolveManifestPaths:
    def test_resolves_all_keys(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        paths = resolve_manifest_paths(tmp_path, manifest)

        assert "artifact_dir" in paths
        assert "source_pdf" in paths
        assert "unit_files" in paths
        assert "audit_notes" in paths
        assert "book_memory" in paths
        assert "evidence_index" in paths

    def test_artifact_dir_is_absolute(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        paths = resolve_manifest_paths(tmp_path, manifest)
        assert paths["artifact_dir"].is_absolute()

    def test_unit_files_is_list(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        paths = resolve_manifest_paths(tmp_path, manifest)
        assert isinstance(paths["unit_files"], list)
        assert len(paths["unit_files"]) == 1


# ---------------------------------------------------------------------------
# Tests: model construction and validation
# ---------------------------------------------------------------------------


class TestModelConstruction:
    def test_stage3_warning_defaults(self) -> None:
        w = Stage3Warning(message="something went wrong")
        assert w.severity == "warning"
        assert w.page is None

    def test_stage3_contract_error_is_runtime_error(self) -> None:
        err = Stage3ContractError("test")
        assert isinstance(err, RuntimeError)

    def test_evidence_index_defaults(self) -> None:
        ei = EvidenceIndex(
            artifact_id="abc",
            mode="skip_vlm",
            source_pdf="source/source.pdf",
        )
        assert ei.schema_version == 3
        assert ei.pages == {}
        assert ei.refs == {}

    def test_stage3_extraction_result(self, tmp_path: Path) -> None:
        result = Stage3ExtractionResult(
            mode="skip_vlm",
            unit_files=[tmp_path / "unit_0000.json"],
            audit_notes_path=tmp_path / "audit_notes.json",
            book_memory_path=tmp_path / "book_memory.json",
            evidence_index_path=tmp_path / "evidence_index.json",
            selected_pages=[1, 2],
            toc_pages=[],
            complex_pages=[2],
        )
        assert result.warnings == []
        assert result.mode == "skip_vlm"

    def test_stage3_manifest_roundtrip(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        serialised = manifest.model_dump_json()
        loaded = Stage3Manifest.model_validate_json(serialised)
        assert loaded.artifact_id == manifest.artifact_id
        assert loaded.settings["skip_vlm"] is True

    def test_stage3_active_pointer_schema_version(self) -> None:
        p = Stage3ActivePointer(
            active_artifact_id="abc123",
            manifest_path="03_extract/artifacts/abc123/manifest.json",
            manifest_sha256="x" * 64,
            activated_at="2026-04-24T00:00:00Z",
        )
        assert p.schema_version == 3


# ---------------------------------------------------------------------------
# Tests: _sha256_str utility
# ---------------------------------------------------------------------------


class TestSha256Str:
    def test_same_input_same_output(self) -> None:
        assert _sha256_str("hello") == _sha256_str("hello")

    def test_different_input_different_output(self) -> None:
        assert _sha256_str("hello") != _sha256_str("world")

    def test_length_is_64_hex(self) -> None:
        result = _sha256_str("test")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)
