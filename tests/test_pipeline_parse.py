"""Focused tests for Stage 1 page limits and segmented archives."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import threading
import time
import zipfile

from filelock import FileLock
import pymupdf
import pytest

from epubforge import pipeline
from epubforge.config import Config, MineruSettings, RuntimeSettings
from epubforge.mineru import MineruDownloadResult


def _write_pdf(path: Path, page_count: int) -> None:
    document = pymupdf.open()
    try:
        for page_number in range(1, page_count + 1):
            page = document.new_page()
            page.insert_text((72, 72), f"Page {page_number}")
        document.save(str(path))
    finally:
        document.close()


class _FakeMineruClient:
    instances: list[_FakeMineruClient] = []

    def __init__(self, _settings: MineruSettings) -> None:
        self.calls: list[tuple[str, int, Path]] = []
        self.responses: list[bytes] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> _FakeMineruClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def process_file(self, file_path: Path, zip_path: Path) -> MineruDownloadResult:
        path = Path(file_path)
        with pymupdf.open(str(path)) as document:
            page_count = document.page_count
        self.calls.append((path.name, page_count, Path(zip_path)))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as archive:
            archive.writestr("full.md", f"response {len(self.calls)}")
            archive.writestr("layout.json", json.dumps({"pages": page_count}))
        response = buffer.getvalue()
        self.responses.append(response)
        Path(zip_path).write_bytes(response)
        return MineruDownloadResult(
            batch_id=f"batch-{len(self.calls)}",
            file_name=path.name,
            zip_path=Path(zip_path),
        )


def _config(work_dir: Path) -> Config:
    return Config(
        mineru=MineruSettings(api_key="secret"),
        runtime=RuntimeSettings(work_dir=work_dir),
    )


def _seed_published_stage1(work: Path) -> dict[Path, bytes]:
    source_dir = work / "source"
    source_dir.mkdir(parents=True)
    source_bytes = b"previous source"
    source_meta = (
        json.dumps(
            {
                "source_pdf": "source/source.pdf",
                "original_pdf_abs": str(work / "original.pdf"),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "size_bytes": len(source_bytes),
                "copied_at": "2026-01-01T00:00:00+00:00",
            },
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    files = {
        source_dir / "source.pdf": source_bytes,
        source_dir / "source_meta.json": source_meta,
        work / "01_raw.zip": b"previous output",
    }
    for path, content in files.items():
        path.write_bytes(content)
    return files


def test_stage1_capability_reports_non_posix_without_host_switch(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work" / "book"
    work.mkdir(parents=True)

    error = pipeline._stage1_capability_error(work, platform_name="nt")

    assert error is not None
    assert "POSIX" in error
    assert "directory fsync" in error


def test_stage1_capability_failure_precedes_source_and_mineru(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)
    monkeypatch.setattr(
        pipeline,
        "_stage1_capability_error",
        lambda _work: "requires POSIX directory fsync support",
    )
    monkeypatch.setattr(
        pipeline,
        "MineruClient",
        lambda *args: (_ for _ in ()).throw(AssertionError("MinerU must not run")),
    )

    with pytest.raises(RuntimeError, match="POSIX directory fsync"):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)

    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not list(work.glob(".stage1-*"))


def test_page_ranges_cover_boundary_counts() -> None:
    assert pipeline._page_ranges(200) == ((1, 200),)
    assert pipeline._page_ranges(201) == ((1, 200), (201, 201))
    assert pipeline._page_ranges(1000) == (
        (1, 200),
        (201, 400),
        (401, 600),
        (601, 800),
        (801, 1000),
    )


def test_parse_keeps_single_response_for_200_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 200)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)

    pipeline.run_parse(pdf_path, _config(tmp_path / "work"))

    client = _FakeMineruClient.instances[0]
    assert [(name, count) for name, count, _ in client.calls] == [("source.pdf", 200)]
    output = tmp_path / "work" / "book" / "01_raw.zip"
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["full.md", "layout.json"]
    assert not list(output.parent.glob(".stage1-*"))


def test_parse_copies_source_to_a_distinct_inode_and_records_matching_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)

    pipeline.run_parse(pdf_path, _config(tmp_path / "work"))

    source_path = tmp_path / "work" / "book" / "source" / "source.pdf"
    source_meta = json.loads(
        (source_path.parent / "source_meta.json").read_text(encoding="utf-8")
    )
    assert (source_path.stat().st_dev, source_path.stat().st_ino) != (
        pdf_path.stat().st_dev,
        pdf_path.stat().st_ino,
    )
    assert source_meta["sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert source_meta["size_bytes"] == source_path.stat().st_size


def test_original_mutation_after_success_does_not_change_published_source_or_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")

    pipeline.run_parse(pdf_path, cfg)
    work = tmp_path / "work" / "book"
    published_source = work / "source" / "source.pdf"
    published_meta = work / "source" / "source_meta.json"
    source_before = published_source.read_bytes()
    meta_before = published_meta.read_bytes()

    pdf_path.write_bytes(pdf_path.read_bytes() + b"changed after Stage 1")

    pipeline.run_parse(pdf_path, cfg, force=False)

    assert published_source.read_bytes() == source_before
    assert published_meta.read_bytes() == meta_before


def test_reuse_after_original_deletion_does_not_change_published_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")

    pipeline.run_parse(pdf_path, cfg)
    work = tmp_path / "work" / "book"
    published_source = work / "source" / "source.pdf"
    published_meta = work / "source" / "source_meta.json"
    source_before = published_source.read_bytes()
    meta_before = published_meta.read_bytes()
    pdf_path.unlink()

    pipeline.run_parse(pdf_path, cfg, force=False)

    assert published_source.read_bytes() == source_before
    assert published_meta.read_bytes() == meta_before


def test_force_rerun_accepts_published_source_as_input_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    published_source = work / "source" / "source.pdf"
    source_before = published_source.read_bytes()
    monkeypatch.setattr(Config, "book_work_dir", lambda _self, _pdf_path: work)

    pipeline.run_parse(published_source, cfg, force=True)

    assert published_source.read_bytes() == source_before
    assert not list(work.glob(".stage1-*"))


def test_concurrent_source_mutation_keeps_previous_published_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    previous_files = {
        path: path.read_bytes()
        for path in (
            work / "source" / "source.pdf",
            work / "source" / "source_meta.json",
            work / "01_raw.zip",
        )
    }
    real_read = pipeline.os.read
    mutated = False

    def mutate_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            pdf_path.write_bytes(pdf_path.read_bytes() + b"concurrent mutation")
        return chunk

    monkeypatch.setattr(pipeline.os, "read", mutate_after_first_read)
    with pytest.raises(RuntimeError, match="concurrent mutation"):
        pipeline.run_parse(pdf_path, cfg, force=True)

    assert mutated
    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not list(work.glob(".stage1-*"))


@pytest.mark.parametrize("mutation", ["replace", "delete"])
def test_atomic_source_path_mutation_during_copy_keeps_previous_published_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    previous_files = {
        path: path.read_bytes()
        for path in (
            work / "source" / "source.pdf",
            work / "source" / "source_meta.json",
            work / "01_raw.zip",
        )
    }
    real_read = pipeline.os.read
    mutated = False

    def mutate_path_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            if mutation == "replace":
                replacement = tmp_path / "replacement.pdf"
                replacement.write_bytes(pdf_path.read_bytes())
                os.replace(replacement, pdf_path)
            else:
                pdf_path.unlink()
        return chunk

    monkeypatch.setattr(pipeline.os, "read", mutate_path_after_first_read)
    with pytest.raises(RuntimeError, match="concurrent mutation"):
        pipeline.run_parse(pdf_path, cfg, force=True)

    assert mutated
    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not list(work.glob(".stage1-*"))


def test_same_size_source_mutation_with_restored_mtime_fails_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    previous_files = {
        path: path.read_bytes()
        for path in (
            work / "source" / "source.pdf",
            work / "source" / "source_meta.json",
            work / "01_raw.zip",
        )
    }
    original_stat = pdf_path.stat()
    source_bytes = bytearray(pdf_path.read_bytes())
    source_bytes[0] ^= 1
    real_read = pipeline.os.read
    mutated = False

    def mutate_content_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            pdf_path.write_bytes(source_bytes)
            os.utime(
                pdf_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
        return chunk

    monkeypatch.setattr(pipeline.os, "read", mutate_content_after_first_read)
    with pytest.raises(RuntimeError, match="concurrent mutation"):
        pipeline.run_parse(pdf_path, cfg, force=True)

    assert mutated
    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not list(work.glob(".stage1-*"))


def test_non_force_skip_detects_published_source_hash_and_size_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    source_path = work / "source" / "source.pdf"
    source_path.write_bytes(source_path.read_bytes() + b"drift")

    with pytest.raises(RuntimeError, match="--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)


def test_non_force_skip_detects_same_size_published_source_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    source_path = tmp_path / "work" / "book" / "source" / "source.pdf"
    source_bytes = bytearray(source_path.read_bytes())
    source_bytes[0] ^= 1
    source_path.write_bytes(source_bytes)

    with pytest.raises(RuntimeError, match=r"SHA-256.*--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)


def test_non_force_skip_rejects_metadata_hash_mismatch_without_size_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    source_path = work / "source" / "source.pdf"
    source_meta = work / "source" / "source_meta.json"
    metadata = json.loads(source_meta.read_text(encoding="utf-8"))
    metadata["sha256"] = "0" * 64
    source_meta.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"SHA-256.*--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)
    assert source_path.stat().st_size == metadata["size_bytes"]


def test_non_force_skip_rejects_raw_archive_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    raw_archive = work / "01_raw.zip"
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(raw_archive.read_bytes())
    raw_archive.unlink()
    try:
        raw_archive.symlink_to(replacement)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match=r"raw archive.*symlink.*--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)


def test_stage1_backup_rejects_fifo_target_without_blocking(
    tmp_path: Path,
) -> None:
    if os.name != "posix" or not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    work = tmp_path / "work" / "book"
    source_dir = work / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "source.pdf").write_bytes(b"published source")
    (source_dir / "source_meta.json").write_bytes(b"{}\n")
    raw_archive = work / "01_raw.zip"
    try:
        os.mkfifo(raw_archive)
    except OSError:
        pytest.skip("FIFO creation is unavailable")
    staging_dir = work / ".stage1-fifo-test"
    staging_dir.mkdir()

    with pytest.raises(RuntimeError, match=r"non-regular file.*--force-rerun"):
        pipeline._begin_stage1_transaction(work, staging_dir)

    assert raw_archive.is_fifo()
    assert not list(work.glob(".stage1-recovery-*"))


def test_non_force_skip_rejects_published_source_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    source_path = work / "source" / "source.pdf"
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(source_path.read_bytes())
    source_path.unlink()
    try:
        source_path.symlink_to(replacement)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match=r"symlink.*--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)


def test_non_force_skip_rejects_source_metadata_symlink_or_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")
    pipeline.run_parse(pdf_path, cfg)

    work = tmp_path / "work" / "book"
    source_meta = work / "source" / "source_meta.json"
    replacement = tmp_path / "replacement-meta.json"
    replacement.write_bytes(source_meta.read_bytes())
    source_meta.unlink()
    try:
        source_meta.symlink_to(replacement)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match=r"symlink.*--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)

    source_meta.unlink()
    valid_metadata = json.loads(replacement.read_text(encoding="utf-8"))
    valid_metadata["source_pdf"] = "source/other.pdf"
    source_meta.write_text(json.dumps(valid_metadata), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected source_pdf.*--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)

    source_meta.write_bytes(replacement.read_bytes())
    source_meta.unlink()
    source_meta.write_text("{malformed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="--force-rerun"):
        pipeline.run_parse(pdf_path, cfg, force=False)


def test_parse_packages_ordered_201_page_responses_without_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 201)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    cfg = _config(tmp_path / "work")

    pipeline.run_parse(pdf_path, cfg)
    pipeline.run_parse(pdf_path, cfg, force=True)

    client = _FakeMineruClient.instances[-1]
    assert [(count) for _, count, _ in client.calls] == [200, 1]
    work = tmp_path / "work" / "book"
    output = work / "01_raw.zip"
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "segments/segment-001-pages-0001-0200.zip",
            "segments/segment-002-pages-0201-0201.zip",
        ]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["source_page_count"] == 201
        assert [
            (segment["index"], segment["first_page"], segment["last_page"])
            for segment in manifest["segments"]
        ] == [(1, 1, 200), (2, 201, 201)]
        response_archives = [
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("segments/")
        ]
    assert response_archives == client.responses
    assert [segment["response_sha256"] for segment in manifest["segments"]] == [
        hashlib.sha256(response).hexdigest() for response in client.responses
    ]
    source_path = work / "source" / "source.pdf"
    meta_path = work / "source" / "source_meta.json"
    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert source_meta["sha256"] == source_sha256
    assert manifest["source_pdf"] == "source/source.pdf"
    assert manifest["source_pdf_sha256"] == source_sha256
    with pymupdf.open(str(source_path)) as source:
        assert source.page_count == 201
    assert not list(work.glob(".stage1-*"))


def test_split_pdf_1000_pages_creates_five_real_segments(tmp_path: Path) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1000)
    segments_dir = tmp_path / "segments"
    segments_dir.mkdir()

    segments = pipeline._split_pdf(
        pdf_path, pipeline._pdf_page_count(pdf_path), segments_dir
    )

    assert len(segments) == 5
    assert [(segment.first_page, segment.last_page) for segment in segments] == [
        (1, 200),
        (201, 400),
        (401, 600),
        (601, 800),
        (801, 1000),
    ]
    for segment in segments:
        with pymupdf.open(str(segment.path)) as document:
            assert document.page_count == segment.last_page - segment.first_page + 1
            assert document.page_count <= pipeline.MAX_MINERU_FILE_PAGES


def test_parse_rejects_1001_pages_before_constructing_mineru_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1001)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)

    def fail_if_constructed(*args: object, **kwargs: object) -> object:
        raise AssertionError("MinerU client must not be constructed")

    monkeypatch.setattr(pipeline, "MineruClient", fail_if_constructed)

    with pytest.raises(RuntimeError, match="at most 1000 pages"):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)

    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not list(work.glob(".stage1-*"))


def test_segment_failure_keeps_previous_archive_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 201)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)

    class FailingMineruClient(_FakeMineruClient):
        def process_file(self, file_path: Path, zip_path: Path) -> MineruDownloadResult:
            if len(self.calls) == 1:
                raise RuntimeError("simulated second-segment failure")
            return super().process_file(file_path, zip_path)

    FailingMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", FailingMineruClient)

    with pytest.raises(RuntimeError, match="second-segment failure"):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)

    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not list(work.glob(".stage1-*"))
    assert not list(work.glob(".01_raw.zip.*.tmp"))


@pytest.mark.parametrize("failure_call", range(1, 4))
def test_keyboard_interrupt_at_each_publish_boundary_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    real_replace = pipeline._replace_stage1_target
    replace_calls = 0
    injected = False

    def interrupting_replace(source: Path, destination: Path) -> None:
        nonlocal injected, replace_calls
        replace_calls += 1
        if replace_calls == failure_call:
            injected = True
            raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr(pipeline, "_replace_stage1_target", interrupting_replace)

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)

    assert injected
    assert replace_calls >= failure_call
    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not list(work.glob(".stage1-*"))
    assert not list(work.glob(".stage1-recovery-*"))


def test_first_run_publish_interrupt_restores_absent_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)

    def interrupt_publish(source: Path, destination: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "_replace_stage1_target", interrupt_publish)

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=False)

    work = tmp_path / "work" / "book"
    assert not (work / "source" / "source.pdf").exists()
    assert not (work / "source" / "source_meta.json").exists()
    assert not (work / "01_raw.zip").exists()
    assert not (work / ".stage1-transaction.json").exists()
    assert not list(work.glob(".stage1-recovery-*"))
    assert not list(work.glob(".stage1-*"))


def test_unfinished_transaction_is_recovered_before_non_force_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)
    crashed_staging = work / ".stage1-crashed"
    crashed_staging.mkdir()
    marker_path, recovery_dir = pipeline._begin_stage1_transaction(
        work, crashed_staging
    )
    replacement = crashed_staging / "replacement-source.pdf"
    replacement.write_bytes(b"partial new source")
    pipeline.os.replace(replacement, work / "source" / "source.pdf")

    def fail_if_constructed(*args: object, **kwargs: object) -> object:
        raise AssertionError("non-force recovery should skip without MinerU")

    monkeypatch.setattr(pipeline, "MineruClient", fail_if_constructed)

    pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=False)

    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not marker_path.exists()
    assert not recovery_dir.exists()
    assert not crashed_staging.exists()


def test_recovery_retries_after_restore_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)
    staging_dir = work / ".stage1-interrupted"
    staging_dir.mkdir()
    pipeline._begin_stage1_transaction(work, staging_dir)
    partial_source = staging_dir / "partial-source.pdf"
    partial_source.write_bytes(b"partial")
    pipeline.os.replace(partial_source, work / "source" / "source.pdf")
    real_fsync_file = pipeline._fsync_file
    failed = True

    def fail_once(path: Path) -> None:
        nonlocal failed
        if failed and path.name.startswith(".restore-"):
            failed = False
            raise RuntimeError("simulated recovery interruption")
        real_fsync_file(path)

    monkeypatch.setattr(pipeline, "_fsync_file", fail_once)
    with pytest.raises(RuntimeError, match="recovery interruption"):
        pipeline._recover_stage1_transaction(work)
    assert (work / ".stage1-transaction.json").exists()

    monkeypatch.setattr(pipeline, "_fsync_file", real_fsync_file)
    assert pipeline._recover_stage1_transaction(work) is True
    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert not (work / ".stage1-transaction.json").exists()
    assert not list(work.glob(".stage1-recovery-*"))
    assert not staging_dir.exists()


def test_rollback_failure_preserves_recovery_and_staging_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    real_replace = pipeline._replace_stage1_target
    replace_calls = 0

    def fail_publish_and_rollback(source: Path, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr(pipeline, "_replace_stage1_target", fail_publish_and_rollback)
    real_fsync_file = pipeline._fsync_file

    def fail_restore_sync(path: Path) -> None:
        if path.name.startswith(".restore-"):
            raise RuntimeError("simulated restore fsync failure")
        real_fsync_file(path)

    monkeypatch.setattr(pipeline, "_fsync_file", fail_restore_sync)

    with pytest.raises(RuntimeError, match="recovery failed") as exc_info:
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)

    assert "recover using marker" in str(exc_info.value)
    assert (work / "01_raw.zip").read_bytes() == previous_files[work / "01_raw.zip"]
    recovery_dirs = list(work.glob(".stage1-recovery-*"))
    staging_dirs = list(work.glob(".stage1-*"))
    assert recovery_dirs
    assert staging_dirs
    assert any(path != recovery_dirs[0] for path in staging_dirs)
    assert (work / ".stage1-transaction.json").exists()


def test_tampered_marker_rejects_staging_path_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)
    staging_dir = work / ".stage1-tampered"
    staging_dir.mkdir()
    marker_path, recovery_dir = pipeline._begin_stage1_transaction(work, staging_dir)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["staging_dir"] = "source"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(pipeline, "MineruClient", lambda *args: None)

    with pytest.raises(RuntimeError, match="direct"):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=False)

    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert marker_path.exists()
    assert recovery_dir.exists()
    assert staging_dir.exists()
    assert (work / "source").is_dir()


def test_symlink_source_parent_is_rejected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    work.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    try:
        (work / "source").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    monkeypatch.setattr(
        pipeline,
        "MineruClient",
        lambda *args: (_ for _ in ()).throw(AssertionError("MinerU must not run")),
    )

    with pytest.raises(RuntimeError, match="target parent"):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)
    assert list(external.iterdir()) == []


def test_orphan_stage1_directories_are_cleaned_without_touching_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    _seed_published_stage1(work)
    orphan_staging = work / ".stage1-orphan"
    orphan_recovery = work / ".stage1-recovery-orphan"
    orphan_staging.mkdir()
    orphan_recovery.mkdir()
    (orphan_staging / "data").write_text("staging", encoding="utf-8")
    (orphan_recovery / "data").write_text("recovery", encoding="utf-8")
    keep_file = work / ".stage1-keep.txt"
    keep_file.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(pipeline, "MineruClient", lambda *args: None)

    pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=False)

    assert not orphan_staging.exists()
    assert not orphan_recovery.exists()
    assert keep_file.read_text(encoding="utf-8") == "keep"


def test_marker_removal_failure_preserves_recovery_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    work = tmp_path / "work" / "book"
    previous_files = _seed_published_stage1(work)
    _FakeMineruClient.instances.clear()
    monkeypatch.setattr(pipeline, "MineruClient", _FakeMineruClient)
    real_unlink = Path.unlink

    def fail_marker_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name == pipeline._STAGE1_TRANSACTION_NAME:
            raise OSError("simulated marker removal failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_marker_unlink)

    with pytest.raises(RuntimeError, match="recovery failed"):
        pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)

    assert {path: path.read_bytes() for path in previous_files} == previous_files
    assert (work / ".stage1-transaction.json").exists()
    assert list(work.glob(".stage1-recovery-*"))
    assert list(work.glob(".stage1-*"))


def test_stage1_lock_serializes_concurrent_force_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "book.pdf"
    _write_pdf(pdf_path, 1)
    _FakeMineruClient.instances.clear()
    first_started = threading.Event()
    release_first = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    class BlockingMineruClient(_FakeMineruClient):
        def process_file(self, file_path: Path, zip_path: Path) -> MineruDownloadResult:
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["calls"] += 1
                call_number = state["calls"]
            try:
                if call_number == 1:
                    first_started.set()
                    assert release_first.wait(2)
                return super().process_file(file_path, zip_path)
            finally:
                with state_lock:
                    state["active"] -= 1

    monkeypatch.setattr(pipeline, "MineruClient", BlockingMineruClient)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            pipeline.run_parse(pdf_path, _config(tmp_path / "work"), force=True)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert first_started.wait(2)
    second.start()
    time.sleep(0.1)
    with state_lock:
        assert state["calls"] == 1
        assert state["max_active"] == 1
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert state["calls"] == 2
    assert state["max_active"] == 1


def test_stage1_lock_timeout_names_lock_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work" / "book"
    work.mkdir(parents=True)
    lock_path = work / ".stage1.lock"
    held_lock = FileLock(str(lock_path))
    held_lock.acquire(timeout=0.1)
    monkeypatch.setattr(pipeline, "_STAGE1_LOCK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(pipeline, "_STAGE1_LOCK_POLL_SECONDS", 0.001)
    try:
        with pytest.raises(
            RuntimeError,
            match=r"Timed out acquiring Stage 1 lock .*\.stage1\.lock after 0\.01s",
        ):
            with pipeline._stage1_lock(work):
                pass
    finally:
        held_lock.release()
