"""Normalize MinerU response archives into a small downstream artifact.

Stage 1 deliberately stores the response archive without changing it.  This
module is the first reader of that archive and keeps its output independent of
the Semantic IR and editor subsystems.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any
import zipfile

import pymupdf


CONTENT_SCHEMA = "epubforge.mineru-content"
CONTENT_SCHEMA_VERSION = 1
MAX_FULL_PAGE_COUNT = 1000
MAX_MINERU_SEGMENT_PAGES = 200
DEFAULT_MAX_ARCHIVE_BYTES = 2 * 1024**3
DEFAULT_MAX_UNCOMPRESSED_BYTES = 8 * 1024**3
DEFAULT_MAX_MEMBERS = 100_000
_COPY_BUFFER_SIZE = 1024 * 1024
_ASSET_PATH_KEYS = frozenset(
    {
        "asset_path",
        "image_path",
        "img_path",
    }
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


class MineruContentError(ValueError):
    """Raised when a MinerU response archive violates the content contract."""


class _ZipIndex:
    def __init__(self, members: dict[str, zipfile.ZipInfo], total_size: int) -> None:
        self.members = members
        self.total_size = total_size


class _AssetStore:
    """Copy referenced assets into the staged output directory."""

    def __init__(
        self,
        assets_dir: Path,
        *,
        max_asset_bytes: int,
    ) -> None:
        self.assets_dir = assets_dir
        self.max_asset_bytes = max_asset_bytes
        self._cache: dict[tuple[str, str], str] = {}
        self._metadata: dict[str, dict[str, int | str]] = {}

    def copy(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        member_name: str,
        *,
        archive_key: str,
    ) -> str:
        cache_key = (archive_key, member_name)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if info.is_dir():
            raise MineruContentError(f"Referenced asset is a directory: {member_name}")
        if info.file_size > self.max_asset_bytes:
            raise MineruContentError(
                f"Referenced asset exceeds the size limit: {member_name}"
            )

        fd, raw_temp = tempfile.mkstemp(
            dir=self.assets_dir,
            prefix=".asset-",
            suffix=".tmp",
        )
        temp_path = Path(raw_temp)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as destination:
                try:
                    with archive.open(info, mode="r") as source:
                        while True:
                            chunk = source.read(_COPY_BUFFER_SIZE)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > self.max_asset_bytes:
                                raise MineruContentError(
                                    f"Referenced asset exceeds the size limit: "
                                    f"{member_name}"
                                )
                            digest.update(chunk)
                            destination.write(chunk)
                except MineruContentError:
                    raise
                except Exception as exc:
                    raise MineruContentError(
                        f"Cannot read referenced asset {member_name}: {exc}"
                    ) from exc
                if total != info.file_size:
                    raise MineruContentError(
                        f"Referenced asset is truncated: {member_name}"
                    )
                destination.flush()
                os.fsync(destination.fileno())

            basename = _safe_asset_basename(member_name)
            output_name = f"{digest.hexdigest()}-{basename}"
            output_path = self.assets_dir / output_name
            if output_path.exists() or output_path.is_symlink():
                if not output_path.is_file() or output_path.is_symlink():
                    raise MineruContentError(
                        f"Asset output path is not a regular file: {output_path}"
                    )
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, output_path)
            relative = PurePosixPath("assets") / output_name
            result = relative.as_posix()
            self._cache[cache_key] = result
            self._metadata[result] = {
                "sha256": digest.hexdigest(),
                "size": total,
            }
            return result
        finally:
            temp_path.unlink(missing_ok=True)

    def metadata(self) -> dict[str, dict[str, int | str]]:
        """Return deterministic metadata for every copied asset."""
        return {name: dict(self._metadata[name]) for name in sorted(self._metadata)}


def normalize_mineru_content(
    raw_archive: str | Path,
    output_dir: str | Path | None = None,
    *,
    force: bool = False,
    full_page_count: int | None = None,
    source_pdf: str | Path | None = None,
    work_dir: str | Path | None = None,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_member_bytes: int | None = None,
    max_asset_bytes: int | None = None,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> Path:
    """Normalize *raw_archive* and atomically publish a ``02_content`` dir.

    With ``force=False``, a previously published directory is reused only
    when its recorded source archive SHA-256 matches the current input.  A
    stale or incomplete directory gets rebuilt.  Pass ``source_pdf`` to
    validate the manifest's source PDF SHA and page count; ``work_dir`` is an
    explicit shorthand for ``work_dir/source/source.pdf`` and
    ``work_dir/02_content``.  The function returns the published directory.
    """
    raw_path = Path(raw_archive)
    if source_pdf is not None and work_dir is not None:
        raise ValueError("source_pdf and work_dir are mutually exclusive")
    if work_dir is not None:
        work_path = Path(work_dir)
        source_pdf_path = work_path / "source" / "source.pdf"
        output_path = (
            work_path / "02_content" if output_dir is None else Path(output_dir)
        )
    else:
        source_pdf_path = None if source_pdf is None else Path(source_pdf)
        output_path = (
            raw_path.parent / "02_content" if output_dir is None else Path(output_dir)
        )
    _validate_limits(
        max_archive_bytes,
        max_uncompressed_bytes,
        max_member_bytes,
        max_asset_bytes,
        max_members,
    )
    effective_member_bytes = (
        max_member_bytes if max_member_bytes is not None else max_uncompressed_bytes
    )
    effective_asset_bytes = (
        max_asset_bytes if max_asset_bytes is not None else effective_member_bytes
    )
    normalization_contract = {
        "contract_version": CONTENT_SCHEMA_VERSION,
        "max_archive_bytes": max_archive_bytes,
        "max_uncompressed_bytes": max_uncompressed_bytes,
        "max_member_bytes": max_member_bytes,
        "max_asset_bytes": max_asset_bytes,
        "max_members": max_members,
    }
    source_pdf_sha256: str | None = None
    source_pdf_page_count: int | None = None
    if source_pdf_path is not None:
        source_pdf_sha256, source_pdf_page_count = _source_pdf_details(source_pdf_path)

    output_parent = output_path.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    staging_path = Path(
        tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=output_parent)
    )
    try:
        assets_dir = staging_path / "assets"
        assets_dir.mkdir()
        asset_store = _AssetStore(
            assets_dir,
            max_asset_bytes=effective_asset_bytes,
        )
        with _open_raw_archive(
            raw_path,
            max_archive_bytes=max_archive_bytes,
        ) as (archive, archive_size, source_sha256):
            outer_index = _index_zip(
                archive,
                label=f"raw archive {raw_path}",
                max_uncompressed_bytes=max_uncompressed_bytes,
                max_member_bytes=effective_member_bytes,
                max_members=max_members,
            )
            if "manifest.json" in outer_index.members:
                (
                    payload,
                    page_count,
                    kind,
                    segment_count,
                    normalized_source_pdf_sha256,
                ) = _normalize_segmented(
                    archive,
                    outer_index,
                    staging_path,
                    asset_store,
                    max_archive_bytes=max_archive_bytes,
                    max_uncompressed_bytes=max_uncompressed_bytes,
                    max_member_bytes=effective_member_bytes,
                    max_members=max_members,
                    source_pdf_sha256=source_pdf_sha256,
                    source_pdf_page_count=source_pdf_page_count,
                )
            else:
                (
                    payload,
                    page_count,
                    kind,
                    segment_count,
                    normalized_source_pdf_sha256,
                ) = _normalize_direct(
                    archive,
                    outer_index,
                    asset_store,
                    full_page_count=full_page_count,
                    source_pdf_sha256=source_pdf_sha256,
                    source_pdf_page_count=source_pdf_page_count,
                )

        result = {
            "schema": CONTENT_SCHEMA,
            "schema_version": CONTENT_SCHEMA_VERSION,
            "source_archive_sha256": source_sha256,
            "source_archive_size": archive_size,
            "source_kind": kind,
            "segment_count": segment_count,
            "page_count": page_count,
            "source_pdf_sha256": normalized_source_pdf_sha256,
            "items_sha256": _json_sha256(payload),
            "normalization": normalization_contract,
            "assets": asset_store.metadata(),
            "items": payload,
        }
        content_path = staging_path / "content.json"
        content_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _fsync_file(content_path)
        if not force and _existing_output_matches(
            output_path,
            staging_path,
            asset_store.metadata(),
        ):
            return output_path
        _publish_directory(staging_path, output_path)
        return output_path
    except (MineruContentError, OSError, zipfile.BadZipFile) as exc:
        if isinstance(exc, MineruContentError):
            raise
        raise MineruContentError(f"Cannot normalize MinerU archive: {exc}") from exc
    finally:
        _remove_path(staging_path)


def normalize_content(
    raw_archive: str | Path,
    output_dir: str | Path | None = None,
    *,
    force: bool = False,
    full_page_count: int | None = None,
    source_pdf: str | Path | None = None,
    work_dir: str | Path | None = None,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_member_bytes: int | None = None,
    max_asset_bytes: int | None = None,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> Path:
    """Compatibility alias for :func:`normalize_mineru_content`."""
    return normalize_mineru_content(
        raw_archive,
        output_dir,
        force=force,
        full_page_count=full_page_count,
        source_pdf=source_pdf,
        work_dir=work_dir,
        max_archive_bytes=max_archive_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_member_bytes=max_member_bytes,
        max_asset_bytes=max_asset_bytes,
        max_members=max_members,
    )


def _source_pdf_details(path: Path) -> tuple[str, int]:
    fd = _open_regular_fd(path, f"source PDF {path}")
    try:
        data = _read_fd(fd, f"source PDF {path}")
        source_sha256 = hashlib.sha256(data).hexdigest()
        with pymupdf.open(stream=data, filetype="pdf") as document:
            page_count = document.page_count
    except Exception as exc:
        raise MineruContentError(f"Cannot read source PDF: {path}") from exc
    finally:
        os.close(fd)
    if page_count < 0 or page_count > MAX_FULL_PAGE_COUNT:
        raise MineruContentError(
            f"Source PDF page count exceeds the 1000-page limit: {path}"
        )
    return source_sha256, page_count


def _normalize_direct(
    archive: zipfile.ZipFile,
    index: _ZipIndex,
    asset_store: _AssetStore,
    *,
    full_page_count: int | None,
    source_pdf_sha256: str | None,
    source_pdf_page_count: int | None,
) -> tuple[list[dict[str, Any]], int, str, int, str | None]:
    if any(name.startswith("segments/") for name in index.members):
        raise MineruContentError("Segmented response archive is missing manifest.json")
    if "manifest.json" in index.members:
        raise MineruContentError("Direct response archive cannot contain manifest.json")
    raw_items = _load_content_items(archive, index, "direct response")
    page_count = _direct_page_count(
        raw_items,
        full_page_count,
        source_pdf_page_count,
    )
    if page_count > MAX_FULL_PAGE_COUNT:
        raise MineruContentError("Direct response exceeds the 1000-page limit")
    items = _normalize_items(
        archive,
        index,
        raw_items,
        page_offset=0,
        page_limit=page_count,
        archive_key="direct",
        content_idx_start=0,
        asset_store=asset_store,
    )
    return items, page_count, "direct", 1, source_pdf_sha256


def _direct_page_count(
    raw_items: list[dict[str, Any]],
    explicit_page_count: int | None,
    source_pdf_page_count: int | None,
) -> int:
    if source_pdf_page_count is not None:
        if (
            explicit_page_count is not None
            and explicit_page_count != source_pdf_page_count
        ):
            raise MineruContentError(
                "Explicit full page count does not match the source PDF"
            )
        page_count = source_pdf_page_count
    elif explicit_page_count is not None:
        page_count = explicit_page_count
    else:
        page_count = max((item["page_idx"] for item in raw_items), default=-1) + 1
    if (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 0
    ):
        raise MineruContentError(
            "Direct full page count must be a non-negative integer"
        )
    if page_count > MAX_FULL_PAGE_COUNT:
        raise MineruContentError("Direct response exceeds the 1000-page limit")
    return page_count


def _normalize_segmented(
    outer: zipfile.ZipFile,
    outer_index: _ZipIndex,
    staging_path: Path,
    asset_store: _AssetStore,
    *,
    max_archive_bytes: int,
    max_uncompressed_bytes: int,
    max_member_bytes: int,
    max_members: int,
    source_pdf_sha256: str | None,
    source_pdf_page_count: int | None,
) -> tuple[list[dict[str, Any]], int, str, int, str]:
    manifest_info = outer_index.members["manifest.json"]
    manifest = _load_json_member(
        outer, manifest_info, "segmented manifest", max_member_bytes
    )
    if not isinstance(manifest, dict):
        raise MineruContentError("Segmented manifest must be a JSON object")
    manifest_source_pdf_sha256 = _validate_manifest(manifest, outer_index)
    page_count = _required_int(manifest, "source_page_count", minimum=1)
    if page_count > MAX_FULL_PAGE_COUNT:
        raise MineruContentError("Segmented manifest exceeds the 1000-page limit")
    segments = manifest["segments"]
    if not isinstance(segments, list) or not segments:
        raise MineruContentError("Segmented manifest must contain non-empty segments")
    if source_pdf_sha256 is not None:
        if source_pdf_sha256 != manifest_source_pdf_sha256:
            raise MineruContentError(
                "Source PDF SHA-256 does not match segmented manifest"
            )
        if source_pdf_page_count != page_count:
            raise MineruContentError(
                "Source PDF page count does not match segmented manifest"
            )

    expected_first_page = 1
    normalized: list[dict[str, Any]] = []
    content_idx = 0
    nested_dir = staging_path / ".nested"
    nested_dir.mkdir()
    nested_uncompressed_size = outer_index.total_size
    for position, raw_segment in enumerate(segments, start=1):
        if not isinstance(raw_segment, dict):
            raise MineruContentError(f"Segment {position} must be an object")
        index = _required_int(raw_segment, "index", minimum=1)
        if index != position:
            raise MineruContentError("Segment indexes must be ordered from 1")
        first_page = _required_int(raw_segment, "first_page", minimum=1)
        last_page = _required_int(raw_segment, "last_page", minimum=first_page)
        segment_page_count = _required_int(raw_segment, "page_count", minimum=1)
        if last_page - first_page + 1 != segment_page_count:
            raise MineruContentError(f"Segment {index} has an invalid page_count")
        if first_page != expected_first_page or last_page > page_count:
            raise MineruContentError("Segment page ranges overlap or contain a gap")
        if segment_page_count > MAX_MINERU_SEGMENT_PAGES:
            raise MineruContentError(f"Segment {index} exceeds the 200-page limit")
        response_archive = _required_string(raw_segment, "response_archive")
        response_name = _normalize_member_name(response_archive)
        if response_name not in outer_index.members:
            raise MineruContentError(
                f"Segment response archive is missing: {response_archive}"
            )
        expected_sha = _required_sha256(raw_segment, "response_sha256")
        nested_info = outer_index.members[response_name]
        if nested_info.is_dir():
            raise MineruContentError(
                f"Segment response is a directory: {response_name}"
            )
        if nested_info.file_size > max_archive_bytes:
            raise MineruContentError(
                f"Nested response exceeds the size limit: {response_name}"
            )
        nested_path = nested_dir / f"segment-{index:04d}.zip"
        actual_sha = _copy_member_to_file(
            outer, nested_info, nested_path, max_archive_bytes
        )
        if actual_sha != expected_sha:
            raise MineruContentError(
                f"Nested response SHA-256 mismatch for segment {index}"
            )
        try:
            with _open_zip(nested_path, f"segment {index} response") as nested:
                nested_index = _index_zip(
                    nested,
                    label=f"segment {index} response",
                    max_uncompressed_bytes=max_uncompressed_bytes,
                    max_member_bytes=max_member_bytes,
                    max_members=max_members,
                )
                nested_uncompressed_size += nested_index.total_size
                if nested_uncompressed_size > max_uncompressed_bytes:
                    raise MineruContentError(
                        "Segmented archive exceeds the uncompressed size limit"
                    )
                raw_items = _load_content_items(
                    nested, nested_index, f"segment {index} response"
                )
                normalized.extend(
                    _normalize_items(
                        nested,
                        nested_index,
                        raw_items,
                        page_offset=first_page - 1,
                        page_limit=segment_page_count,
                        archive_key=f"segment-{index}",
                        content_idx_start=content_idx,
                        asset_store=asset_store,
                    )
                )
                content_idx += len(raw_items)
        finally:
            nested_path.unlink(missing_ok=True)
        expected_first_page = last_page + 1

    nested_dir.rmdir()
    if expected_first_page != page_count + 1:
        raise MineruContentError("Segment page ranges do not cover the full book")
    if set(outer_index.members) != {
        "manifest.json",
        *(_normalize_member_name(segment["response_archive"]) for segment in segments),
    }:
        raise MineruContentError("Segmented archive contains unreferenced members")
    return (
        normalized,
        page_count,
        "segmented",
        len(segments),
        manifest_source_pdf_sha256,
    )


def _load_content_items(
    archive: zipfile.ZipFile,
    index: _ZipIndex,
    label: str,
) -> list[dict[str, Any]]:
    candidates = [
        name
        for name, info in index.members.items()
        if not info.is_dir() and name.endswith("_content_list.json")
    ]
    candidates = [
        name for name in candidates if not name.endswith("_content_list_v2.json")
    ]
    if len(candidates) != 1:
        if (
            any(name.endswith("_content_list_v2.json") for name in index.members)
            and not candidates
        ):
            raise MineruContentError(
                f"{label} contains only *_content_list_v2.json; standard content list is required"
            )
        raise MineruContentError(
            f"{label} must contain exactly one standard *_content_list.json"
        )
    raw = _load_json_member(
        archive,
        index.members[candidates[0]],
        f"{label} content list",
        max(index.members[candidates[0]].file_size, 1),
    )
    if not isinstance(raw, list):
        raise MineruContentError(f"{label} content list must be a JSON array")
    items: list[dict[str, Any]] = []
    for item_index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise MineruContentError(f"{label} item {item_index} must be a JSON object")
        item_type = item.get("type")
        if not isinstance(item_type, str) or not item_type.strip():
            raise MineruContentError(
                f"{label} item {item_index} must have a non-empty string type"
            )
        page_idx = item.get("page_idx")
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx < 0:
            raise MineruContentError(
                f"{label} item {item_index} has an invalid page_idx"
            )
        items.append(item)
    return items


def _normalize_items(
    archive: zipfile.ZipFile,
    index: _ZipIndex,
    raw_items: list[dict[str, Any]],
    *,
    page_offset: int,
    page_limit: int,
    archive_key: str,
    content_idx_start: int,
    asset_store: _AssetStore,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for local_index, item in enumerate(raw_items):
        local_page_idx = item["page_idx"]
        if local_page_idx >= page_limit:
            raise MineruContentError(
                f"Content item {local_index} has page_idx outside its response range"
            )
        rewritten = _rewrite_asset_paths(
            item,
            archive,
            index,
            archive_key=archive_key,
            asset_store=asset_store,
        )
        rewritten["page_idx"] = local_page_idx + page_offset
        rewritten["content_idx"] = content_idx_start + local_index
        normalized.append(rewritten)
    return normalized


def _rewrite_asset_paths(
    value: Any,
    archive: zipfile.ZipFile,
    index: _ZipIndex,
    *,
    archive_key: str,
    asset_store: _AssetStore,
) -> Any:
    if isinstance(value, list):
        return [
            _rewrite_asset_paths(
                child,
                archive,
                index,
                archive_key=archive_key,
                asset_store=asset_store,
            )
            for child in value
        ]
    if not isinstance(value, dict):
        return value

    rewritten: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            raise MineruContentError("Content item contains a non-string JSON key")
        if key in _ASSET_PATH_KEYS:
            rewritten[key] = _rewrite_asset_reference(
                child,
                archive,
                index,
                archive_key=archive_key,
                asset_store=asset_store,
            )
        else:
            rewritten[key] = _rewrite_asset_paths(
                child,
                archive,
                index,
                archive_key=archive_key,
                asset_store=asset_store,
            )
    return rewritten


def _rewrite_asset_reference(
    value: Any,
    archive: zipfile.ZipFile,
    index: _ZipIndex,
    *,
    archive_key: str,
    asset_store: _AssetStore,
) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [
            _rewrite_asset_reference(
                child,
                archive,
                index,
                archive_key=archive_key,
                asset_store=asset_store,
            )
            for child in value
        ]
    if not isinstance(value, str):
        raise MineruContentError("Asset path fields must contain strings or null")
    if value == "":
        return value
    member_name = _normalize_member_name(value)
    info = index.members.get(member_name)
    if info is None:
        raise MineruContentError(f"Referenced asset does not exist: {value}")
    return asset_store.copy(
        archive,
        info,
        member_name,
        archive_key=archive_key,
    )


def _validate_manifest(
    manifest: dict[str, Any],
    outer_index: _ZipIndex,
) -> str:
    if manifest.get("schema_version") != 1:
        raise MineruContentError("Segmented manifest has an unsupported schema_version")
    if manifest.get("stage") != 1:
        raise MineruContentError("Segmented manifest is not a Stage 1 manifest")
    if manifest.get("format") != "segmented-mineru-responses":
        raise MineruContentError("Segmented manifest has an unsupported format")
    source_pdf = _required_string(manifest, "source_pdf")
    _normalize_member_name(source_pdf)
    if source_pdf != "source/source.pdf":
        raise MineruContentError(
            "Segmented manifest source_pdf must be exactly source/source.pdf"
        )
    source_pdf_sha256 = _required_sha256(manifest, "source_pdf_sha256")
    segment_limit = _required_int(manifest, "segment_page_limit", minimum=1)
    if segment_limit != MAX_MINERU_SEGMENT_PAGES:
        raise MineruContentError("Segmented manifest has an unsupported page limit")
    segments = manifest.get("segments")
    if not isinstance(segments, list):
        raise MineruContentError("Segmented manifest segments must be a JSON array")
    response_names: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise MineruContentError("Segment entries must be JSON objects")
        _required_string(segment, "uploaded_file")
        _required_string(segment, "batch_id")
        name = _normalize_member_name(_required_string(segment, "response_archive"))
        if not name.endswith(".zip"):
            raise MineruContentError(
                f"Segment response archive must be a .zip file: {name}"
            )
        if name in response_names:
            raise MineruContentError("Segment response archives must be unique")
        response_names.add(name)
        if name == "manifest.json" or name not in outer_index.members:
            raise MineruContentError(f"Invalid segment response archive: {name}")
    return source_pdf_sha256


def _index_zip(
    archive: zipfile.ZipFile,
    *,
    label: str,
    max_uncompressed_bytes: int,
    max_member_bytes: int,
    max_members: int,
) -> _ZipIndex:
    try:
        infos = archive.infolist()
    except Exception as exc:
        raise MineruContentError(f"Cannot read ZIP members for {label}: {exc}") from exc
    if len(infos) > max_members:
        raise MineruContentError(f"{label} contains too many ZIP members")
    members: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in infos:
        name = _normalize_member_name(info.filename, allow_directory=True)
        if name in members:
            raise MineruContentError(f"{label} contains duplicate ZIP member: {name}")
        if _is_symlink(info):
            raise MineruContentError(f"{label} contains a symlink member: {name}")
        if info.file_size < 0 or info.compress_size < 0:
            raise MineruContentError(f"{label} contains an invalid ZIP size: {name}")
        if info.file_size > max_member_bytes:
            raise MineruContentError(f"ZIP member exceeds the size limit: {name}")
        total_size += info.file_size
        if total_size > max_uncompressed_bytes:
            raise MineruContentError(f"{label} exceeds the uncompressed size limit")
        members[name] = info
    return _ZipIndex(members, total_size)


def _load_json_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    label: str,
    max_bytes: int,
) -> Any:
    data = _read_member(archive, info, label, max_bytes)
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MineruContentError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MineruContentError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise MineruContentError(f"Invalid JSON constant: {value}")


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    label: str,
    max_bytes: int,
) -> bytes:
    if info.file_size > max_bytes:
        raise MineruContentError(f"{label} exceeds the size limit")
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(info, mode="r") as source:
            while True:
                chunk = source.read(_COPY_BUFFER_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MineruContentError(f"{label} exceeds the size limit")
                chunks.append(chunk)
    except MineruContentError:
        raise
    except Exception as exc:
        raise MineruContentError(f"Cannot read {label}: {exc}") from exc
    if total != info.file_size:
        raise MineruContentError(f"{label} is truncated")
    return b"".join(chunks)


def _copy_member_to_file(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    max_bytes: int,
) -> str:
    if info.file_size > max_bytes:
        raise MineruContentError(f"ZIP member exceeds the size limit: {info.filename}")
    digest = hashlib.sha256()
    total = 0
    try:
        with destination.open("wb") as target, archive.open(info, mode="r") as source:
            while True:
                chunk = source.read(_COPY_BUFFER_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MineruContentError(
                        f"ZIP member exceeds the size limit: {info.filename}"
                    )
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except MineruContentError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise MineruContentError(
            f"Cannot read nested ZIP member {info.filename}: {exc}"
        ) from exc
    if total != info.file_size:
        destination.unlink(missing_ok=True)
        raise MineruContentError(f"Nested ZIP member is truncated: {info.filename}")
    return digest.hexdigest()


@contextmanager
def _open_raw_archive(
    path: Path,
    *,
    max_archive_bytes: int,
):
    fd = _open_regular_fd(path, f"raw archive {path}")
    try:
        archive_stat = os.fstat(fd)
        archive_size = archive_stat.st_size
        if archive_size > max_archive_bytes:
            raise MineruContentError(
                f"Raw MinerU archive exceeds the size limit: {path}"
            )
        with os.fdopen(fd, "rb", closefd=False) as source:
            source_sha256 = _hash_fd(source.fileno(), f"raw archive {path}")
            source.seek(0)
            try:
                archive = zipfile.ZipFile(source, mode="r", allowZip64=True)
            except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
                raise MineruContentError(
                    f"Cannot open raw archive {path} as a ZIP: {exc}"
                ) from exc
            with archive:
                yield archive, archive_size, source_sha256
    finally:
        os.close(fd)


def _open_zip(path: Path, label: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path, mode="r", allowZip64=True)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise MineruContentError(f"Cannot open {label} as a ZIP: {exc}") from exc


def _normalize_member_name(name: str, *, allow_directory: bool = False) -> str:
    if not isinstance(name, str) or not name:
        raise MineruContentError("ZIP member paths must be non-empty strings")
    if "\x00" in name or "\\" in name:
        raise MineruContentError(f"Unsafe ZIP member path: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise MineruContentError(f"Unsafe ZIP member path: {name!r}")
    directory = name.endswith("/")
    if directory and not allow_directory:
        raise MineruContentError(f"Asset path points to a directory: {name!r}")
    body = name[:-1] if directory else name
    parts = body.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise MineruContentError(f"Unsafe ZIP member path: {name!r}")
    normalized = "/".join(parts)
    return normalized + ("/" if directory else "")


def _safe_asset_basename(member_name: str) -> str:
    basename = PurePosixPath(member_name).name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", basename)
    if safe in {"", ".", ".."}:
        safe = "asset"
    return safe[:128]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MineruContentError(f"Manifest field {key} must be a non-empty string")
    return value


def _required_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise MineruContentError(
            f"Manifest field {key} must be an integer >= {minimum}"
        )
    return value


def _required_sha256(payload: Mapping[str, Any], key: str) -> str:
    value = _required_string(payload, key)
    if _HEX_SHA256.fullmatch(value) is None:
        raise MineruContentError(f"Manifest field {key} must be a lowercase SHA-256")
    return value


def _validate_limits(
    max_archive_bytes: int,
    max_uncompressed_bytes: int,
    max_member_bytes: int | None,
    max_asset_bytes: int | None,
    max_members: int,
) -> None:
    for name, value in (
        ("max_archive_bytes", max_archive_bytes),
        ("max_uncompressed_bytes", max_uncompressed_bytes),
        ("max_member_bytes", max_member_bytes),
        ("max_asset_bytes", max_asset_bytes),
        ("max_members", max_members),
    ):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise ValueError(f"{name} must be a positive integer")


def _open_regular_fd(path: Path, label: str) -> int:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise MineruContentError(
            f"Cannot safely open {label}: POSIX O_NOFOLLOW is unavailable"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MineruContentError(
                f"Cannot safely open {label}: symlink rejected"
            ) from exc
        raise MineruContentError(f"Cannot safely open {label}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MineruContentError(f"{label} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_fd(fd: int, label: str) -> bytes:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, _COPY_BUFFER_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise MineruContentError(f"Cannot read {label}: {exc}") from exc


def _hash_fd(fd: int, label: str) -> str:
    digest = hashlib.sha256()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, _COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    except OSError as exc:
        raise MineruContentError(f"Cannot read {label}: {exc}") from exc
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _directory_open_flags() -> int:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise MineruContentError(
            "Cannot safely compare existing output: POSIX directory no-follow "
            "support is unavailable"
        )
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY


def _existing_output_matches(
    output_path: Path,
    staging_path: Path,
    expected_assets: dict[str, dict[str, int | str]],
) -> bool:
    expected_content = (staging_path / "content.json").read_bytes()
    expected_asset_names: set[str] = set()
    for relative_name in expected_assets:
        relative = PurePosixPath(relative_name)
        if relative.parts[:1] != ("assets",) or len(relative.parts) != 2:
            return False
        expected_asset_names.add(relative.parts[1])

    try:
        directory_flags = _directory_open_flags()
        root_fd = os.open(output_path, directory_flags)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return False
    except OSError:
        return False
    try:
        if set(os.listdir(root_fd)) != {"content.json", "assets"}:
            return False
        content_fd = os.open(
            "content.json",
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            if not _fd_matches_bytes(content_fd, expected_content):
                return False
        finally:
            os.close(content_fd)

        assets_fd = os.open("assets", directory_flags, dir_fd=root_fd)
        try:
            if set(os.listdir(assets_fd)) != expected_asset_names:
                return False
            for relative_name, metadata in expected_assets.items():
                filename = PurePosixPath(relative_name).name
                expected_sha256 = metadata.get("sha256")
                expected_size = metadata.get("size")
                if (
                    not isinstance(expected_sha256, str)
                    or not isinstance(expected_size, int)
                    or isinstance(expected_size, bool)
                ):
                    return False
                asset_fd = os.open(
                    filename,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=assets_fd,
                )
                try:
                    if not _fd_matches_asset(
                        asset_fd,
                        expected_sha256,
                        expected_size,
                    ):
                        return False
                finally:
                    os.close(asset_fd)
        finally:
            os.close(assets_fd)
        return True
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return False
    finally:
        os.close(root_fd)


def _fd_matches_bytes(fd: int, expected: bytes) -> bool:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size != len(expected):
        return False
    actual = _read_fd(fd, "existing content.json")
    after = os.fstat(fd)
    return (
        stat.S_ISREG(after.st_mode)
        and after.st_size == before.st_size
        and actual == expected
    )


def _fd_matches_asset(fd: int, expected_sha256: str, expected_size: int) -> bool:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
        return False
    digest = hashlib.sha256()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, _COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    except OSError:
        return False
    after = os.fstat(fd)
    return (
        stat.S_ISREG(after.st_mode)
        and after.st_size == before.st_size
        and digest.hexdigest() == expected_sha256
    )


def _publish_directory(staged_path: Path, output_path: Path) -> None:
    backup_path: Path | None = None
    if output_path.exists() or output_path.is_symlink():
        backup_path = Path(
            tempfile.mkdtemp(
                prefix=f".{output_path.name}.backup-", dir=output_path.parent
            )
        )
        shutil.rmtree(backup_path)
        try:
            os.replace(output_path, backup_path)
        except OSError as exc:
            raise MineruContentError(
                f"Cannot prepare previous content output for replacement: {exc}"
            ) from exc
    try:
        os.replace(staged_path, output_path)
    except BaseException:
        if backup_path is not None:
            try:
                os.replace(backup_path, output_path)
            except OSError as restore_error:
                raise MineruContentError(
                    "Cannot publish content output and cannot restore the previous output"
                ) from restore_error
        raise
    if backup_path is not None:
        try:
            _remove_path(backup_path)
        except OSError:
            pass


def _remove_path(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


__all__ = [
    "CONTENT_SCHEMA",
    "CONTENT_SCHEMA_VERSION",
    "MineruContentError",
    "normalize_content",
    "normalize_mineru_content",
]
