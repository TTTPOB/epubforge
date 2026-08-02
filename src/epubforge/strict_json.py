"""Read JSON documents from stable, regular-file snapshots."""

from __future__ import annotations

import errno
import json
import math
import os
from pathlib import Path
import stat
from typing import Any


DEFAULT_MAX_JSON_BYTES = 64 * 1024 * 1024
_READ_BUFFER_SIZE = 1024 * 1024


class StrictJsonError(ValueError):
    """Raised when a JSON document violates the strict input contract."""


def read_json_document(
    path: str | Path,
    label: str,
    *,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
) -> tuple[Any, bytes]:
    """Read and parse one fd-backed JSON snapshot.

    The returned bytes and value come from the same opened descriptor. The
    descriptor stays independent from later replacement of the path name.
    """
    path = Path(path)
    data = _read_regular_snapshot(path, label, max_bytes=max_bytes)
    return parse_json_document(data, label=label, max_bytes=max_bytes), data


def parse_json_document(
    data: bytes,
    *,
    label: str,
    max_bytes: int | None = None,
) -> Any:
    """Parse strict UTF-8 JSON bytes with duplicate and non-finite rejection."""
    if max_bytes is not None:
        _validate_limit(max_bytes)
        if len(data) > max_bytes:
            raise StrictJsonError(f"{label} exceeds the size limit")
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except StrictJsonError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StrictJsonError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _read_regular_snapshot(path: Path, label: str, *, max_bytes: int) -> bytes:
    _validate_limit(max_bytes)
    fd = _open_regular_fd(path, label)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise StrictJsonError(f"{label} is not a regular file: {path}")
        if before.st_size > max_bytes:
            raise StrictJsonError(f"{label} exceeds the size limit: {path}")

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, _READ_BUFFER_SIZE)
            except OSError as exc:
                raise StrictJsonError(f"Cannot read {label}: {path}: {exc}") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise StrictJsonError(f"{label} exceeds the size limit: {path}")
            chunks.append(chunk)

        after = os.fstat(fd)
        if not _same_snapshot(before, after) or total != before.st_size:
            raise StrictJsonError(f"{label} changed while being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _same_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_mode == after.st_mode
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def _open_regular_fd(path: Path, label: str) -> int:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise StrictJsonError(
            f"Cannot safely open {label}: POSIX O_NOFOLLOW is unavailable"
        )

    if path.is_absolute():
        components = path.parts[1:]
        try:
            directory_fd = os.open("/", os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        except OSError as exc:
            raise StrictJsonError(f"Cannot safely open {label}: {exc}") from exc
    else:
        components = path.parts
        try:
            directory_fd = os.open(".", os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        except OSError as exc:
            raise StrictJsonError(f"Cannot safely open {label}: {exc}") from exc

    if not components or any(part in {"", ".", ".."} for part in components):
        os.close(directory_fd)
        raise StrictJsonError(f"Cannot safely open {label}: unsafe path")

    fd: int | None = None
    try:
        for component in components[:-1]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise _open_error(exc, label) from exc
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            fd = os.open(
                components[-1],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                fd = None
                raise StrictJsonError(f"{label} is not a regular file: {path}")
            return fd
        except OSError as exc:
            if fd is not None:
                os.close(fd)
                fd = None
            raise _open_error(exc, label) from exc
    finally:
        os.close(directory_fd)


def _open_error(exc: OSError, label: str) -> StrictJsonError:
    if exc.errno == errno.ELOOP:
        return StrictJsonError(f"{label} is a symlink and cannot be read")
    return StrictJsonError(f"Cannot safely open {label}: {exc}")


def _validate_limit(max_bytes: int) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise StrictJsonError(f"Non-finite JSON number is not allowed: {value}")


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise StrictJsonError(f"Non-finite JSON number is not allowed: {value}")
    return parsed


__all__ = [
    "DEFAULT_MAX_JSON_BYTES",
    "StrictJsonError",
    "parse_json_document",
    "read_json_document",
]
