"""Pipeline orchestration for the five-stage chapter workflow."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
from filelock import FileLock, Timeout

from epubforge.config import Config
from epubforge.llm.client import LLMClient
from epubforge.mineru import MineruClient, MineruDownloadResult
from epubforge.observability import get_tracker, stage_timer
from epubforge.strict_json import StrictJsonError, read_json_document

log = logging.getLogger(__name__)

MAX_STAGE1_PAGES = 1000
MAX_MINERU_FILE_PAGES = 200
_ARCHIVE_COPY_BUFFER_SIZE = 1024 * 1024
_STAGE1_LOCK_TIMEOUT_SECONDS = 300.0
_STAGE1_LOCK_POLL_SECONDS = 0.05
_STAGE1_TRANSACTION_NAME = ".stage1-transaction.json"
_STAGE1_STAGING_PREFIX = ".stage1-"
_STAGE1_RECOVERY_PREFIX = ".stage1-recovery-"
_STAGE1_TARGET_NAMES = (
    "source/source.pdf",
    "source/source_meta.json",
    "01_raw.zip",
)


@dataclass(frozen=True)
class _PageSegment:
    index: int
    first_page: int
    last_page: int
    path: Path


class _Stage1PublishRecoveryError(RuntimeError):
    """Raised when Stage 1 publication cannot restore its previous result."""

    def __init__(
        self,
        message: str,
        marker_path: Path,
        recovery_dir: Path,
        staging_dir: Path,
    ) -> None:
        super().__init__(message)
        self.marker_path = marker_path
        self.recovery_dir = recovery_dir
        self.staging_dir = staging_dir


@contextmanager
def _stage1_lock(work: Path) -> Iterator[None]:
    """Serialize Stage 1 runs for one book with a cross-platform file lock."""
    lock_path = work / ".stage1.lock"
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(
            timeout=_STAGE1_LOCK_TIMEOUT_SECONDS,
            poll_interval=_STAGE1_LOCK_POLL_SECONDS,
        )
    except Timeout as exc:
        raise RuntimeError(
            f"Timed out acquiring Stage 1 lock {lock_path} after "
            f"{_STAGE1_LOCK_TIMEOUT_SECONDS:g}s"
        ) from exc
    try:
        yield
    finally:
        lock.release()


def _stage_path(work: Path, name: str) -> Path:
    return work / name


def _require_stage_file(work: Path, relative_path: str, stage: int) -> Path:
    path = work / relative_path
    if not path.is_file():
        raise RuntimeError(
            f"Stage {stage} requires {relative_path} to exist in {work}. "
            "Run earlier pipeline stages first."
        )
    return path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_regular_fd(path: Path, label: str) -> int:
    """Open one regular file descriptor without following the final symlink."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(
            f"Cannot safely open {label}: POSIX O_NOFOLLOW is unavailable"
        )
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(
                f"{label} is a symlink and cannot be read: {path}"
            ) from exc
        raise RuntimeError(f"Cannot safely open {label}: {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"{label} is not a regular file: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _sha256_fd(descriptor: int, label: str) -> str:
    digest = hashlib.sha256()
    while True:
        try:
            chunk = os.read(descriptor, _ARCHIVE_COPY_BUFFER_SIZE)
        except OSError as exc:
            raise RuntimeError(f"Cannot read {label}: {exc}") from exc
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _same_source_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def _write_all(descriptor: int, data: bytes, label: str) -> None:
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except OSError as exc:
            raise RuntimeError(f"Cannot write {label}: {exc}") from exc
        if written <= 0:
            raise RuntimeError(f"Cannot write {label}: write made no progress")
        remaining = remaining[written:]


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as file_handle:
            os.fsync(file_handle.fileno())
    except OSError as exc:
        raise RuntimeError(f"Cannot durably sync Stage 1 file {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Cannot durably sync Stage 1 directory: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            f"Filesystem does not support durable Stage 1 directory sync for {path}: {exc}"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot durably sync Stage 1 directory {path}: {exc}"
        ) from exc
    finally:
        os.close(descriptor)


def _stage1_capability_error(
    work: Path, *, platform_name: str | None = None
) -> str | None:
    runtime_name = platform_name or os.name
    if runtime_name != "posix":
        return (
            "Stage 1 crash-consistent publication requires a POSIX runtime "
            f"with directory fsync support; current runtime is {runtime_name!r}"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(work, flags)
    except OSError as exc:
        return (
            "Stage 1 crash-consistent publication requires a filesystem that "
            f"supports opening work directories for fsync ({work}): {exc}"
        )
    try:
        os.fsync(descriptor)
    except OSError as exc:
        return (
            "Stage 1 crash-consistent publication requires directory fsync "
            f"support for {work}: {exc}"
        )
    finally:
        os.close(descriptor)
    return None


def _require_stage1_capabilities(work: Path) -> None:
    error = _stage1_capability_error(work)
    if error is not None:
        raise RuntimeError(error)


def _pdf_page_count(pdf_path: Path) -> int:
    """Read the complete PDF page count before starting Stage 1 requests."""
    try:
        with pymupdf.open(str(pdf_path)) as document:
            page_count = document.page_count
    except Exception as exc:
        raise RuntimeError(
            f"Unable to inspect PDF page count for {pdf_path}: {exc}"
        ) from exc
    if page_count <= 0:
        raise RuntimeError(f"PDF contains no pages: {pdf_path}")
    return page_count


def _page_ranges(page_count: int) -> tuple[tuple[int, int], ...]:
    if page_count <= 0:
        raise ValueError("PDF page count must be positive")
    return tuple(
        (
            first_page,
            min(first_page + MAX_MINERU_FILE_PAGES - 1, page_count),
        )
        for first_page in range(1, page_count + 1, MAX_MINERU_FILE_PAGES)
    )


def _split_pdf(
    source_pdf: Path, page_count: int, temp_dir: Path
) -> tuple[_PageSegment, ...]:
    """Write ordered, page-limited PDF inputs into *temp_dir*."""
    segments: list[_PageSegment] = []
    try:
        with pymupdf.open(str(source_pdf)) as source:
            if source.page_count != page_count:
                raise RuntimeError(
                    "PDF page count changed while preparing MinerU segments: "
                    f"expected {page_count}, found {source.page_count}"
                )
            for index, (first_page, last_page) in enumerate(
                _page_ranges(page_count), start=1
            ):
                segment_path = temp_dir / (
                    f"{source_pdf.stem}.part-{index:03d}-pages-"
                    f"{first_page:04d}-{last_page:04d}.pdf"
                )
                segment = pymupdf.open()
                try:
                    segment.insert_pdf(
                        source,
                        from_page=first_page - 1,
                        to_page=last_page - 1,
                    )
                    segment.save(str(segment_path))
                finally:
                    segment.close()
                _fsync_file(segment_path)
                segments.append(
                    _PageSegment(
                        index=index,
                        first_page=first_page,
                        last_page=last_page,
                        path=segment_path,
                    )
                )
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Unable to split PDF for MinerU: {exc}") from exc
    _fsync_directory(temp_dir)
    return tuple(segments)


def _fixed_zip_info(name: str) -> zipfile.ZipInfo:
    """Create a stable outer-archive member header."""
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def _write_archive_file(
    archive: zipfile.ZipFile, source_path: Path, archive_name: str
) -> None:
    info = _fixed_zip_info(archive_name)
    with (
        source_path.open("rb") as source,
        archive.open(info, mode="w", force_zip64=True) as destination,
    ):
        shutil.copyfileobj(source, destination, length=_ARCHIVE_COPY_BUFFER_SIZE)


def _package_segmented_archives(
    output_path: Path,
    source_pdf_sha256: str,
    page_count: int,
    segments: tuple[_PageSegment, ...],
    results: tuple[MineruDownloadResult, ...],
) -> None:
    """Package complete MinerU response ZIPs without flattening their members."""
    if len(segments) != len(results):
        raise RuntimeError("MinerU segment and result counts do not match")

    manifest_segments: list[dict[str, object]] = []
    for segment, result in zip(segments, results, strict=True):
        manifest_segments.append(
            {
                "index": segment.index,
                "first_page": segment.first_page,
                "last_page": segment.last_page,
                "page_count": segment.last_page - segment.first_page + 1,
                "uploaded_file": result.file_name,
                "batch_id": result.batch_id,
                "response_archive": (
                    f"segments/segment-{segment.index:03d}-pages-"
                    f"{segment.first_page:04d}-{segment.last_page:04d}.zip"
                ),
                "response_sha256": _sha256_file(result.zip_path),
            }
        )
    manifest = {
        "schema_version": 1,
        "stage": 1,
        "format": "segmented-mineru-responses",
        "source_pdf": "source/source.pdf",
        "source_pdf_sha256": source_pdf_sha256,
        "source_page_count": page_count,
        "segment_page_limit": MAX_MINERU_FILE_PAGES,
        "segments": manifest_segments,
    }

    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
        )
        os.close(fd)
        temp_path = Path(raw_temp_path)
        with zipfile.ZipFile(temp_path, mode="w", allowZip64=True) as archive:
            archive.writestr(
                _fixed_zip_info("manifest.json"),
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
            for segment, result in zip(segments, results, strict=True):
                archive_name = (
                    f"segments/segment-{segment.index:03d}-pages-"
                    f"{segment.first_page:04d}-{segment.last_page:04d}.zip"
                )
                _write_archive_file(archive, result.zip_path, archive_name)
        os.replace(temp_path, output_path)
        temp_path = None
        _fsync_file(output_path)
        _fsync_directory(output_path.parent)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _source_paths(work: Path) -> tuple[Path, Path]:
    source_dir = work / "source"
    return source_dir / "source.pdf", source_dir / "source_meta.json"


def _ensure_existing_parse_source(work: Path) -> None:
    source_pdf, source_meta = _source_paths(work)
    raw_archive = _stage_path(work, "01_raw.zip")
    try:
        raw_descriptor = _open_regular_fd(
            raw_archive,
            "published Stage 1 raw archive",
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"Existing parse output raw archive is invalid: {exc}. "
            "Rerun parse with --force-rerun."
        ) from exc
    else:
        os.close(raw_descriptor)

    try:
        metadata, _ = read_json_document(
            source_meta,
            "Stage 1 source metadata",
        )
    except StrictJsonError as exc:
        raise RuntimeError(
            "Existing parse output has invalid source metadata at "
            f"{source_meta}: {exc}. Rerun parse with --force-rerun."
        ) from exc

    if not isinstance(metadata, dict):
        raise RuntimeError(
            "Existing parse output source metadata must be a JSON object at "
            f"{source_meta}. Rerun parse with --force-rerun."
        )
    if metadata.get("source_pdf") != "source/source.pdf":
        raise RuntimeError(
            "Existing parse output source metadata has an unexpected source_pdf "
            f"at {source_meta}. Rerun parse with --force-rerun."
        )
    expected_sha256 = metadata.get("sha256")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise RuntimeError(
            "Existing parse output source metadata has an invalid sha256 at "
            f"{source_meta}. Rerun parse with --force-rerun."
        )
    expected_size = metadata.get("size_bytes")
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
    ):
        raise RuntimeError(
            "Existing parse output source metadata has an invalid size_bytes at "
            f"{source_meta}. Rerun parse with --force-rerun."
        )

    try:
        descriptor = _open_regular_fd(source_pdf, "published Stage 1 source PDF")
    except RuntimeError as exc:
        raise RuntimeError(
            f"Existing parse output source PDF is invalid: {exc}. "
            "Rerun parse with --force-rerun."
        ) from exc
    try:
        before = os.fstat(descriptor)
        digest = _sha256_fd(descriptor, f"published Stage 1 source PDF {source_pdf}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    if not _same_source_snapshot(before, after):
        raise RuntimeError(
            "Existing parse output source PDF changed while it was being checked. "
            "Rerun parse with --force-rerun."
        )
    if before.st_size != expected_size:
        raise RuntimeError(
            "Existing parse output source PDF size drifted from source_meta.json. "
            "Rerun parse with --force-rerun."
        )
    if digest != expected_sha256:
        raise RuntimeError(
            "Existing parse output source PDF SHA-256 drifted from source_meta.json. "
            "Rerun parse with --force-rerun."
        )


def _prepare_source_pdf(
    pdf_path: Path, stage_dir: Path
) -> tuple[Path, dict[str, object]]:
    """Prepare source artifacts in an unpublished Stage 1 directory."""
    source_pdf, source_meta = _source_paths(stage_dir)
    source_pdf.parent.mkdir(parents=True, exist_ok=True)

    original = pdf_path.resolve()
    if not original.is_file():
        raise FileNotFoundError(f"PDF not found: {original}")

    source_fd = _open_regular_fd(original, "input PDF")
    destination_fd: int | None = None
    try:
        source_before = os.fstat(source_fd)
        destination_fd = os.open(
            source_pdf,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = os.read(source_fd, _ARCHIVE_COPY_BUFFER_SIZE)
            except OSError as exc:
                raise RuntimeError(f"Cannot read input PDF {original}: {exc}") from exc
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            _write_all(destination_fd, chunk, f"staged source PDF {source_pdf}")

        source_after = os.fstat(source_fd)
        if (
            not _same_source_snapshot(source_before, source_after)
            or total != source_before.st_size
        ):
            raise RuntimeError(
                "Input PDF changed while it was being copied; concurrent mutation "
                f"detected for {original}. Retry after the input stops changing."
            )
        destination_stat = os.fstat(destination_fd)
        if (
            not stat.S_ISREG(destination_stat.st_mode)
            or destination_stat.st_size != total
        ):
            raise RuntimeError(
                f"Staged source PDF is not a complete regular file: {source_pdf}"
            )
        _verify_original_input_path(original, source_after)
        os.fsync(destination_fd)
        sha256 = digest.hexdigest()
        copy_method = "copy"
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)

    meta: dict[str, object] = {
        "source_pdf": "source/source.pdf",
        "original_pdf_abs": str(original),
        "sha256": sha256,
        "size_bytes": total,
        "copied_at": datetime.now(timezone.utc).isoformat(),
    }
    source_meta.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _fsync_file(source_pdf)
    _fsync_file(source_meta)
    _fsync_directory(source_pdf.parent)
    _fsync_directory(stage_dir)
    log.info(
        "parse source: original=%s target=%s sha256=%s size_bytes=%s method=%s",
        original,
        source_pdf,
        sha256,
        meta["size_bytes"],
        copy_method,
    )
    return source_pdf, meta


def _verify_original_input_path(
    original: Path, copied_source_stat: os.stat_result
) -> None:
    """Reject path replacement or deletion after copying from the source fd."""
    try:
        path_stat = os.lstat(original)
    except OSError as exc:
        raise RuntimeError(
            "Input PDF changed while it was being copied; concurrent mutation "
            f"detected for {original}: {exc}"
        ) from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_dev != copied_source_stat.st_dev
        or path_stat.st_ino != copied_source_stat.st_ino
    ):
        raise RuntimeError(
            "Input PDF changed while it was being copied; concurrent mutation "
            f"detected for {original}."
        )

    try:
        current_descriptor = _open_regular_fd(original, "input PDF")
    except RuntimeError as exc:
        raise RuntimeError(
            "Input PDF changed while it was being copied; concurrent mutation "
            f"detected for {original}: {exc}"
        ) from exc
    try:
        current_stat = os.fstat(current_descriptor)
    finally:
        os.close(current_descriptor)
    if not _same_source_snapshot(copied_source_stat, current_stat):
        raise RuntimeError(
            "Input PDF changed while it was being copied; concurrent mutation "
            f"detected for {original}."
        )


def _stage1_transaction_path(work: Path) -> Path:
    return work / _STAGE1_TRANSACTION_NAME


def _strict_stage1_dir(
    work: Path,
    value: object,
    field: str,
    prefix: str,
    *,
    require_exists: bool = True,
) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Stage 1 transaction has invalid {field}")
    relative = Path(value)
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or relative.name != value
        or not relative.name.startswith(prefix)
    ):
        raise RuntimeError(
            f"Stage 1 transaction {field} must name a direct {prefix} child"
        )
    directory = work / relative.name
    if directory.parent != work or directory.is_symlink():
        raise RuntimeError(
            f"Stage 1 transaction {field} is not a safe directory: {directory}"
        )
    if directory.exists() and not directory.is_dir():
        raise RuntimeError(
            f"Stage 1 transaction {field} is not a safe directory: {directory}"
        )
    if require_exists and not directory.is_dir():
        raise RuntimeError(
            f"Stage 1 transaction {field} is not a safe directory: {directory}"
        )
    return directory


def _validate_stage1_target_parents(work: Path) -> None:
    source_dir = work / "source"
    if source_dir.is_symlink() or (source_dir.exists() and not source_dir.is_dir()):
        raise RuntimeError(
            f"Stage 1 target parent is not a safe directory: {source_dir}"
        )


def _remove_stage1_dir(work: Path, directory: Path, prefix: str) -> None:
    if directory.parent != work or not directory.name.startswith(prefix):
        raise RuntimeError(f"Refusing to remove unsafe Stage 1 directory: {directory}")
    if directory.is_symlink() or not directory.is_dir():
        if directory.exists() or directory.is_symlink():
            raise RuntimeError(
                f"Refusing to remove unsafe Stage 1 directory: {directory}"
            )
        return
    shutil.rmtree(directory)
    _fsync_directory(work)


def _cleanup_orphan_stage1_dirs(work: Path) -> None:
    removed = False
    for entry in work.iterdir():
        if entry.parent != work or entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name.startswith(_STAGE1_RECOVERY_PREFIX):
            _strict_stage1_dir(
                work, entry.name, "orphan recovery_dir", _STAGE1_RECOVERY_PREFIX
            )
            _remove_stage1_dir(work, entry, _STAGE1_RECOVERY_PREFIX)
            removed = True
        elif entry.name.startswith(_STAGE1_STAGING_PREFIX):
            _strict_stage1_dir(
                work, entry.name, "orphan staging_dir", _STAGE1_STAGING_PREFIX
            )
            _remove_stage1_dir(work, entry, _STAGE1_STAGING_PREFIX)
            removed = True
    if removed:
        _fsync_directory(work)


def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        os.close(fd)
        temp_path = Path(raw_temp_path)
        temp_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        # Sync marker data before publishing its name so recovery sees a complete journal.
        _fsync_file(temp_path)
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _begin_stage1_transaction(work: Path, staging_dir: Path) -> tuple[Path, Path]:
    marker_path = _stage1_transaction_path(work)
    _validate_stage1_target_parents(work)
    if (
        staging_dir.parent != work
        or staging_dir.is_symlink()
        or not staging_dir.is_dir()
        or not staging_dir.name.startswith(_STAGE1_STAGING_PREFIX)
        or staging_dir.name.startswith(_STAGE1_RECOVERY_PREFIX)
    ):
        raise RuntimeError(f"Stage 1 staging directory is not safe: {staging_dir}")
    target_stats: list[os.stat_result | None] = []
    for target_name in _STAGE1_TARGET_NAMES:
        target = work / target_name
        try:
            target_stat = os.lstat(target)
        except FileNotFoundError:
            target_stat = None
        except OSError as exc:
            raise RuntimeError(
                f"Cannot inspect existing Stage 1 target {target}: {exc}. "
                "Remove or replace it, then rerun with --force-rerun."
            ) from exc
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            target_kind = (
                "symlink" if stat.S_ISLNK(target_stat.st_mode) else "non-regular file"
            )
            raise RuntimeError(
                f"Cannot create Stage 1 backup for {target_kind} target {target}. "
                "Remove or replace it with a regular file, then rerun with "
                "--force-rerun."
            )
        target_stats.append(target_stat)

    recovery_dir = Path(tempfile.mkdtemp(prefix=_STAGE1_RECOVERY_PREFIX, dir=work))
    try:
        staging_relative = staging_dir.relative_to(work)
        targets: list[dict[str, object]] = []
        for index, (target_name, target_stat) in enumerate(
            zip(_STAGE1_TARGET_NAMES, target_stats, strict=True)
        ):
            target = work / target_name
            exists = target_stat is not None
            backup_name: str | None = None
            sha256: str | None = None
            size_bytes: int | None = None
            if exists:
                backup = recovery_dir / f"{index:02d}-{target.name}"
                shutil.copy2(target, backup)
                _fsync_file(backup)
                backup_name = str(backup.relative_to(work))
                sha256 = _sha256_file(backup)
                size_bytes = backup.stat().st_size
            targets.append(
                {
                    "path": target_name,
                    "exists": exists,
                    "backup": backup_name,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                }
            )
        # Backups must reach disk before the journal advertises recovery data.
        _fsync_directory(recovery_dir)
        _fsync_directory(work)
        _write_json_atomic(
            marker_path,
            {
                "schema_version": 1,
                "recovery_dir": str(recovery_dir.relative_to(work)),
                "staging_dir": str(staging_relative),
                "targets": targets,
            },
        )
        return marker_path, recovery_dir
    except BaseException:
        if not marker_path.exists():
            try:
                _remove_stage1_dir(work, recovery_dir, _STAGE1_RECOVERY_PREFIX)
            except (OSError, RuntimeError) as cleanup_error:
                log.warning(
                    "Could not remove failed Stage 1 recovery setup %s: %s",
                    recovery_dir,
                    cleanup_error,
                )
        else:
            log.error(
                "Stage 1 transaction marker %s remains after setup failure; recovery data is %s",
                marker_path,
                recovery_dir,
            )
        raise


def _load_stage1_transaction(
    work: Path,
) -> tuple[Path, Path, Path, list[dict[str, Any]]] | None:
    marker_path = _stage1_transaction_path(work)
    if not marker_path.exists():
        return None
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot parse unfinished Stage 1 transaction {marker_path}; "
            "preserve it and inspect the file before retrying"
        ) from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise RuntimeError(f"Unsupported Stage 1 transaction marker: {marker_path}")

    recovery_dir = _strict_stage1_dir(
        work, data.get("recovery_dir"), "recovery_dir", _STAGE1_RECOVERY_PREFIX
    )
    staging_dir = _strict_stage1_dir(
        work,
        data.get("staging_dir"),
        "staging_dir",
        _STAGE1_STAGING_PREFIX,
        require_exists=False,
    )
    if staging_dir.name.startswith(_STAGE1_RECOVERY_PREFIX):
        raise RuntimeError(
            f"Stage 1 transaction staging directory is unsafe: {staging_dir}"
        )
    _validate_stage1_target_parents(work)
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or len(raw_targets) != len(
        _STAGE1_TARGET_NAMES
    ):
        raise RuntimeError(f"Stage 1 transaction has invalid targets: {marker_path}")

    targets: list[dict[str, Any]] = []
    for expected_name, raw_target in zip(
        _STAGE1_TARGET_NAMES, raw_targets, strict=True
    ):
        if not isinstance(raw_target, dict) or raw_target.get("path") != expected_name:
            raise RuntimeError(f"Stage 1 transaction target mismatch: {marker_path}")
        exists = raw_target.get("exists")
        if not isinstance(exists, bool):
            raise RuntimeError(
                f"Stage 1 transaction has invalid target state: {marker_path}"
            )
        backup_value = raw_target.get("backup")
        backup: Path | None = None
        if exists:
            if not isinstance(backup_value, str):
                raise RuntimeError(
                    f"Stage 1 transaction backup is invalid: {marker_path}"
                )
            backup_relative = Path(backup_value)
            if (
                backup_relative.is_absolute()
                or len(backup_relative.parts) != 2
                or backup_relative.parent.name != recovery_dir.name
            ):
                raise RuntimeError(
                    f"Stage 1 transaction backup escapes recovery directory: {marker_path}"
                )
            backup = work / backup_relative
            if backup.parent != recovery_dir:
                raise RuntimeError(
                    f"Stage 1 transaction backup escapes recovery directory: {marker_path}"
                )
            if backup.is_symlink() or not backup.is_file():
                raise RuntimeError(
                    f"Stage 1 transaction backup is unavailable: {backup}"
                )
            if not isinstance(raw_target.get("sha256"), str):
                raise RuntimeError(f"Stage 1 transaction backup has no hash: {backup}")
        elif backup_value is not None:
            raise RuntimeError(
                f"Stage 1 transaction records backup for absent target: {marker_path}"
            )
        targets.append(
            {
                "path": work / expected_name,
                "exists": exists,
                "backup": backup,
                "sha256": raw_target.get("sha256"),
            }
        )
    return marker_path, recovery_dir, staging_dir, targets


def _recover_stage1_transaction(work: Path) -> bool:
    loaded = _load_stage1_transaction(work)
    if loaded is None:
        return False
    marker_path, recovery_dir, staging_dir, targets = loaded
    for index, target_state in enumerate(targets):
        target = target_state["path"]
        if not isinstance(target, Path):
            raise RuntimeError(
                f"Stage 1 transaction target path is invalid: {marker_path}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target_state["exists"]:
            backup = target_state["backup"]
            if not isinstance(backup, Path):
                raise RuntimeError(
                    f"Stage 1 transaction backup is invalid: {marker_path}"
                )
            restore_path = recovery_dir / f".restore-{index:02d}-{target.name}"
            restore_path.unlink(missing_ok=True)
            shutil.copy2(backup, restore_path)
            _fsync_file(restore_path)
            _fsync_directory(recovery_dir)
            os.replace(restore_path, target)
            _fsync_directory(target.parent)
            expected_sha256 = target_state["sha256"]
            if _sha256_file(target) != expected_sha256:
                raise RuntimeError(f"Stage 1 recovery hash mismatch for {target}")
        else:
            if target.is_dir() and not target.is_symlink():
                raise RuntimeError(f"Stage 1 recovery target is a directory: {target}")
            target.unlink(missing_ok=True)
            _fsync_directory(target.parent)
    _fsync_directory(work)
    marker_path.unlink()
    _fsync_directory(marker_path.parent)
    try:
        _remove_stage1_dir(work, staging_dir, _STAGE1_STAGING_PREFIX)
    except (OSError, RuntimeError) as cleanup_error:
        log.warning(
            "Could not remove recovered Stage 1 staging directory %s: %s",
            staging_dir,
            cleanup_error,
        )
    try:
        _remove_stage1_dir(work, recovery_dir, _STAGE1_RECOVERY_PREFIX)
    except (OSError, RuntimeError) as cleanup_error:
        log.warning(
            "Could not remove recovered Stage 1 directory %s: %s",
            recovery_dir,
            cleanup_error,
        )
    return True


def _replace_stage1_target(staged_path: Path, published_path: Path) -> None:
    os.replace(staged_path, published_path)
    _fsync_directory(published_path.parent)


def _publish_stage1_result(
    work: Path,
    staged_source_pdf: Path,
    staged_source_meta: Path,
    staged_output: Path,
    staging_dir: Path,
) -> None:
    """Publish Stage 1 files through a marker-backed recoverable transaction."""
    published_source_pdf, published_source_meta = _source_paths(work)
    _validate_stage1_target_parents(work)
    published_source_pdf.parent.mkdir(parents=True, exist_ok=True)
    _fsync_directory(work)
    marker_path, recovery_dir = _begin_stage1_transaction(work, staging_dir)
    published_files = (
        (staged_source_pdf, published_source_pdf),
        (staged_source_meta, published_source_meta),
        (staged_output, _stage_path(work, "01_raw.zip")),
    )
    try:
        for staged_path, published_path in published_files:
            _replace_stage1_target(staged_path, published_path)
        _fsync_directory(published_source_pdf.parent)
        _fsync_directory(work)
        # Keep the journal until every published name and parent directory is durable.
        marker_path.unlink()
        _fsync_directory(marker_path.parent)
    except BaseException as publish_error:
        try:
            _recover_stage1_transaction(work)
        except BaseException as recovery_error:
            raise _Stage1PublishRecoveryError(
                "Stage 1 publication failed with "
                f"{type(publish_error).__name__}, and recovery failed with "
                f"{type(recovery_error).__name__}; recover using marker "
                f"{marker_path}, backups in {recovery_dir}, and staged files in "
                f"{staging_dir}",
                marker_path,
                recovery_dir,
                staging_dir,
            ) from recovery_error
        raise
    try:
        _remove_stage1_dir(work, recovery_dir, _STAGE1_RECOVERY_PREFIX)
    except (OSError, RuntimeError) as cleanup_error:
        log.warning(
            "Stage 1 published successfully but could not remove recovery directory %s: %s",
            recovery_dir,
            cleanup_error,
        )


def run_all(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    from_stage: int = 1,
    continue_on_error: bool = False,
) -> None:
    if not 1 <= from_stage <= 5:
        raise ValueError("from_stage must be between 1 and 5")

    # stages < from_stage use normal skip; stages >= from_stage are controlled by --force-rerun
    def _f(stage: int) -> bool:
        return force if stage >= from_stage else False

    with stage_timer(log, "pipeline"):
        run_parse(pdf_path, cfg, force=_f(1))
        run_normalize(pdf_path, cfg, force=_f(2))
        run_segment(pdf_path, cfg, force=_f(3))
        run_prepare(pdf_path, cfg, force=_f(4))
        run_revise(
            pdf_path,
            cfg,
            force=_f(5),
            continue_on_error=continue_on_error,
        )

    log.info("pipeline total: %s", get_tracker().summary_line())


def run_parse(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    work = cfg.book_work_dir(pdf_path)
    work.mkdir(parents=True, exist_ok=True)
    with _stage1_lock(work):
        _require_stage1_capabilities(work)
        _recover_stage1_transaction(work)
        _cleanup_orphan_stage1_dirs(work)
        _validate_stage1_target_parents(work)
        out = _stage_path(work, "01_raw.zip")
        if (out.exists() or out.is_symlink()) and not force:
            log.info("skip parse — reusing %s (pass --force-rerun to re-run)", out)
            _ensure_existing_parse_source(work)
            return

        staging_dir = Path(tempfile.mkdtemp(prefix=".stage1-", dir=work))
        preserve_staging = False
        try:
            staged_source_pdf, source_meta = _prepare_source_pdf(pdf_path, staging_dir)
            page_count = _pdf_page_count(staged_source_pdf)
            if page_count > MAX_STAGE1_PAGES:
                raise RuntimeError(
                    f"PDF has {page_count} pages; Stage 1 accepts at most "
                    f"{MAX_STAGE1_PAGES} pages"
                )
            cfg.require_mineru()

            staged_output = staging_dir / out.name
            log.info("Stage 1: sending %s to MinerU...", pdf_path.name)
            with stage_timer(log, "1 parse"):
                with MineruClient(cfg.mineru) as client:
                    if page_count <= MAX_MINERU_FILE_PAGES:
                        result = client.process_file(staged_source_pdf, staged_output)
                        log.info("  -> %s (batch=%s)", staged_output, result.batch_id)
                    else:
                        segments_dir = staging_dir / "segments"
                        segments_dir.mkdir()
                        segments = _split_pdf(
                            staged_source_pdf, page_count, segments_dir
                        )
                        results: list[MineruDownloadResult] = []
                        for segment in segments:
                            response_path = (
                                staging_dir / f"response-{segment.index:03d}.zip"
                            )
                            log.info(
                                "Stage 1: sending segment %d (%d-%d/%d) to MinerU...",
                                segment.index,
                                segment.first_page,
                                segment.last_page,
                                page_count,
                            )
                            result = client.process_file(segment.path, response_path)
                            results.append(result)
                        _package_segmented_archives(
                            staged_output,
                            str(source_meta["sha256"]),
                            page_count,
                            segments,
                            tuple(results),
                        )
                        log.info(
                            "  -> %s (segments=%d, pages=%d)",
                            out,
                            len(segments),
                            page_count,
                        )
            staged_source_meta = _source_paths(staging_dir)[1]
            _fsync_file(staged_source_pdf)
            _fsync_file(staged_source_meta)
            _fsync_file(staged_output)
            _fsync_directory(staged_source_pdf.parent)
            _fsync_directory(staging_dir)
            _publish_stage1_result(
                work,
                staged_source_pdf,
                staged_source_meta,
                staged_output,
                staging_dir,
            )
        except _Stage1PublishRecoveryError:
            preserve_staging = True
            raise
        finally:
            if not preserve_staging:
                try:
                    _remove_stage1_dir(work, staging_dir, _STAGE1_STAGING_PREFIX)
                except (OSError, RuntimeError) as cleanup_error:
                    log.warning(
                        "Could not remove Stage 1 staging directory %s: %s",
                        staging_dir,
                        cleanup_error,
                    )


def run_normalize(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    """Run Stage 2, normalizing the immutable MinerU archive."""
    from epubforge.mineru_content import normalize_mineru_content

    work = cfg.book_work_dir(pdf_path)
    raw_archive = _require_stage_file(work, "01_raw.zip", 2)
    source_pdf = _require_stage_file(work, "source/source.pdf", 2)
    output_dir = work / "02_content"
    log.info("Stage 2: normalizing MinerU content...")
    with stage_timer(log, "2 normalize"):
        normalize_mineru_content(
            raw_archive,
            output_dir,
            source_pdf=source_pdf,
            force=force,
        )
    log.info("  -> %s", output_dir)


def run_segment(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    """Run Stage 3, detecting chapter boundaries with the configured LLM."""
    from epubforge.chapter_segmentation import (
        is_chapter_segmentation_fresh,
        segment_chapters,
    )

    work = cfg.book_work_dir(pdf_path)
    content_path = _require_stage_file(work, "02_content/content.json", 3)
    output_dir = work / "03_chapters"
    with stage_timer(log, "3 segment"):
        if not force and is_chapter_segmentation_fresh(
            content_path,
            output_dir,
            model=cfg.llm.model,
        ):
            log.info("skip segment — reusing fresh %s", output_dir / "chapters.json")
            return

        cfg.require_llm()
        client = LLMClient(cfg)
        log.info("Stage 3: segmenting chapters with model=%s...", client.model)
        segment_chapters(content_path, output_dir, client, force=force)
        log.info("  -> %s", output_dir / "chapters.json")


def run_prepare(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    """Run Stage 4, rendering deterministic chapter editing workspaces."""
    from epubforge.chapter_workspace import build_chapter_workspace

    work = cfg.book_work_dir(pdf_path)
    _require_stage_file(work, "02_content/content.json", 4)
    _require_stage_file(work, "03_chapters/chapters.json", 4)
    _require_stage_file(work, "source/source.pdf", 4)
    output_dir = work / "04_edit"
    log.info(
        "Stage 4: preparing chapter workspaces (dpi=%d quality=%d)...",
        cfg.chapters.render_dpi,
        cfg.chapters.jpeg_quality,
    )
    with stage_timer(log, "4 prepare"):
        build_chapter_workspace(
            work,
            force=force,
            dpi=cfg.chapters.render_dpi,
            quality=cfg.chapters.jpeg_quality,
        )
    log.info("  -> %s", output_dir)


def run_revise(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    continue_on_error: bool = False,
) -> None:
    """Run Stage 5 and fail clearly after preserving successful chapters."""
    from epubforge.chapter_revision import (
        is_chapter_revision_fresh,
        revise_all_chapters,
    )

    work = cfg.book_work_dir(pdf_path)
    edit_dir = work / "04_edit"
    if not edit_dir.is_dir():
        raise RuntimeError(
            f"Stage 5 requires 04_edit to exist in {work}. "
            "Run earlier pipeline stages first."
        )
    _require_stage_file(work, "04_edit/manifest.json", 5)
    with stage_timer(log, "5 revise"):
        if not force and is_chapter_revision_fresh(edit_dir, model=cfg.llm.model):
            log.info(
                "skip revise — reusing fresh corrected chapter outputs in %s", edit_dir
            )
            return

        cfg.require_llm()
        client = LLMClient(cfg)
        log.info(
            "Stage 5: revising chapters with model=%s (continue_on_error=%s)...",
            client.model,
            continue_on_error,
        )
        report = revise_all_chapters(
            edit_dir,
            client,
            force=force,
            continue_on_error=continue_on_error,
        )
        log.info(
            "Stage 5 report: completed=%d skipped=%d failed=%d",
            len(report.completed),
            len(report.skipped),
            len(report.failed),
        )
        if report.failed:
            details = "; ".join(
                f"{chapter}: {report.errors.get(chapter, 'unknown error')}"
                for chapter in report.failed
            )
            raise RuntimeError(
                "Stage 5 chapter revision failed; successful chapters were preserved. "
                f"Failures: {details}"
            )
