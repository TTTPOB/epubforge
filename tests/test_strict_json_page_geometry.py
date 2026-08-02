"""Direct tests for the shared JSON and page geometry contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from epubforge.page_geometry import (
    PageGeometryError,
    content_source_sha256,
    normalize_page_geometry,
)
from epubforge.strict_json import (
    StrictJsonError,
    parse_json_document,
    read_json_document,
)


def test_parse_json_rejects_duplicate_keys_and_non_finite_numbers() -> None:
    with pytest.raises(StrictJsonError, match="Duplicate JSON object key"):
        parse_json_document(b'{"value": 1, "value": 2}', label="payload")

    with pytest.raises(StrictJsonError, match="Non-finite JSON number"):
        parse_json_document(b'{"value": NaN}', label="payload")


def test_parse_json_enforces_utf8_and_byte_limit() -> None:
    with pytest.raises(StrictJsonError, match="valid UTF-8 JSON"):
        parse_json_document(b"\xff", label="payload")

    with pytest.raises(StrictJsonError, match="size limit"):
        parse_json_document(b"{}", label="payload", max_bytes=1)


def test_read_json_rejects_symlinks_and_returns_the_snapshot_bytes(
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "payload.json"
    payload_bytes = json.dumps({"value": "stable"}).encode("utf-8")
    payload_path.write_bytes(payload_bytes)

    value, raw = read_json_document(payload_path, "payload")

    assert value == {"value": "stable"}
    assert raw == payload_bytes

    link = tmp_path / "link.json"
    link.symlink_to(payload_path)
    with pytest.raises(StrictJsonError, match="symlink"):
        read_json_document(link, "payload")


def test_page_geometry_normalizes_dimensions_and_hashes_int_float_equally() -> None:
    integer = [{"page_idx": 0, "width": 100, "height": 120}]
    floating = [{"page_idx": 0, "width": 100.0, "height": 120.0}]

    assert normalize_page_geometry(integer, page_count=1) == (
        {"page_idx": 0, "width": 100.0, "height": 120.0},
    )
    assert content_source_sha256([], integer) == content_source_sha256([], floating)


@pytest.mark.parametrize(
    ("value", "page_count", "match"),
    [
        ("not-an-array", 1, "array"),
        ([{"page_idx": 0, "width": 100, "height": 120}], 2, "exactly one"),
        ([{"page_idx": 1, "width": 100, "height": 120}], 1, "ordered"),
        ([{"page_idx": 0, "width": 0, "height": 120}], 1, "width is invalid"),
        (
            [{"page_idx": 0, "width": float("nan"), "height": 120}],
            1,
            "width is invalid",
        ),
    ],
)
def test_page_geometry_rejects_invalid_records(
    value: object, page_count: int, match: str
) -> None:
    with pytest.raises(PageGeometryError, match=match):
        normalize_page_geometry(value, page_count=page_count)
