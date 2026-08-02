"""Pipeline orchestration for stages 1-4 plus explicit build."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
from filelock import FileLock, Timeout

from epubforge.config import Config
from epubforge.mineru import MineruClient, MineruDownloadResult
from epubforge.observability import get_tracker, stage_timer

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


def _skip(path: Path, force: bool, label: str) -> bool:
    if path.exists() and not force:
        log.info("skip %s — reusing %s (pass --force-rerun to re-run)", label, path)
        return True
    return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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
    missing = [
        str(path.relative_to(work))
        for path in (source_pdf, source_meta)
        if not path.is_file()
    ]
    if missing:
        raise RuntimeError(
            "Existing parse output is missing stable source artifact(s): "
            f"{', '.join(missing)}. Rerun parse with --force-rerun."
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

    try:
        os.link(original, source_pdf)
        copy_method = "hardlink"
    except OSError:
        shutil.copy2(original, source_pdf)
        copy_method = "copy2"

    if not os.access(source_pdf, os.R_OK):
        raise RuntimeError(f"Persisted source PDF is not readable: {source_pdf}")

    sha256 = _sha256_file(source_pdf)
    meta: dict[str, object] = {
        "source_pdf": "source/source.pdf",
        "original_pdf_abs": str(original),
        "sha256": sha256,
        "size_bytes": source_pdf.stat().st_size,
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
    recovery_dir = Path(tempfile.mkdtemp(prefix=_STAGE1_RECOVERY_PREFIX, dir=work))
    try:
        staging_relative = staging_dir.relative_to(work)
        targets: list[dict[str, object]] = []
        for index, target_name in enumerate(_STAGE1_TARGET_NAMES):
            target = work / target_name
            if target.is_symlink() or target.is_dir():
                raise RuntimeError(
                    f"Cannot create Stage 1 backup for non-file target: {target}"
                )
            exists = target.exists()
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
    pages: set[int] | None = None,
) -> None:
    # stages < from_stage use normal skip; stages >= from_stage are controlled by --force-rerun
    def _f(stage: int) -> bool:
        return force if stage >= from_stage else False

    with stage_timer(log, "pipeline"):
        run_parse(pdf_path, cfg, force=_f(1))
        run_classify(pdf_path, cfg, force=_f(2))
        if from_stage >= 4:
            # run --from 4: only validate active artifact exists, never create a new one
            run_extract(pdf_path, cfg, force=False, pages=pages, reuse_only=True)
        else:
            run_extract(pdf_path, cfg, force=_f(3), pages=pages)
        run_assemble(pdf_path, cfg, force=_f(4))

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
        if out.exists() and not force:
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


def run_classify(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.classifier import classify_pages

    work = cfg.book_work_dir(pdf_path)
    raw = _stage_path(work, "01_raw.json")
    out = _stage_path(work, "02_pages.json")
    if _skip(out, force, "classify"):
        return
    log.info("Stage 2: classifying pages…")
    with stage_timer(log, "2 classify"):
        classify_pages(raw, out)
    log.info("  -> %s", out)


def _parse_pages_json(pages_json: Path) -> tuple[list[int], list[int], list[int]]:
    """Read 02_pages.json and return (selected_pages, toc_pages, complex_pages).

    selected_pages: pages with kind != "toc", sorted.
    toc_pages: pages with kind == "toc", sorted.
    complex_pages: pages with kind == "complex", sorted.
    """
    data: dict[str, Any] = json.loads(pages_json.read_text(encoding="utf-8"))
    pages_data: list[dict[str, Any]] = data["pages"]
    selected_pages = sorted(p["page"] for p in pages_data if p["kind"] != "toc")
    toc_pages = sorted(p["page"] for p in pages_data if p["kind"] == "toc")
    complex_pages = sorted(p["page"] for p in pages_data if p["kind"] == "complex")
    return selected_pages, toc_pages, complex_pages


def _settings_for_artifact(cfg: Config) -> dict[str, Any]:
    """Build the settings snapshot used for artifact_id computation."""
    return {
        "contract_version": 3,
        "enable_book_memory": False,
    }


def run_extract(
    pdf_path: Path,
    cfg: Config,
    *,
    force: bool = False,
    pages: set[int] | None = None,
    reuse_only: bool = False,
) -> None:
    from epubforge.stage3_artifacts import (
        Stage3Manifest,
        active_manifest_matches_desired,
        activate_manifest_atomic,
        build_desired_stage3_manifest,
        load_active_stage3_manifest,
        validate_stage3_artifact,
        write_artifact_manifest_atomic,
    )

    work = cfg.book_work_dir(pdf_path)
    source_pdf = work / "source" / "source.pdf"
    raw = _stage_path(work, "01_raw.json")
    pages_json = _stage_path(work, "02_pages.json")

    # Validate prerequisite files exist
    for label, path in [
        ("source/source.pdf", source_pdf),
        ("01_raw.json", raw),
        ("02_pages.json", pages_json),
    ]:
        if not path.is_file():
            raise RuntimeError(
                f"Stage 3 requires {label} to exist in {work}. "
                "Run earlier pipeline stages first."
            )

    # Read SHAs for artifact_id computation
    source_pdf_sha256 = _sha256_file(source_pdf)
    raw_sha256 = _sha256_file(raw)
    pages_sha256 = _sha256_file(pages_json)

    # Parse pages classification
    selected_pages, toc_pages, complex_pages = _parse_pages_json(pages_json)

    # Apply pages filter
    page_filter: list[int] | None = None
    if pages is not None:
        page_filter = sorted(pages)
        selected_pages = sorted(p for p in selected_pages if p in pages)
        toc_pages = sorted(p for p in toc_pages if p in pages)
        complex_pages = sorted(p for p in complex_pages if p in pages)

    mode = "docling"
    settings = _settings_for_artifact(cfg)

    desired_artifact_id = build_desired_stage3_manifest(
        mode=mode,
        source_pdf_rel="source/source.pdf",
        source_pdf_sha256=source_pdf_sha256,
        raw_sha256=raw_sha256,
        pages_sha256=pages_sha256,
        selected_pages=selected_pages,
        toc_pages=toc_pages,
        complex_pages=complex_pages,
        page_filter=page_filter,
        settings=settings,
    )

    # Check for reusable active artifact (unless force=True)
    if not force:
        if active_manifest_matches_desired(work, desired_artifact_id):
            try:
                pointer, manifest = load_active_stage3_manifest(work)
                validate_stage3_artifact(work, manifest)
                log.info(
                    "Stage 3: reusing active artifact mode=%s artifact_id=%s manifest_sha256=%s",
                    manifest.mode,
                    manifest.artifact_id,
                    pointer.manifest_sha256,
                )
                log.info("Stage 3: provider_required=%s", False)
                return
            except Exception as exc:
                log.warning(
                    "Stage 3: active artifact validation failed (%s), will re-extract",
                    exc,
                )

    # Handle reuse_only mode: fail if we can't reuse
    if reuse_only:
        raise RuntimeError(
            f"Stage 3: no valid active artifact matching desired configuration "
            f"(artifact_id={desired_artifact_id}). "
            "Run `epubforge extract <pdf>` or `epubforge run <pdf> --from 3` first."
        )

    # Read old active artifact_id for logging
    old_artifact_id: str | None = None
    try:
        old_pointer, _ = load_active_stage3_manifest(work)
        old_artifact_id = old_pointer.active_artifact_id
    except Exception:
        log.debug("No prior artifact found, starting fresh")

    # Create artifact directory
    artifact_dir = work / "03_extract" / "artifacts" / desired_artifact_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    from epubforge.extract_skip_vlm import extract_skip_vlm

    log.info("Stage 3: extracting (Docling evidence draft)...")
    log.info("Stage 3: provider_required=%s", False)
    with stage_timer(log, "3 extract"):
        result = extract_skip_vlm(
            raw,
            pages_json,
            artifact_dir,
            force=force,
            page_filter=pages,
            images_dir=work / "images",
        )

    # Build and write manifest
    artifact_dir_rel = artifact_dir.relative_to(work).as_posix()
    unit_files_rel = [f.relative_to(work).as_posix() for f in result.unit_files]
    sidecars_rel = {
        "audit_notes": result.audit_notes_path.relative_to(work).as_posix(),
        "book_memory": result.book_memory_path.relative_to(work).as_posix(),
        "evidence_index": result.evidence_index_path.relative_to(work).as_posix(),
        "warnings": (
            result.warnings_path.relative_to(work).as_posix()
            if result.warnings_path is not None
            else (artifact_dir / "warnings.json").relative_to(work).as_posix()
        ),
    }

    from epubforge.stage3_artifacts import _now_utc_iso  # type: ignore[attr-defined]

    manifest = Stage3Manifest(
        mode=result.mode,
        artifact_id=desired_artifact_id,
        artifact_dir=artifact_dir_rel,
        created_at=_now_utc_iso(),
        raw_sha256=raw_sha256,
        pages_sha256=pages_sha256,
        source_pdf="source/source.pdf",
        source_pdf_sha256=source_pdf_sha256,
        selected_pages=selected_pages,
        toc_pages=toc_pages,
        complex_pages=complex_pages,
        page_filter=page_filter,
        unit_files=unit_files_rel,
        sidecars=sidecars_rel,
        settings=settings,
    )

    write_artifact_manifest_atomic(work, manifest)
    activate_manifest_atomic(work, manifest)

    log.info(
        "Stage 3: activated artifact_id=%s (previous=%s)",
        desired_artifact_id,
        old_artifact_id,
    )


def run_assemble(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.ir.semantic import Book, ExtractionMetadata
    from epubforge.stage3_artifacts import load_active_stage3_manifest

    work = cfg.book_work_dir(pdf_path)
    out = _stage_path(work, "05_semantic_raw.json")

    # 1. Load active Stage 3 manifest (fail if missing)
    pointer, manifest = load_active_stage3_manifest(work)

    # 2. Check freshness
    if not force and out.exists():
        try:
            book = Book.model_validate_json(out.read_text(encoding="utf-8"))
            if (
                book.extraction.artifact_id == pointer.active_artifact_id
                and book.extraction.stage3_manifest_sha256 == pointer.manifest_sha256
            ):
                log.info(
                    "Stage 4: skipping assemble (fresh: artifact_id=%s)",
                    pointer.active_artifact_id,
                )
                return
        except Exception:
            pass  # damaged/old format → rerun

    # 3. Assemble from manifest
    log.info(
        "Stage 4: assembling from manifest artifact_id=%s mode=%s...",
        manifest.artifact_id,
        manifest.mode,
    )
    from epubforge.assembler import assemble_from_manifest

    with stage_timer(log, "4 assemble"):
        book = assemble_from_manifest(work, manifest)

    # 4. Write Book.extraction metadata
    from pathlib import PurePosixPath as _PurePosix

    manifest_path_abs = work / _PurePosix(pointer.manifest_path)
    book.extraction = ExtractionMetadata(
        stage3_mode=manifest.mode,
        stage3_manifest_path=str(manifest_path_abs),
        stage3_manifest_sha256=pointer.manifest_sha256,
        artifact_id=manifest.artifact_id,
        selected_pages=manifest.selected_pages,
        complex_pages=manifest.complex_pages,
        source_pdf=manifest.source_pdf,
        evidence_index_path=manifest.sidecars.get("evidence_index", ""),
    )

    out.write_text(book.model_dump_json(indent=2), encoding="utf-8")
    log.info("  -> %s", out)


def run_build(pdf_path: Path, cfg: Config, *, force: bool = False) -> None:
    from epubforge.epub_builder import build_epub, resolve_build_source

    work = cfg.book_work_dir(pdf_path)
    semantic = resolve_build_source(work)
    registry = _stage_path(work, "style_registry.json")
    cfg.runtime.out_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.book_out_path(pdf_path)
    if _skip(out, force, "build"):
        return
    log.info("Stage 5: building EPUB...")
    with stage_timer(log, "5 build"):
        build_epub(
            semantic,
            out,
            images_dir=work / "images",
            registry_path=registry if registry.exists() else None,
            work_dir=work,
        )
    log.info("  -> %s", out)
