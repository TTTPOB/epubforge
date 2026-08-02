"""Focused tests for the Stage 1 MinerU content normalizer."""

from __future__ import annotations

from hashlib import sha256
import io
import json
from pathlib import Path
import warnings
import zipfile

import pytest
import pymupdf

from epubforge.mineru_content import (
    MineruContentError,
    normalize_mineru_content,
)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _content_list(items: list[dict[str, object]], name: str = "book") -> bytes:
    return json.dumps(items).encode("utf-8")


def _write_direct(
    tmp_path: Path,
    *,
    items: list[dict[str, object]],
    assets: dict[str, bytes] | None = None,
    include_v2: bool = True,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    members = {f"book_content_list.json": _content_list(items)}
    if include_v2:
        members["book_content_list_v2.json"] = _content_list(
            [{"type": "text", "text": "v2", "page_idx": 0}]
        )
    for name, content in (assets or {}).items():
        members[name] = content
    raw = tmp_path / "01_raw.zip"
    raw.write_bytes(_zip_bytes(members))
    return raw


def _segment_response(
    *,
    page_idx: int,
    asset_name: str,
    asset_content: bytes,
) -> bytes:
    return _zip_bytes(
        {
            "part_content_list.json": _content_list(
                [
                    {
                        "type": "image",
                        "img_path": asset_name,
                        "image_caption": ["caption"],
                        "page_idx": page_idx,
                        "bbox": [1, 2, 3, 4],
                    }
                ]
            ),
            "part_content_list_v2.json": b"[]",
            asset_name: asset_content,
        }
    )


def _write_segmented(
    tmp_path: Path, *, gap: bool = False, bad_sha: bool = False
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    first = _segment_response(
        page_idx=0,
        asset_name="images/cover.png",
        asset_content=b"first asset",
    )
    second = _segment_response(
        page_idx=0,
        asset_name="images/cover.png",
        asset_content=b"second asset",
    )
    second_first_page = 4 if gap else 3
    manifest = {
        "schema_version": 1,
        "stage": 1,
        "format": "segmented-mineru-responses",
        "source_pdf": "source/source.pdf",
        "source_pdf_sha256": "a" * 64,
        "source_page_count": 4,
        "segment_page_limit": 200,
        "segments": [
            {
                "index": 1,
                "first_page": 1,
                "last_page": 2,
                "page_count": 2,
                "uploaded_file": "part-1.pdf",
                "batch_id": "batch-1",
                "response_archive": "segments/one.zip",
                "response_sha256": sha256(first).hexdigest(),
            },
            {
                "index": 2,
                "first_page": second_first_page,
                "last_page": 4,
                "page_count": 1 if gap else 2,
                "uploaded_file": "part-2.pdf",
                "batch_id": "batch-2",
                "response_archive": "segments/two.zip",
                "response_sha256": (
                    "b" * 64 if bad_sha else sha256(second).hexdigest()
                ),
            },
        ],
    }
    raw = tmp_path / "01_raw.zip"
    raw.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(manifest).encode("utf-8"),
                "segments/one.zip": first,
                "segments/two.zip": second,
            }
        )
    )
    return raw


def _write_single_segmented(
    tmp_path: Path,
    *,
    source_pdf: str = "source/source.pdf",
    source_sha256: str = "a" * 64,
    source_page_count: int = 1,
    asset_content: bytes = b"asset",
    manifest_bytes: bytes | None = None,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    nested = _segment_response(
        page_idx=0,
        asset_name="images/cover.png",
        asset_content=asset_content,
    )
    manifest = {
        "schema_version": 1,
        "stage": 1,
        "format": "segmented-mineru-responses",
        "source_pdf": source_pdf,
        "source_pdf_sha256": source_sha256,
        "source_page_count": source_page_count,
        "segment_page_limit": 200,
        "segments": [
            {
                "index": 1,
                "first_page": 1,
                "last_page": source_page_count,
                "page_count": source_page_count,
                "uploaded_file": "part.pdf",
                "batch_id": "batch",
                "response_archive": "segments/one.zip",
                "response_sha256": sha256(nested).hexdigest(),
            }
        ],
    }
    raw = tmp_path / "01_raw.zip"
    raw.write_bytes(
        _zip_bytes(
            {
                "manifest.json": (
                    json.dumps(manifest).encode("utf-8")
                    if manifest_bytes is None
                    else manifest_bytes
                ),
                "segments/one.zip": nested,
            }
        )
    )
    return raw


def test_direct_normalization_selects_standard_list_and_rewrites_asset(
    tmp_path: Path,
) -> None:
    raw = _write_direct(
        tmp_path,
        items=[
            {
                "type": "image",
                "text": "preserve me",
                "img_path": "images/figure.png",
                "image_caption": ["caption"],
                "page_idx": 1,
                "bbox": [1, 2, 3, 4],
                "text_level": 2,
            }
        ],
        assets={"images/figure.png": b"figure"},
    )

    output = normalize_mineru_content(raw)
    payload = json.loads((output / "content.json").read_text(encoding="utf-8"))
    item = payload["items"][0]

    assert payload["schema_version"] == 1
    assert payload["source_archive_sha256"] == sha256(raw.read_bytes()).hexdigest()
    assert payload["page_count"] == 2
    assert item["content_idx"] == 0
    assert item["page_idx"] == 1
    assert item["text"] == "preserve me"
    assert item["text_level"] == 2
    assert item["image_caption"] == ["caption"]
    assert item["img_path"].startswith("assets/")
    assert (output / item["img_path"]).read_bytes() == b"figure"
    assert item["img_path"] != "images/figure.png"


def test_direct_page_count_uses_stage1_source_pdf_when_present(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_pdf = source_dir / "source.pdf"
    document = pymupdf.open()
    try:
        for _ in range(3):
            document.new_page()
        document.save(str(source_pdf))
    finally:
        document.close()
    raw = _write_direct(
        tmp_path,
        items=[{"type": "text", "text": "first page", "page_idx": 0}],
    )

    output = normalize_mineru_content(raw, work_dir=tmp_path)
    payload = json.loads((output / "content.json").read_text(encoding="utf-8"))

    assert payload["page_count"] == 3


def test_empty_optional_asset_path_is_preserved(tmp_path: Path) -> None:
    raw = _write_direct(
        tmp_path,
        items=[{"type": "image", "img_path": "", "page_idx": 0}],
    )

    output = normalize_mineru_content(raw)
    payload = json.loads((output / "content.json").read_text(encoding="utf-8"))

    assert payload["items"][0]["img_path"] == ""
    assert payload["assets"] == {}


@pytest.mark.parametrize(
    "item",
    [
        {"type": "", "page_idx": 0},
        {"type": "   ", "page_idx": 0},
        {"type": 123, "page_idx": 0},
        {"page_idx": 0},
    ],
)
def test_content_items_require_non_empty_string_type(
    tmp_path: Path, item: dict[str, object]
) -> None:
    raw = _write_direct(tmp_path, items=[item])

    with pytest.raises(MineruContentError, match="non-empty string type"):
        normalize_mineru_content(raw)


def test_duplicate_keys_are_rejected_in_content_and_manifest(tmp_path: Path) -> None:
    content = tmp_path / "duplicate-content.zip"
    content.write_bytes(
        _zip_bytes(
            {"book_content_list.json": (b'[{"type":"text","page_idx":0,"page_idx":1}]')}
        )
    )
    with pytest.raises(MineruContentError, match="Duplicate JSON object key"):
        normalize_mineru_content(content)

    manifest = (
        b'{"schema_version":1,"stage":1,"format":"segmented-mineru-responses",'
        b'"source_pdf":"source/source.pdf","source_pdf_sha256":"'
        + (b"a" * 64)
        + b'","source_page_count":1,"source_page_count":1,'
        b'"segment_page_limit":200,"segments":[]}'
    )
    duplicate_manifest = _write_single_segmented(
        tmp_path / "duplicate-manifest",
        manifest_bytes=manifest,
    )
    with pytest.raises(MineruContentError, match="Duplicate JSON object key"):
        normalize_mineru_content(duplicate_manifest)


def test_segmented_source_pdf_sha_and_page_count_are_verified(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_pdf = source_dir / "source.pdf"
    document = pymupdf.open()
    try:
        document.new_page()
        document.save(str(source_pdf))
    finally:
        document.close()
    source_sha256 = sha256(source_pdf.read_bytes()).hexdigest()

    bad_sha = _write_single_segmented(tmp_path / "bad-sha")
    with pytest.raises(MineruContentError, match="Source PDF SHA-256"):
        normalize_mineru_content(bad_sha, source_pdf=source_pdf)

    bad_page_count = _write_single_segmented(
        tmp_path / "bad-pages",
        source_sha256=source_sha256,
        source_page_count=2,
    )
    with pytest.raises(MineruContentError, match="Source PDF page count"):
        normalize_mineru_content(bad_page_count, source_pdf=source_pdf)


def test_source_pdf_hash_and_pages_use_one_open_file(
    monkeypatch, tmp_path: Path
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_pdf = source_dir / "source.pdf"
    document = pymupdf.open()
    try:
        document.new_page()
        document.save(str(source_pdf))
    finally:
        document.close()
    original_bytes = source_pdf.read_bytes()
    raw = _write_single_segmented(
        tmp_path / "raw",
        source_sha256=sha256(original_bytes).hexdigest(),
    )

    replacement = tmp_path / "replacement.pdf"
    document = pymupdf.open()
    try:
        document.new_page()
        document.new_page()
        document.save(str(replacement))
    finally:
        document.close()
    real_open = pymupdf.open
    replaced = False

    def replace_path_after_read(*args, **kwargs):
        nonlocal replaced
        if kwargs.get("stream") is not None and not replaced:
            replaced = True
            source_pdf.unlink()
            source_pdf.write_bytes(replacement.read_bytes())
        return real_open(*args, **kwargs)

    monkeypatch.setattr(pymupdf, "open", replace_path_after_read)
    output = normalize_mineru_content(raw, source_pdf=source_pdf)
    payload = json.loads((output / "content.json").read_text(encoding="utf-8"))

    assert replaced
    assert payload["source_pdf_sha256"] == sha256(original_bytes).hexdigest()
    assert payload["page_count"] == 1


def test_source_pdf_symlink_is_rejected(tmp_path: Path) -> None:
    real_pdf = tmp_path / "real.pdf"
    document = pymupdf.open()
    try:
        document.new_page()
        document.save(str(real_pdf))
    finally:
        document.close()
    source_pdf = tmp_path / "source.pdf"
    source_pdf.symlink_to(real_pdf)
    raw = _write_direct(
        tmp_path / "raw",
        items=[{"type": "text", "page_idx": 0}],
    )

    with pytest.raises(MineruContentError, match="symlink rejected"):
        normalize_mineru_content(raw, source_pdf=source_pdf)


def test_unsafe_manifest_source_pdf_path_is_rejected(tmp_path: Path) -> None:
    raw = _write_single_segmented(tmp_path, source_pdf="../source.pdf")

    with pytest.raises(MineruContentError, match="Unsafe ZIP member path"):
        normalize_mineru_content(raw)

    wrong_safe_path = _write_single_segmented(
        tmp_path / "wrong-safe", source_pdf="other/source.pdf"
    )
    with pytest.raises(MineruContentError, match="exactly source/source.pdf"):
        normalize_mineru_content(wrong_safe_path)


def test_nested_archive_member_limits_are_enforced(tmp_path: Path) -> None:
    raw = _write_single_segmented(
        tmp_path,
        asset_content=b"zero" * 20_000,
    )

    with pytest.raises(MineruContentError, match="size limit"):
        normalize_mineru_content(raw, max_uncompressed_bytes=2_000)


def test_fresh_skip_rebuilds_tampered_items_and_assets(tmp_path: Path) -> None:
    raw = _write_direct(
        tmp_path,
        items=[
            {
                "type": "image",
                "text": "original",
                "img_path": "images/figure.png",
                "page_idx": 0,
            }
        ],
        assets={"images/figure.png": b"original asset"},
    )
    output = normalize_mineru_content(raw)
    payload_path = output / "content.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    asset_path = output / payload["items"][0]["img_path"]

    payload["items"][0]["text"] = "tampered"
    payload["items_sha256"] = sha256(
        json.dumps(
            payload["items"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    normalize_mineru_content(raw)
    restored = json.loads(payload_path.read_text(encoding="utf-8"))
    assert restored["items"][0]["text"] == "original"

    restored["assets"][restored["items"][0]["img_path"]] = {
        "sha256": sha256(b"coordinated asset").hexdigest(),
        "size": len(b"coordinated asset"),
    }
    asset_path.write_bytes(b"coordinated asset")
    payload_path.write_text(json.dumps(restored), encoding="utf-8")
    normalize_mineru_content(raw)
    assert asset_path.is_file()
    assert asset_path.read_bytes() == b"original asset"

    asset_path.write_bytes(b"replaced asset")
    normalize_mineru_content(raw)
    assert asset_path.read_bytes() == b"original asset"

    asset_path.unlink()
    outside = tmp_path / "outside-asset"
    outside.write_bytes(b"outside")
    asset_path.symlink_to(outside)
    normalize_mineru_content(raw)
    assert asset_path.is_file()
    assert not asset_path.is_symlink()
    assert asset_path.read_bytes() == b"original asset"

    previous_content = payload_path.read_bytes()
    with pytest.raises(MineruContentError, match="asset exceeds the size limit"):
        normalize_mineru_content(raw, max_asset_bytes=1)
    assert payload_path.read_bytes() == previous_content


def test_segmented_normalization_offsets_pages_and_separates_colliding_assets(
    tmp_path: Path,
) -> None:
    raw = _write_segmented(tmp_path)

    output = normalize_mineru_content(raw)
    payload = json.loads((output / "content.json").read_text(encoding="utf-8"))
    items = payload["items"]

    assert payload["source_kind"] == "segmented"
    assert payload["segment_count"] == 2
    assert payload["page_count"] == 4
    assert [item["content_idx"] for item in items] == [0, 1]
    assert [item["page_idx"] for item in items] == [0, 2]
    assert items[0]["img_path"] != items[1]["img_path"]
    assert (output / items[0]["img_path"]).read_bytes() == b"first asset"
    assert (output / items[1]["img_path"]).read_bytes() == b"second asset"


def test_v2_only_archive_is_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "01_raw.zip"
    raw.write_bytes(
        _zip_bytes({"book_content_list_v2.json": b"[]", "origin.pdf": b"pdf"})
    )

    with pytest.raises(MineruContentError, match="standard content list"):
        normalize_mineru_content(raw)


@pytest.mark.parametrize(
    ("item", "assets", "match"),
    [
        (
            {"type": "image", "img_path": "../secret", "page_idx": 0},
            {},
            "Unsafe ZIP member path",
        ),
        (
            {"type": "image", "img_path": "images/missing.png", "page_idx": 0},
            {},
            "does not exist",
        ),
    ],
)
def test_invalid_content_references_and_page_ranges_fail(
    tmp_path: Path,
    item: dict[str, object],
    assets: dict[str, bytes],
    match: str,
) -> None:
    raw = _write_direct(tmp_path, items=[item], assets=assets)

    with pytest.raises(MineruContentError, match=match):
        normalize_mineru_content(raw)
    assert not (tmp_path / "02_content").exists()


def test_segmented_item_page_idx_must_fit_local_range(tmp_path: Path) -> None:
    first = _segment_response(
        page_idx=2,
        asset_name="images/cover.png",
        asset_content=b"first asset",
    )
    second = _segment_response(
        page_idx=0,
        asset_name="images/cover.png",
        asset_content=b"second asset",
    )
    manifest = {
        "schema_version": 1,
        "stage": 1,
        "format": "segmented-mineru-responses",
        "source_pdf": "source/source.pdf",
        "source_pdf_sha256": "a" * 64,
        "source_page_count": 4,
        "segment_page_limit": 200,
        "segments": [
            {
                "index": 1,
                "first_page": 1,
                "last_page": 2,
                "page_count": 2,
                "uploaded_file": "part-1.pdf",
                "batch_id": "batch-1",
                "response_archive": "segments/one.zip",
                "response_sha256": sha256(first).hexdigest(),
            },
            {
                "index": 2,
                "first_page": 3,
                "last_page": 4,
                "page_count": 2,
                "uploaded_file": "part-2.pdf",
                "batch_id": "batch-2",
                "response_archive": "segments/two.zip",
                "response_sha256": sha256(second).hexdigest(),
            },
        ],
    }
    raw = tmp_path / "01_raw.zip"
    raw.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(manifest).encode("utf-8"),
                "segments/one.zip": first,
                "segments/two.zip": second,
            }
        )
    )

    with pytest.raises(MineruContentError, match="outside its response range"):
        normalize_mineru_content(raw)


def test_segmented_bad_sha_and_ranges_are_rejected(tmp_path: Path) -> None:
    bad_sha = _write_segmented(tmp_path / "sha", bad_sha=True)
    with pytest.raises(MineruContentError, match="SHA-256 mismatch"):
        normalize_mineru_content(bad_sha)

    gap = _write_segmented(tmp_path / "gap", gap=True)
    with pytest.raises(MineruContentError, match="overlap or contain a gap"):
        normalize_mineru_content(gap)


def test_malformed_direct_zip_and_json_are_rejected(tmp_path: Path) -> None:
    malformed_zip = tmp_path / "bad.zip"
    malformed_zip.write_bytes(b"not a zip")
    with pytest.raises(MineruContentError, match="as a ZIP"):
        normalize_mineru_content(malformed_zip)

    malformed_json = tmp_path / "json.zip"
    malformed_json.write_bytes(_zip_bytes({"book_content_list.json": b"{"}))
    with pytest.raises(MineruContentError, match="valid UTF-8 JSON"):
        normalize_mineru_content(malformed_json)


def test_unsafe_duplicate_and_oversized_zip_members_are_rejected(
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "unsafe.zip"
    unsafe.write_bytes(
        _zip_bytes(
            {
                "../outside": b"x",
                "book_content_list.json": b"[]",
            }
        )
    )
    with pytest.raises(MineruContentError, match="Unsafe ZIP member path"):
        normalize_mineru_content(unsafe)

    duplicate = tmp_path / "duplicate.zip"
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, mode="w") as archive:
            archive.writestr("book_content_list.json", b"[]")
            archive.writestr("book_content_list.json", b"[]")
    duplicate.write_bytes(buffer.getvalue())
    with pytest.raises(MineruContentError, match="duplicate ZIP member"):
        normalize_mineru_content(duplicate)

    oversized = _write_direct(
        tmp_path / "oversized",
        items=[{"type": "text", "page_idx": 0}],
    )
    with pytest.raises(MineruContentError, match="size limit"):
        normalize_mineru_content(oversized, max_archive_bytes=1)


def test_nested_truncation_is_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "01_raw.zip"
    nested = b"truncated nested zip"
    manifest = {
        "schema_version": 1,
        "stage": 1,
        "format": "segmented-mineru-responses",
        "source_pdf": "source/source.pdf",
        "source_pdf_sha256": "a" * 64,
        "source_page_count": 1,
        "segment_page_limit": 200,
        "segments": [
            {
                "index": 1,
                "first_page": 1,
                "last_page": 1,
                "page_count": 1,
                "uploaded_file": "part.pdf",
                "batch_id": "batch",
                "response_archive": "segments/one.zip",
                "response_sha256": sha256(nested).hexdigest(),
            }
        ],
    }
    raw.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(manifest).encode("utf-8"),
                "segments/one.zip": nested,
            }
        )
    )

    with pytest.raises(MineruContentError, match="as a ZIP"):
        normalize_mineru_content(raw)


def test_failure_preserves_previous_output_and_fresh_skip_force_rebuilds(
    tmp_path: Path,
) -> None:
    raw = _write_direct(
        tmp_path,
        items=[{"type": "text", "text": "valid", "page_idx": 0}],
    )
    output = normalize_mineru_content(raw)
    old_content = (output / "content.json").read_bytes()
    old_inode = output.stat().st_ino

    raw.write_bytes(_zip_bytes({"book_content_list.json": b"not json"}))
    with pytest.raises(MineruContentError):
        normalize_mineru_content(raw)
    assert (output / "content.json").read_bytes() == old_content

    raw.write_bytes(
        _zip_bytes(
            {
                "book_content_list.json": _content_list(
                    [{"type": "text", "text": "fresh", "page_idx": 0}]
                )
            }
        )
    )
    normalize_mineru_content(raw)
    skipped_inode = output.stat().st_ino
    assert skipped_inode != old_inode

    normalize_mineru_content(raw)
    assert output.stat().st_ino == skipped_inode

    normalize_mineru_content(raw, force=True)
    assert output.stat().st_ino != skipped_inode
