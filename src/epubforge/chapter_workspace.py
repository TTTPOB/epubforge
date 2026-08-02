"""Build deterministic, agent-editable workspaces for MinerU chapters.

This module consumes the committed ``02_content`` and ``03_chapters`` JSON
contracts.  It deliberately stops at HTML and page evidence; it does not
create Semantic IR or editor patches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import math
import os
import errno
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any, Literal

from filelock import FileLock
import pymupdf
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from epubforge.annotation import draw_annotation
from epubforge.chapter_segmentation import (
    ChapterBoundary,
    ChapterSegmentationArtifact,
    validate_boundaries,
)
from epubforge.strict_json import StrictJsonError, read_json_document
from epubforge.page_geometry import (
    PAGE_GEOMETRY_CONTRACT,
    PAGE_GEOMETRY_TOLERANCE,
    PageGeometryError,
    content_source_sha256,
    normalize_page_geometry,
)


WORKSPACE_SCHEMA = "epubforge.chapter-workspace"
WORKSPACE_SCHEMA_VERSION = 1
DEFAULT_RENDER_DPI = 150
DEFAULT_JPEG_QUALITY = 92
MIN_RENDER_DPI = 72
MAX_RENDER_DPI = 300
MAX_SOURCE_PDF_PAGES = 1000
MAX_SOURCE_PDF_BYTES = 2 * 1024 * 1024 * 1024
MAX_INPUT_JSON_BYTES = 64 * 1024 * 1024
MAX_RENDER_PIXELS_PER_PAGE = 25_000_000
MAX_TOTAL_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 256 * 1024 * 1024
_SHA256_CHARS = frozenset("0123456789abcdef")
_CONTENT_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "source_archive_sha256",
        "source_archive_size",
        "source_kind",
        "segment_count",
        "page_count",
        "page_geometry",
        "source_pdf_sha256",
        "items_sha256",
        "normalization",
        "assets",
        "items",
    }
)
_ASSET_KEYS = frozenset({"asset_path", "image_path", "img_path"})
_ANNOTATION_TYPES = {
    "image": "FIGURE",
    "figure": "FIGURE",
    "chart": "FIGURE",
    "seal": "FIGURE",
    "figure_title": "CAPTION",
    "figure_caption": "CAPTION",
    "image_caption": "CAPTION",
    "caption": "CAPTION",
    "text": "BODY",
    "paragraph": "BODY",
    "reference": "BODY",
    "abstract": "BODY",
    "content": "BODY",
    "title": "TITLE",
    "document_title": "TITLE",
    "doc_title": "TITLE",
    "paragraph_title": "TITLE",
    "section_header": "TITLE",
    "header": "HEADER",
    "page_header": "HEADER",
    "footer": "FOOTNOTE",
    "page_footer": "FOOTNOTE",
    "footnote": "FOOTNOTE",
    "figure_footnote": "CAPTION",
    "table_footnote": "FOOTNOTE",
    "aside_text": "BODY",
    "algorithm": "BODY",
    "number": "BODY",
    "table": "TABLE",
    "table_title": "CAPTION",
    "table_caption": "CAPTION",
    "formula": "FORMULA",
    "equation": "FORMULA",
    "formula_number": "FORMULA",
    "list": "LIST",
    "list_item": "LIST",
}
_HTML_VOID_TAGS = frozenset({"br", "col", "img", "hr"})
_TABLE_TAGS = frozenset(
    {
        "table",
        "caption",
        "colgroup",
        "col",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "img",
        "br",
        "p",
        "div",
        "span",
        "b",
        "strong",
        "i",
        "em",
        "sup",
        "sub",
    }
)
_TABLE_ATTRS = {
    "table": frozenset({"class", "role"}),
    "caption": frozenset({"class"}),
    "colgroup": frozenset({"class"}),
    "col": frozenset({"class", "span"}),
    "thead": frozenset({"class"}),
    "tbody": frozenset({"class"}),
    "tfoot": frozenset({"class"}),
    "tr": frozenset({"class"}),
    "th": frozenset({"class", "colspan", "rowspan", "scope", "abbr"}),
    "td": frozenset({"class", "colspan", "rowspan"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "br": frozenset(),
    "p": frozenset({"class"}),
    "div": frozenset({"class"}),
    "span": frozenset({"class"}),
    "b": frozenset(),
    "strong": frozenset(),
    "i": frozenset(),
    "em": frozenset(),
    "sup": frozenset(),
    "sub": frozenset(),
}
_TABLE_CHILDREN = {
    "table": frozenset({"caption", "colgroup", "thead", "tbody", "tfoot", "tr"}),
    "caption": frozenset({"br", "img", "span", "b", "strong", "i", "em", "sup", "sub"}),
    "colgroup": frozenset({"col"}),
    "thead": frozenset({"tr"}),
    "tbody": frozenset({"tr"}),
    "tfoot": frozenset({"tr"}),
    "tr": frozenset({"th", "td"}),
    "th": frozenset(
        {"br", "img", "p", "div", "span", "b", "strong", "i", "em", "sup", "sub"}
    ),
    "td": frozenset(
        {"br", "img", "p", "div", "span", "b", "strong", "i", "em", "sup", "sub"}
    ),
    "p": frozenset({"br", "img", "span", "b", "strong", "i", "em", "sup", "sub"}),
    "div": frozenset(
        {"br", "img", "p", "span", "b", "strong", "i", "em", "sup", "sub"}
    ),
    "span": frozenset({"br", "img", "span", "b", "strong", "i", "em", "sup", "sub"}),
    "b": frozenset(),
    "strong": frozenset(),
    "i": frozenset(),
    "em": frozenset(),
    "sup": frozenset(),
    "sub": frozenset(),
    "col": frozenset(),
    "br": frozenset(),
    "img": frozenset(),
}
_TABLE_TEXT_TAGS = frozenset(
    {"caption", "th", "td", "p", "div", "span", "b", "strong", "i", "em", "sup", "sub"}
)
log = logging.getLogger(__name__)


class ChapterWorkspaceError(ValueError):
    """Raised when an input contract or workspace build is unsafe."""


class _WorkspaceFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _WorkspaceChapterEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ordinal: int = Field(ge=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    start_content_idx: int = Field(ge=0)
    end_content_idx: int = Field(ge=0)
    start_page_idx: int = Field(ge=0)
    end_page_idx: int = Field(ge=0)
    chapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: str = Field(alias="schema", min_length=1)
    schema_version: Literal[1]
    source_fingerprints: _WorkspaceFingerprint
    freshness_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_dpi: int = Field(ge=MIN_RENDER_DPI, le=MAX_RENDER_DPI)
    jpeg_quality: int = Field(ge=1, le=100)
    chapters: list[_WorkspaceChapterEntry] = Field(min_length=1)
    files_sha256: dict[str, str] = Field(min_length=1)


@dataclass(frozen=True)
class _Content:
    path: Path
    items: list[dict[str, Any]]
    assets: dict[str, dict[str, Any]]
    asset_bytes: dict[str, bytes]
    page_count: int
    page_geometry: tuple[dict[str, int | float], ...]
    items_sha256: str
    source_content_sha256: str
    source_pdf_sha256: str
    file_sha256: str


@dataclass(frozen=True)
class _Pdf:
    data: bytes
    page_count: int
    rects: tuple[pymupdf.Rect, ...]
    sha256: str


@dataclass(frozen=True)
class _ChapterRange:
    ordinal: int
    boundary: ChapterBoundary
    start_content_idx: int
    end_content_idx: int
    start_page_idx: int
    end_page_idx: int
    page_indices: tuple[int, ...]
    items: tuple[dict[str, Any], ...]


def build_chapter_workspace(
    work_dir: str | Path,
    chapters_path: str | Path | None = None,
    source_pdf: str | Path | None = None,
    output_dir: str | Path | None = None,
    *,
    content_path: str | Path | None = None,
    force: bool = False,
    dpi: int = DEFAULT_RENDER_DPI,
    quality: int = DEFAULT_JPEG_QUALITY,
    max_render_pixels_per_page: int = MAX_RENDER_PIXELS_PER_PAGE,
    max_total_output_bytes: int = MAX_TOTAL_OUTPUT_BYTES,
    max_total_asset_bytes: int = MAX_TOTAL_ASSET_BYTES,
) -> Path:
    """Publish ``04_edit`` for the normalized book at *work_dir*.

    The positional path arguments also support direct callers that keep the
    three upstream artifacts outside the conventional work-directory layout.
    The function returns the published ``04_edit`` directory.
    """
    if not isinstance(force, bool):
        raise ChapterWorkspaceError("force must be a boolean")
    if (
        not isinstance(dpi, int)
        or isinstance(dpi, bool)
        or not MIN_RENDER_DPI <= dpi <= MAX_RENDER_DPI
    ):
        raise ChapterWorkspaceError(
            f"dpi must be an integer from {MIN_RENDER_DPI} through {MAX_RENDER_DPI}"
        )
    if (
        not isinstance(quality, int)
        or isinstance(quality, bool)
        or not 1 <= quality <= 100
    ):
        raise ChapterWorkspaceError("quality must be an integer from 1 through 100")
    for name, value in (
        ("max_render_pixels_per_page", max_render_pixels_per_page),
        ("max_total_output_bytes", max_total_output_bytes),
        ("max_total_asset_bytes", max_total_asset_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ChapterWorkspaceError(f"{name} must be a positive integer")

    requested_work = Path(work_dir)
    if requested_work.is_symlink():
        raise ChapterWorkspaceError(f"work directory is a symlink: {requested_work}")
    if requested_work.is_file():
        resolved_content = (
            requested_work if content_path is None else Path(content_path)
        )
        base_work = requested_work.parent.parent
    else:
        base_work = requested_work
        resolved_content = (
            Path(content_path)
            if content_path is not None
            else base_work / "02_content" / "content.json"
        )
    resolved_chapters = (
        Path(chapters_path)
        if chapters_path is not None
        else base_work / "03_chapters" / "chapters.json"
    )
    resolved_source = (
        Path(source_pdf)
        if source_pdf is not None
        else base_work / "source" / "source.pdf"
    )
    resolved_output = (
        Path(output_dir) if output_dir is not None else base_work / "04_edit"
    )
    if resolved_content.is_dir():
        resolved_content = resolved_content / "content.json"
    if resolved_chapters.is_dir():
        resolved_chapters = resolved_chapters / "chapters.json"

    for label, input_path in (
        ("normalized content", resolved_content),
        ("chapter plan", resolved_chapters),
        ("source PDF", resolved_source),
    ):
        _reject_symlink_ancestors(input_path, label)

    content = _load_content(
        resolved_content,
        max_total_asset_bytes=max_total_asset_bytes,
    )
    pdf = _load_pdf(resolved_source)
    if pdf.page_count != content.page_count:
        raise ChapterWorkspaceError(
            "source PDF page count does not match normalized content page_count: "
            f"{pdf.page_count} != {content.page_count}"
        )
    _validate_items_against_pdf(
        content.items,
        content.page_geometry,
        rects=pdf.rects,
    )
    if content.source_pdf_sha256 != pdf.sha256:
        raise ChapterWorkspaceError(
            "normalized content source_pdf_sha256 does not match source PDF"
        )

    ranges, chapters_sha256 = _load_ranges(
        resolved_chapters,
        content.items,
        content.source_content_sha256,
        page_count=content.page_count,
    )
    output = resolved_output
    _validate_output_parent(output)
    contract_sha256 = _workspace_contract_sha256()
    freshness = _freshness_fingerprint(
        content=content,
        chapters_sha256=chapters_sha256,
        source_pdf_sha256=pdf.sha256,
        dpi=dpi,
        quality=quality,
        contract_sha256=contract_sha256,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.parent / f".{output.name}.lock"
    with FileLock(str(lock_path)):
        if not force and _fresh_output_matches(
            output,
            freshness=freshness,
            ranges=ranges,
        ):
            return output

        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        try:
            _build_staging_output(
                staging,
                content=content,
                pdf=pdf,
                ranges=ranges,
                chapters_sha256=chapters_sha256,
                source_pdf_sha256=pdf.sha256,
                dpi=dpi,
                quality=quality,
                contract_sha256=contract_sha256,
                freshness=freshness,
                max_render_pixels_per_page=max_render_pixels_per_page,
                max_total_output_bytes=max_total_output_bytes,
                max_total_asset_bytes=max_total_asset_bytes,
            )
            _publish_directory(staging, output)
            staging = Path()
            return output
        finally:
            if staging != Path():
                _remove_path(staging)


def build_chapter_edit_workspace(*args: Any, **kwargs: Any) -> Path:
    """Compatibility name for callers that describe the edit output."""
    return build_chapter_workspace(*args, **kwargs)


def materialize_chapter_workspace(*args: Any, **kwargs: Any) -> Path:
    """Compatibility name for pipeline wiring and direct callers."""
    return build_chapter_workspace(*args, **kwargs)


def _load_content(path: Path, *, max_total_asset_bytes: int) -> _Content:
    raw, raw_bytes = _read_json_document(path, "normalized content")
    if not isinstance(raw, dict):
        raise ChapterWorkspaceError("normalized content must be a JSON object")
    if set(raw) != _CONTENT_KEYS:
        raise ChapterWorkspaceError(
            "normalized content has an unexpected top-level contract"
        )
    _require_string(raw, "schema")
    if raw["schema"] != "epubforge.mineru-content":
        raise ChapterWorkspaceError("normalized content has an invalid schema")
    _require_exact_int(raw, "schema_version", 2)
    _require_sha(raw, "source_archive_sha256")
    _require_nonnegative_int(raw, "source_archive_size")
    if raw.get("source_kind") not in {"direct", "segmented"}:
        raise ChapterWorkspaceError("normalized content has an invalid source_kind")
    _require_positive_int(raw, "segment_count")
    page_count = _require_positive_int(raw, "page_count")
    source_pdf_sha = _require_sha(raw, "source_pdf_sha256")
    _require_sha(raw, "items_sha256")
    if not isinstance(raw.get("normalization"), dict):
        raise ChapterWorkspaceError(
            "normalized content normalization must be an object"
        )
    assets = raw.get("assets")
    if not isinstance(assets, dict):
        raise ChapterWorkspaceError("normalized content assets must be an object")
    page_geometry = raw.get("page_geometry")
    if not isinstance(page_geometry, list) or not page_geometry:
        raise ChapterWorkspaceError(
            "normalized content page_geometry must be a non-empty array"
        )
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        raise ChapterWorkspaceError(
            "normalized content items must be a non-empty array"
        )

    items_sha256 = _items_sha256(items)
    if raw["items_sha256"] != items_sha256:
        raise ChapterWorkspaceError(
            "normalized content items_sha256 does not match items"
        )
    asset_bytes = _validate_assets(
        path.parent,
        assets,
        max_total_asset_bytes=max_total_asset_bytes,
    )
    _validate_items(items, assets, page_count)
    geometry = _validate_page_geometry(page_geometry, page_count)
    return _Content(
        path=path,
        items=items,
        assets=assets,
        asset_bytes=asset_bytes,
        page_count=page_count,
        page_geometry=geometry,
        items_sha256=items_sha256,
        source_content_sha256=_content_source_sha256(items, geometry),
        source_pdf_sha256=source_pdf_sha,
        file_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def _load_pdf(path: Path) -> _Pdf:
    data = _read_regular_bytes(
        path,
        "source PDF",
        max_bytes=MAX_SOURCE_PDF_BYTES,
    )
    digest = hashlib.sha256(data).hexdigest()
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            page_count = document.page_count
            rects = tuple(page.rect for page in document)
    except ChapterWorkspaceError:
        raise
    except Exception as exc:
        raise ChapterWorkspaceError(f"cannot read source PDF: {path}") from exc
    if page_count <= 0:
        raise ChapterWorkspaceError("source PDF must contain at least one page")
    if page_count > MAX_SOURCE_PDF_PAGES:
        raise ChapterWorkspaceError(
            f"source PDF exceeds the {MAX_SOURCE_PDF_PAGES}-page limit"
        )
    return _Pdf(data=data, page_count=page_count, rects=rects, sha256=digest)


def _load_ranges(
    path: Path,
    items: Sequence[dict[str, Any]],
    items_sha256: str,
    *,
    page_count: int,
) -> tuple[tuple[_ChapterRange, ...], str]:
    raw, raw_bytes = _read_json_document(path, "chapter plan")
    if not isinstance(raw, dict):
        raise ChapterWorkspaceError("chapter plan must be a JSON object")
    try:
        artifact = ChapterSegmentationArtifact.model_validate(raw)
    except ValidationError as exc:
        raise ChapterWorkspaceError("chapter plan failed its strict contract") from exc
    if artifact.source_content_sha256 != items_sha256:
        raise ChapterWorkspaceError(
            "chapter plan source_content_sha256 does not match content"
        )
    try:
        validate_boundaries(artifact.boundaries, items)
    except Exception as exc:
        raise ChapterWorkspaceError(
            f"chapter plan boundaries are invalid: {exc}"
        ) from exc
    ranges: list[_ChapterRange] = []
    for position, boundary in enumerate(artifact.boundaries):
        start = boundary.start_content_idx
        included_start = 0 if position == 0 else start
        end = (
            artifact.boundaries[position + 1].start_content_idx - 1
            if position + 1 < len(artifact.boundaries)
            else len(items) - 1
        )
        if included_start > end or end >= len(items):
            raise ChapterWorkspaceError(
                f"chapter {position + 1} has an empty or invalid range"
            )
        included = tuple(items[included_start : end + 1])
        pages = sorted({int(item["page_idx"]) for item in included})
        if not pages:
            raise ChapterWorkspaceError(f"chapter {position + 1} has no pages")
        page_start = pages[0]
        page_end = pages[-1]
        if page_start > boundary.start_page_idx or page_end >= page_count:
            raise ChapterWorkspaceError(
                f"chapter {position + 1} page range does not match its content range"
            )
        if position > 0 and page_start != boundary.start_page_idx:
            raise ChapterWorkspaceError(
                f"chapter {position + 1} page range does not match its content range"
            )
        page_indices = tuple(range(page_start, page_end + 1))
        ranges.append(
            _ChapterRange(
                ordinal=position + 1,
                boundary=boundary,
                start_content_idx=start,
                end_content_idx=end,
                start_page_idx=page_start,
                end_page_idx=page_end,
                page_indices=page_indices,
                items=included,
            )
        )
    return tuple(ranges), hashlib.sha256(raw_bytes).hexdigest()


def _validate_assets(
    content_dir: Path,
    assets: Mapping[str, Any],
    *,
    max_total_asset_bytes: int,
) -> dict[str, bytes]:
    declared_total = 0
    for relative, metadata in assets.items():
        if not isinstance(metadata, dict) or set(metadata) != {"sha256", "size"}:
            raise ChapterWorkspaceError(f"asset metadata is invalid: {relative}")
        size = metadata.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ChapterWorkspaceError(f"asset metadata size is invalid: {relative}")
        declared_total += size
        if declared_total > max_total_asset_bytes:
            raise ChapterWorkspaceError(
                "declared assets exceed the total output asset limit"
            )

    snapshots: dict[str, bytes] = {}
    for relative, metadata in assets.items():
        _validate_asset_relative_path(relative, "asset manifest path")
        _validate_sha(metadata.get("sha256"), f"asset {relative} sha256")
        size = metadata.get("size")
        asset_path = content_dir / PurePosixPath(relative)
        data = _read_regular_bytes(
            asset_path,
            f"content asset {relative}",
            max_bytes=size,
        )
        if len(data) != size or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ChapterWorkspaceError(f"asset fingerprint mismatch: {relative}")
        snapshots[relative] = data
    return snapshots


def _validate_items(
    items: Sequence[Mapping[str, Any]],
    assets: Mapping[str, Any],
    page_count: int,
) -> None:
    previous_page: int | None = None
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise ChapterWorkspaceError(f"content item {position} must be an object")
        if item.get("content_idx") != position:
            raise ChapterWorkspaceError(
                "normalized content items must have contiguous ordered content_idx values"
            )
        page_idx = item.get("page_idx")
        if (
            not isinstance(page_idx, int)
            or isinstance(page_idx, bool)
            or not 0 <= page_idx < page_count
        ):
            raise ChapterWorkspaceError(
                f"content item {position} has an invalid page_idx"
            )
        if previous_page is not None and page_idx < previous_page:
            raise ChapterWorkspaceError(
                "normalized content page order cannot go backward"
            )
        item_type = item.get("type")
        if not isinstance(item_type, str) or not item_type.strip():
            raise ChapterWorkspaceError(f"content item {position} has an invalid type")
        _validate_asset_references(item, assets)
        bbox = item.get("bbox")
        if bbox is not None:
            _validate_bbox(bbox, position)
        previous_page = page_idx


def _validate_items_against_pdf(
    items: Sequence[Mapping[str, Any]],
    page_geometry: Sequence[Mapping[str, Any]],
    *,
    rects: Sequence[pymupdf.Rect],
) -> None:
    if len(page_geometry) != len(rects):
        raise ChapterWorkspaceError(
            "normalized content page_geometry does not match source PDF pages"
        )
    for page_idx, geometry in enumerate(page_geometry):
        width = float(geometry["width"])
        height = float(geometry["height"])
        rect = rects[page_idx]
        pdf_width = float(rect.width)
        pdf_height = float(rect.height)
        width_delta = abs(width - pdf_width) / max(pdf_width, 1.0)
        height_delta = abs(height - pdf_height) / max(pdf_height, 1.0)
        geometry_aspect = width / height
        pdf_aspect = pdf_width / pdf_height
        aspect_delta = abs(geometry_aspect - pdf_aspect) / pdf_aspect
        if (
            width_delta > PAGE_GEOMETRY_TOLERANCE
            or height_delta > PAGE_GEOMETRY_TOLERANCE
            or aspect_delta > PAGE_GEOMETRY_TOLERANCE
        ):
            raise ChapterWorkspaceError(
                "normalized content page_geometry does not match displayed PDF "
                f"page.rect dimensions on page {page_idx}"
            )
    for item in items:
        if item.get("bbox") is None:
            continue
        page_idx = int(item["page_idx"])
        geometry = page_geometry[page_idx]
        width = float(geometry["width"])
        height = float(geometry["height"])
        x0, y0, x1, y1 = _validate_bbox(item["bbox"], int(item["content_idx"]))
        epsilon = 1e-5
        if (
            x0 < -epsilon
            or y0 < -epsilon
            or x1 > width + epsilon
            or y1 > height + epsilon
        ):
            raise ChapterWorkspaceError(
                f"content item {item['content_idx']} bbox is outside layout page "
                f"geometry {page_idx}"
            )


def _validate_bbox(value: Any, position: int) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ChapterWorkspaceError(
            f"content item {position} bbox must contain four numbers"
        )
    values: list[float] = []
    for coordinate in value:
        if not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool):
            raise ChapterWorkspaceError(
                f"content item {position} bbox contains a non-number"
            )
        converted = float(coordinate)
        if not math.isfinite(converted):
            raise ChapterWorkspaceError(
                f"content item {position} bbox contains a non-finite number"
            )
        values.append(converted)
    x0, y0, x1, y1 = values
    if x0 >= x1 or y0 >= y1:
        raise ChapterWorkspaceError(
            f"content item {position} bbox has impossible geometry"
        )
    return x0, y0, x1, y1


def _validate_page_geometry(
    value: Any, page_count: int
) -> tuple[dict[str, int | float], ...]:
    try:
        return normalize_page_geometry(value, page_count=page_count)
    except PageGeometryError as exc:
        raise ChapterWorkspaceError(f"normalized content {exc}") from exc


def _validate_asset_references(
    value: Any, assets: Mapping[str, Any], *, key: str | None = None
) -> None:
    if isinstance(value, list):
        for child in value:
            _validate_asset_references(child, assets, key=key)
        return
    if isinstance(value, dict):
        for name, child in value.items():
            if not isinstance(name, str):
                raise ChapterWorkspaceError(
                    "content item contains a non-string JSON key"
                )
            _validate_asset_references(
                child, assets, key=name if name in _ASSET_KEYS else None
            )
        return
    if key not in _ASSET_KEYS:
        return
    if value is None or value == "":
        return
    if not isinstance(value, str):
        raise ChapterWorkspaceError(
            f"asset path field {key} must contain a string or null"
        )
    _validate_asset_relative_path(value, f"asset reference {value}")
    if value not in assets:
        raise ChapterWorkspaceError(
            f"asset reference is missing from manifest: {value}"
        )


def _validate_asset_relative_path(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
    ):
        raise ChapterWorkspaceError(f"unsafe {label}: {value!r}")
    path = PurePosixPath(value)
    if (
        len(path.parts) != 2
        or path.parts[0] != "assets"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ChapterWorkspaceError(f"unsafe {label}: {value!r}")
    if path.as_posix() != value:
        raise ChapterWorkspaceError(f"unsafe {label}: {value!r}")


class _SafeTableParser(HTMLParser):
    """Rebuild a small table fragment from a strict local tag/attribute set."""

    def __init__(self, assets: Mapping[str, Any]) -> None:
        super().__init__(convert_charrefs=False)
        self.assets = assets
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.asset_refs: set[str] = set()
        self.root_seen = False
        self.root_closed = False
        self.row_cells: list[int] = []
        self.row_count = 0

    def _start(
        self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool
    ) -> None:
        tag = tag.lower()
        if tag not in _TABLE_TAGS:
            raise ChapterWorkspaceError(f"unsafe table tag: {tag}")
        if self.root_closed or (not self.stack and self.root_seen):
            raise ChapterWorkspaceError("table HTML has multiple root elements")
        if not self.stack:
            if tag != "table":
                raise ChapterWorkspaceError("table HTML must have a table root")
            self.root_seen = True
        else:
            parent = self.stack[-1]
            if tag not in _TABLE_CHILDREN[parent]:
                raise ChapterWorkspaceError(
                    f"table tag {tag} is not allowed inside {parent}"
                )
        if tag in {"th", "td"}:
            if not self.row_cells:
                raise ChapterWorkspaceError("table cells must be inside a row")
            self.row_cells[-1] += 1
        if tag == "tr":
            self.row_cells.append(0)
            self.row_count += 1
        allowed = _TABLE_ATTRS[tag]
        rendered_attrs: list[str] = []
        seen: set[str] = set()
        for name, value in attrs:
            name = name.lower()
            if (
                name in seen
                or name not in allowed
                or value is None
                or name.startswith("on")
                or name in {"style", "href", "action", "formaction", "xlink:href"}
            ):
                raise ChapterWorkspaceError(f"unsafe table attribute: {name}")
            seen.add(name)
            if name == "src":
                _validate_asset_relative_path(value, "table asset reference")
                if value not in self.assets:
                    raise ChapterWorkspaceError(
                        f"table asset reference is missing: {value}"
                    )
                self.asset_refs.add(value)
                value = f"assets/{PurePosixPath(value).name}"
            elif name in {"colspan", "rowspan", "span", "width", "height"}:
                if not value.isdigit() or int(value) <= 0:
                    raise ChapterWorkspaceError(
                        f"invalid table numeric attribute: {name}"
                    )
            rendered_attrs.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(rendered_attrs)}>")
        if tag not in _HTML_VOID_TAGS and not self_closing:
            self.stack.append(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, True)
        if tag.lower() not in _HTML_VOID_TAGS:
            self.parts.append(f"</{tag.lower()}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in _TABLE_TAGS or not self.stack or self.stack[-1] != tag:
            raise ChapterWorkspaceError(f"invalid table closing tag: {tag}")
        if tag == "tr":
            if not self.row_cells or self.row_cells[-1] == 0:
                raise ChapterWorkspaceError("table rows must contain a cell")
            self.row_cells.pop()
        self.stack.pop()
        self.parts.append(f"</{tag}>")
        if not self.stack:
            self.root_closed = True

    def handle_data(self, data: str) -> None:
        if not self.root_seen or self.root_closed:
            if data.strip():
                raise ChapterWorkspaceError("table HTML has text outside its root")
            return
        if self.stack[-1] not in _TABLE_TEXT_TAGS and data.strip():
            raise ChapterWorkspaceError(
                f"table text is not allowed inside {self.stack[-1]}"
            )
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if (
            not self.root_seen
            or self.root_closed
            or self.stack[-1] not in _TABLE_TEXT_TAGS
        ):
            raise ChapterWorkspaceError("table HTML has an entity outside its root")
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if (
            not self.root_seen
            or self.root_closed
            or self.stack[-1] not in _TABLE_TEXT_TAGS
        ):
            raise ChapterWorkspaceError(
                "table HTML has a character reference outside its root"
            )
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        raise ChapterWorkspaceError("comments are not allowed in table HTML")

    def handle_decl(self, decl: str) -> None:
        raise ChapterWorkspaceError("declarations are not allowed in table HTML")

    def finish(self) -> tuple[str, set[str]]:
        if (
            not self.root_seen
            or not self.root_closed
            or self.stack
            or self.row_cells
            or self.row_count == 0
        ):
            raise ChapterWorkspaceError("table HTML must contain one balanced table")
        return "".join(self.parts), self.asset_refs


def _sanitize_table_html(value: Any, assets: Mapping[str, Any]) -> tuple[str, set[str]]:
    if not isinstance(value, str) or not value.strip():
        raise ChapterWorkspaceError("table html must be a non-empty string")
    parser = _SafeTableParser(assets)
    try:
        parser.feed(value)
        parser.close()
    except ChapterWorkspaceError:
        raise
    except Exception as exc:
        raise ChapterWorkspaceError("table html failed strict parsing") from exc
    return parser.finish()


def _build_staging_output(
    staging: Path,
    *,
    content: _Content,
    pdf: _Pdf,
    ranges: Sequence[_ChapterRange],
    chapters_sha256: str,
    source_pdf_sha256: str,
    dpi: int,
    quality: int,
    contract_sha256: str,
    freshness: str,
    max_render_pixels_per_page: int,
    max_total_output_bytes: int,
    max_total_asset_bytes: int,
) -> None:
    chapters_root = staging / "chapters"
    chapters_root.mkdir()
    prepared: list[tuple[dict[int, str], set[str]]] = []
    total_asset_bytes = 0
    for chapter in ranges:
        sanitized_tables, asset_refs = _prepare_chapter_content(
            chapter.items,
            content.assets,
        )
        total_asset_bytes += sum(
            int(content.assets[relative]["size"]) for relative in asset_refs
        )
        prepared.append((sanitized_tables, asset_refs))
    if total_asset_bytes > max_total_asset_bytes:
        raise ChapterWorkspaceError(
            "chapter assets exceed the total output asset limit"
        )

    rendered_pages = staging / ".rendered-pages"
    rendered_pages.mkdir()
    try:
        with pymupdf.open(stream=pdf.data, filetype="pdf") as document:
            if document.page_count != pdf.page_count:
                raise ChapterWorkspaceError(
                    "source PDF changed while building workspace"
                )
            page_files: dict[int, Path] = {}
            page_indices = sorted(
                {page_idx for chapter in ranges for page_idx in chapter.page_indices}
            )
            for page_idx in page_indices:
                png, pixel_width, pixel_height = _render_base_page(
                    document,
                    page_idx,
                    dpi=dpi,
                    max_render_pixels=max_render_pixels_per_page,
                )
                page_boxes = _chapter_page_boxes(
                    content.items,
                    page_idx,
                    content.page_geometry[page_idx],
                    pixel_width=pixel_width,
                    pixel_height=pixel_height,
                )
                page_path = rendered_pages / f"page-{page_idx:04d}.jpg"
                _write_annotated_jpeg(
                    page_path,
                    png,
                    pixel_width=pixel_width,
                    pixel_height=pixel_height,
                    boxes=page_boxes,
                    quality=quality,
                )
                page_files[page_idx] = page_path

            for chapter, (sanitized_tables, asset_refs) in zip(
                ranges, prepared, strict=True
            ):
                chapter_dir = chapters_root / f"{chapter.ordinal:04d}"
                chapter_dir.mkdir()
                chapter_dir.joinpath("pages").mkdir()
                chapter_dir.joinpath("assets").mkdir()
                asset_map = _copy_chapter_assets(
                    content.asset_bytes,
                    chapter_dir,
                    asset_refs,
                )
                chapter_html = _render_chapter_html(
                    chapter,
                    sanitized_tables=sanitized_tables,
                    asset_map=asset_map,
                )
                chapter_html_path = chapter_dir / "chapter.html"
                chapter_html_path.write_text(chapter_html, encoding="utf-8")
                page_names: list[str] = []
                for page_idx in chapter.page_indices:
                    page_name = f"pages/page-{page_idx:04d}.jpg"
                    _link_or_copy_page(
                        page_files[page_idx],
                        chapter_dir / PurePosixPath(page_name),
                    )
                    page_names.append(page_name)
                chapter_manifest = {
                    "schema": WORKSPACE_SCHEMA,
                    "schema_version": WORKSPACE_SCHEMA_VERSION,
                    "source_content_sha256": content.source_content_sha256,
                    "source_content_file_sha256": content.file_sha256,
                    "source_chapters_sha256": chapters_sha256,
                    "source_pdf_sha256": source_pdf_sha256,
                    "source_page_count": pdf.page_count,
                    "ordinal": chapter.ordinal,
                    "title": chapter.boundary.title,
                    "kind": chapter.boundary.kind,
                    "start_content_idx": chapter.start_content_idx,
                    "end_content_idx": chapter.end_content_idx,
                    "start_page_idx": chapter.start_page_idx,
                    "end_page_idx": chapter.end_page_idx,
                    "chapter_html_sha256": _sha256_file(chapter_html_path),
                    "pages": page_names,
                    "assets": [
                        f"assets/{PurePosixPath(relative).name}"
                        for relative in sorted(asset_refs)
                    ],
                }
                chapter_dir.joinpath("chapter.json").write_text(
                    json.dumps(
                        chapter_manifest, ensure_ascii=False, indent=2, sort_keys=True
                    )
                    + "\n",
                    encoding="utf-8",
                )
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, ChapterWorkspaceError):
            raise
        raise ChapterWorkspaceError(f"cannot build chapter workspace: {exc}") from exc
    finally:
        _remove_path(rendered_pages)

    files_sha256 = _tree_file_hashes(staging)
    total_output_bytes = sum(
        _safe_file_size(staging, relative) for relative in files_sha256
    )
    if total_output_bytes > max_total_output_bytes:
        raise ChapterWorkspaceError("chapter workspace exceeds the total output limit")
    chapter_entries: list[dict[str, Any]] = []
    for chapter in ranges:
        chapter_manifest_path = (
            staging / "chapters" / f"{chapter.ordinal:04d}" / "chapter.json"
        )
        chapter_manifest = json.loads(chapter_manifest_path.read_text(encoding="utf-8"))
        chapter_entries.append(
            {
                "ordinal": chapter.ordinal,
                "path": f"chapters/{chapter.ordinal:04d}",
                "title": chapter.boundary.title,
                "kind": chapter.boundary.kind,
                "start_content_idx": chapter.start_content_idx,
                "end_content_idx": chapter.end_content_idx,
                "start_page_idx": chapter.start_page_idx,
                "end_page_idx": chapter.end_page_idx,
                "chapter_sha256": files_sha256[
                    f"chapters/{chapter.ordinal:04d}/chapter.json"
                ],
            }
        )
    manifest = {
        "schema": WORKSPACE_SCHEMA,
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "source_fingerprints": {
            "content_sha256": content.source_content_sha256,
            "content_file_sha256": content.file_sha256,
            "chapters_sha256": chapters_sha256,
            "source_pdf_sha256": source_pdf_sha256,
        },
        "freshness_fingerprint": freshness,
        "contract_sha256": contract_sha256,
        "render_dpi": dpi,
        "jpeg_quality": quality,
        "chapters": chapter_entries,
        "files_sha256": files_sha256,
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _prepare_chapter_content(
    items: Sequence[Mapping[str, Any]],
    assets: Mapping[str, Any],
) -> tuple[dict[int, str], set[str]]:
    sanitized_tables: dict[int, str] = {}
    asset_refs: set[str] = set()
    for item in items:
        asset_refs.update(_item_asset_references(item))
        if str(item["type"]).lower() == "table" and item.get("html"):
            sanitized, table_refs = _sanitize_table_html(item["html"], assets)
            sanitized_tables[int(item["content_idx"])] = sanitized
            asset_refs.update(table_refs)
    return sanitized_tables, asset_refs


def _copy_chapter_assets(
    asset_bytes: Mapping[str, bytes],
    chapter_dir: Path,
    refs: Sequence[str] | set[str],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    destination_names: dict[str, str] = {}
    for relative in sorted(refs):
        _validate_asset_relative_path(relative, "chapter asset reference")
        name = PurePosixPath(relative).name
        previous = destination_names.get(name)
        if previous is not None and previous != relative:
            raise ChapterWorkspaceError(f"chapter asset basename collision: {name}")
        target = chapter_dir / "assets" / name
        try:
            data = asset_bytes[relative]
        except KeyError as exc:
            raise ChapterWorkspaceError(
                f"chapter asset snapshot is missing: {relative}"
            ) from exc
        target.write_bytes(data)
        mapping[relative] = f"assets/{name}"
        destination_names[name] = relative
    return mapping


def _link_or_copy_page(source: Path, target: Path) -> None:
    """Reuse one rendered page snapshot in each chapter that touches it."""
    try:
        os.link(source, target)
    except OSError:
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise ChapterWorkspaceError(
                f"cannot materialize rendered page: {target}"
            ) from exc


def _render_chapter_html(
    chapter: _ChapterRange,
    *,
    sanitized_tables: Mapping[int, str],
    asset_map: Mapping[str, str],
) -> str:
    title = html.escape(chapter.boundary.title, quote=True)
    body: list[str] = []
    for item in chapter.items:
        body.append(
            _render_item(
                item,
                sanitized_table=sanitized_tables.get(int(item["content_idx"])),
                asset_map=asset_map,
            )
        )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f"  <title>{title}</title>\n"
        f'  <meta name="chapter-title" content="{title}">\n'
        f'  <meta name="chapter-kind" content="{html.escape(chapter.boundary.kind, quote=True)}">\n'
        "</head>\n"
        "<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )


def _render_item(
    item: Mapping[str, Any],
    *,
    sanitized_table: str | None,
    asset_map: Mapping[str, str],
) -> str:
    item_type = str(item["type"])
    lowered = item_type.lower()
    attrs = _item_attributes(item)
    text = _text_value(item.get("text", ""))
    escaped_text = html.escape(text, quote=False)
    refs = _item_asset_references(item)
    image_refs = [asset_map[ref] for ref in sorted(refs) if ref in asset_map]
    if lowered in {
        "title",
        "heading",
        "section_header",
        "document_title",
        "doc_title",
    } or (
        isinstance(item.get("text_level"), int)
        and not isinstance(item.get("text_level"), bool)
        and int(item["text_level"]) > 0
        and lowered in {"text", "paragraph", "text_block", "content"}
    ):
        level = 1 if item.get("text_level") in {None, 1} else 2
        return f"<h{level}{attrs}>{escaped_text}</h{level}>"
    if lowered in {"image", "figure", "chart", "seal"}:
        parts = [f"<figure{attrs}>"]
        for position, image_ref in enumerate(image_refs):
            alt = text if position == 0 else ""
            parts.append(
                f'<img src="{html.escape(image_ref, quote=True)}" alt="{html.escape(alt, quote=True)}">'
            )
        if text:
            parts.append(f"<p>{escaped_text}</p>")
        for caption in _captions(item):
            parts.append(
                f"<figcaption>{html.escape(caption, quote=False)}</figcaption>"
            )
        parts.append("</figure>")
        return "".join(parts)
    if lowered == "table":
        if sanitized_table is not None:
            opening, separator, rest = sanitized_table.partition(">")
            if not separator:
                raise ChapterWorkspaceError("sanitized table has no opening tag")
            title = "".join(
                f"<caption>{html.escape(caption, quote=False)}</caption>"
                for caption in _captions(item)
            )
            if text:
                title = f"<caption>{escaped_text}</caption>" + title
            return f"{opening}{attrs}>{title}{rest}"
        return f"<table{attrs}><tbody><tr><td>{escaped_text}</td></tr></tbody></table>"
    if lowered in {"footnote", "table_footnote", "figure_footnote", "aside_text"}:
        callout = _text_value(item.get("callout", ""))
        callout_markup = (
            "" if not callout else f"<sup>{html.escape(callout, quote=False)}</sup>"
        )
        return (
            f'<aside role="doc-footnote"{attrs}>{callout_markup}{escaped_text}</aside>'
        )
    if lowered in {"header", "page_header"}:
        return f"<header{attrs}>{escaped_text}</header>"
    if lowered in {"footer", "page_footer", "page_number", "number"}:
        return f"<footer{attrs}>{escaped_text}</footer>"
    if lowered in {"list", "list_item"}:
        tag = "li" if lowered == "list_item" else "ul"
        return f"<{tag}{attrs}>{escaped_text}</{tag}>"
    if lowered in {"caption", "figure_caption", "image_caption", "table_caption"}:
        return f"<figcaption{attrs}>{escaped_text}</figcaption>"
    if lowered in {"formula", "equation", "formula_number"}:
        return f'<p class="equation"{attrs}>{escaped_text}</p>'
    extra_image = "".join(
        f'<img src="{html.escape(image_ref, quote=True)}" alt="">'
        for image_ref in image_refs
    )
    return f"<p{attrs}>{escaped_text}{extra_image}</p>"


def _item_attributes(item: Mapping[str, Any]) -> str:
    content_idx = item["content_idx"]
    page_idx = item["page_idx"]
    item_type = html.escape(str(item["type"]), quote=True)
    attrs = (
        f' id="content-{int(content_idx):08d}"'
        f' data-content-idx="{int(content_idx)}"'
        f' data-page-idx="{int(page_idx)}"'
        f' data-type="{item_type}"'
    )
    if item.get("bbox") is not None:
        bbox = _validate_bbox(item["bbox"], int(content_idx))
        formatted = ",".join(_format_number(value) for value in bbox)
        attrs += f' data-bbox="{formatted}"'
    return attrs


def _format_number(value: float) -> str:
    formatted = format(float(value), ".12g")
    return "0" if formatted == "-0" else formatted


def _captions(item: Mapping[str, Any]) -> list[str]:
    captions: list[str] = []
    for key in (
        "image_caption",
        "figure_caption",
        "caption",
        "table_title",
        "table_caption",
    ):
        value = item.get(key)
        if isinstance(value, str) and value:
            captions.append(value)
        elif isinstance(value, list):
            captions.extend(
                str(part) for part in value if isinstance(part, str) and part
            )
    return captions


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_text_value(part) for part in value)
    if isinstance(value, dict):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return str(value)


def _item_asset_references(item: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, dict):
            for name, child in value.items():
                visit(child, name if name in _ASSET_KEYS else None)
        elif key in _ASSET_KEYS and isinstance(value, str) and value:
            refs.add(value)

    visit(item)
    return refs


def _chapter_page_boxes(
    items: Sequence[Mapping[str, Any]],
    page_idx: int,
    page_geometry: Mapping[str, Any],
    *,
    pixel_width: int,
    pixel_height: int,
) -> list[dict[str, Any]]:
    geometry_width = page_geometry.get("width")
    geometry_height = page_geometry.get("height")
    if (
        not isinstance(geometry_width, (int, float))
        or isinstance(geometry_width, bool)
        or not isinstance(geometry_height, (int, float))
        or isinstance(geometry_height, bool)
        or not math.isfinite(float(geometry_width))
        or not math.isfinite(float(geometry_height))
        or float(geometry_width) <= 0
        or float(geometry_height) <= 0
    ):
        raise ChapterWorkspaceError(f"layout page geometry {page_idx} is invalid")
    width = float(geometry_width)
    height = float(geometry_height)
    boxes: list[dict[str, Any]] = []
    for item in items:
        if item["page_idx"] != page_idx or item.get("bbox") is None:
            continue
        x0, y0, x1, y1 = _validate_bbox(item["bbox"], int(item["content_idx"]))
        epsilon = 1e-5
        if (
            x0 < -epsilon
            or y0 < -epsilon
            or x1 > width + epsilon
            or y1 > height + epsilon
        ):
            raise ChapterWorkspaceError(
                f"content item {item['content_idx']} bbox is outside layout page {page_idx}"
            )
        boxes.append(
            {
                "id": f"content-{int(item['content_idx']):08d}",
                "type": _ANNOTATION_TYPES.get(
                    str(item["type"]).lower(), str(item["type"]).upper()
                ),
                "reading_order": int(item["content_idx"]),
                "x0": x0 * pixel_width / width,
                "y0": y0 * pixel_height / height,
                "x1": x1 * pixel_width / width,
                "y1": y1 * pixel_height / height,
            }
        )
    return boxes


def _render_base_page(
    document: pymupdf.Document,
    page_idx: int,
    *,
    dpi: int,
    max_render_pixels: int,
) -> tuple[bytes, int, int]:
    try:
        page = document[page_idx]
        scale = dpi / 72.0
        expected_width = max(1, math.ceil(float(page.rect.width) * scale))
        expected_height = max(1, math.ceil(float(page.rect.height) * scale))
        if expected_width * expected_height > max_render_pixels:
            raise ChapterWorkspaceError(
                f"rendered source PDF page {page_idx} exceeds the pixel limit"
            )
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        if pixmap.width <= 0 or pixmap.height <= 0:
            raise ChapterWorkspaceError(f"rendered source PDF page {page_idx} is empty")
        if pixmap.width * pixmap.height > max_render_pixels:
            raise ChapterWorkspaceError(
                f"rendered source PDF page {page_idx} exceeds the pixel limit"
            )
        return pixmap.tobytes("png"), pixmap.width, pixmap.height
    except ChapterWorkspaceError:
        raise
    except Exception as exc:
        raise ChapterWorkspaceError(
            f"cannot render source PDF page {page_idx}"
        ) from exc


def _write_annotated_jpeg(
    output: Path,
    png: bytes,
    *,
    pixel_width: int,
    pixel_height: int,
    boxes: Sequence[Mapping[str, Any]],
    quality: int,
) -> None:
    try:
        with pymupdf.open() as document:
            page = document.new_page(width=pixel_width, height=pixel_height)
            page.insert_image(page.rect, stream=png)
            for box in boxes:
                draw_annotation(
                    page,
                    box,
                    page_width=float(pixel_width),
                    page_height=float(pixel_height),
                )
            rendered = page.get_pixmap(alpha=False)
            output.write_bytes(rendered.tobytes("jpeg", jpg_quality=quality))
    except ChapterWorkspaceError:
        raise
    except Exception as exc:
        raise ChapterWorkspaceError(
            f"cannot write annotated page: {output.name}"
        ) from exc


def _fresh_output_matches(
    output: Path,
    *,
    freshness: str,
    ranges: Sequence[_ChapterRange],
) -> bool:
    if output.is_symlink() or not output.is_dir():
        return False
    manifest_path = output / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    try:
        manifest = _read_json(manifest_path, "chapter workspace manifest")
        if not isinstance(manifest, dict):
            return False
        if (
            manifest.get("schema") != WORKSPACE_SCHEMA
            or manifest.get("schema_version") != WORKSPACE_SCHEMA_VERSION
            or manifest.get("freshness_fingerprint") != freshness
        ):
            return False
        expected_paths = [f"chapters/{chapter.ordinal:04d}" for chapter in ranges]
        expected_keys = {
            "schema",
            "schema_version",
            "source_fingerprints",
            "freshness_fingerprint",
            "contract_sha256",
            "render_dpi",
            "jpeg_quality",
            "chapters",
            "files_sha256",
        }
        if set(manifest) != expected_keys:
            return False
        try:
            _WorkspaceManifest.model_validate(manifest)
        except ValidationError:
            return False
        fingerprints = manifest.get("source_fingerprints")
        if not isinstance(fingerprints, dict) or set(fingerprints) != {
            "content_sha256",
            "content_file_sha256",
            "chapters_sha256",
            "source_pdf_sha256",
        }:
            return False
        if any(
            not isinstance(value, str) or _validate_sha_value(value) is False
            for value in fingerprints.values()
        ):
            return False
        if not isinstance(
            manifest.get("contract_sha256"), str
        ) or not _validate_sha_value(manifest["contract_sha256"]):
            return False
        if (
            not isinstance(manifest.get("render_dpi"), int)
            or isinstance(manifest["render_dpi"], bool)
            or not MIN_RENDER_DPI <= manifest["render_dpi"] <= MAX_RENDER_DPI
            or not isinstance(manifest.get("jpeg_quality"), int)
            or isinstance(manifest["jpeg_quality"], bool)
            or not 1 <= manifest["jpeg_quality"] <= 100
        ):
            return False
        chapters = manifest.get("chapters")
        if (
            not isinstance(chapters, list)
            or len(chapters) != len(expected_paths)
            or any(not isinstance(entry, dict) for entry in chapters)
            or [entry.get("path") for entry in chapters] != expected_paths
        ):
            return False
        files = manifest.get("files_sha256")
        if not isinstance(files, dict) or not files:
            return False
        actual = _tree_file_hashes(output)
        actual.pop("manifest.json", None)
        if set(files) != set(actual):
            return False
        for relative, digest in files.items():
            if not _validate_workspace_relative(relative):
                return False
            if not isinstance(digest, str) or not _validate_sha_value(digest):
                return False
            if _safe_tree_hash(output, relative) != digest:
                return False
        for position, entry in enumerate(chapters):
            if not isinstance(entry, dict) or set(entry) != {
                "chapter_sha256",
                "end_content_idx",
                "end_page_idx",
                "kind",
                "ordinal",
                "path",
                "start_content_idx",
                "start_page_idx",
                "title",
            }:
                return False
            chapter_json = f"{entry.get('path')}/chapter.json"
            if files.get(chapter_json) != entry.get("chapter_sha256"):
                return False
            expected = ranges[position]
            if (
                entry.get("ordinal") != expected.ordinal
                or entry.get("title") != expected.boundary.title
                or entry.get("kind") != expected.boundary.kind
                or entry.get("start_content_idx") != expected.start_content_idx
                or entry.get("end_content_idx") != expected.end_content_idx
                or entry.get("start_page_idx") != expected.start_page_idx
                or entry.get("end_page_idx") != expected.end_page_idx
            ):
                return False
            if not _validate_published_chapter(
                output,
                entry,
                files,
            ):
                return False
        return True
    except (ChapterWorkspaceError, OSError, UnicodeError, ValueError, TypeError):
        return False


def _safe_tree_hash(root: Path, relative: Any) -> str | None:
    if not _validate_workspace_relative(relative):
        return None
    try:
        fd = _open_relative_file_fd(root, relative)
    except (ChapterWorkspaceError, OSError):
        return None
    try:
        return _sha256_fd(fd, f"workspace file {relative}")
    except (ChapterWorkspaceError, OSError):
        return None
    finally:
        os.close(fd)


def _tree_file_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ChapterWorkspaceError(f"workspace contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if not _validate_workspace_relative(relative):
                raise ChapterWorkspaceError(
                    f"workspace contains an unsafe path: {relative}"
                )
            result[relative] = _sha256_file(path)
    return result


def _safe_file_size(root: Path, relative: str) -> int:
    fd = _open_relative_file_fd(root, relative)
    try:
        size = os.fstat(fd).st_size
        if size < 0:
            raise ChapterWorkspaceError(
                f"workspace file has an invalid size: {relative}"
            )
        return size
    finally:
        os.close(fd)


def _validate_workspace_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _validate_published_chapter(
    output: Path,
    entry: Mapping[str, Any],
    files: Mapping[str, Any],
) -> bool:
    path = entry.get("path")
    if not _validate_workspace_relative(path):
        return False
    chapter_path = str(path)
    try:
        chapter = _read_json(
            output / PurePosixPath(chapter_path) / "chapter.json",
            "chapter manifest",
        )
    except ChapterWorkspaceError:
        return False
    if not isinstance(chapter, dict) or set(chapter) != {
        "assets",
        "chapter_html_sha256",
        "end_content_idx",
        "end_page_idx",
        "kind",
        "ordinal",
        "pages",
        "schema",
        "schema_version",
        "source_chapters_sha256",
        "source_content_file_sha256",
        "source_content_sha256",
        "source_page_count",
        "source_pdf_sha256",
        "start_content_idx",
        "start_page_idx",
        "title",
    }:
        return False
    if (
        chapter.get("schema") != WORKSPACE_SCHEMA
        or chapter.get("schema_version") != WORKSPACE_SCHEMA_VERSION
        or chapter.get("ordinal") != entry.get("ordinal")
        or chapter.get("title") != entry.get("title")
        or chapter.get("kind") != entry.get("kind")
        or chapter.get("start_content_idx") != entry.get("start_content_idx")
        or chapter.get("end_content_idx") != entry.get("end_content_idx")
        or chapter.get("start_page_idx") != entry.get("start_page_idx")
        or chapter.get("end_page_idx") != entry.get("end_page_idx")
    ):
        return False
    for name in (
        "source_content_sha256",
        "source_content_file_sha256",
        "source_chapters_sha256",
        "source_pdf_sha256",
        "chapter_html_sha256",
    ):
        if not _validate_sha_value(chapter.get(name)):
            return False
    pages = chapter.get("pages")
    assets = chapter.get("assets")
    if not isinstance(pages, list) or not isinstance(assets, list):
        return False
    for relative in [*pages, *assets]:
        if not isinstance(relative, str) or not _validate_workspace_relative(
            f"{chapter_path}/{relative}"
        ):
            return False
    expected_page_files = {f"{chapter_path}/{relative}" for relative in pages}
    expected_asset_files = {f"{chapter_path}/{relative}" for relative in assets}
    expected_html = f"{chapter_path}/chapter.html"
    return (
        files.get(expected_html) == chapter["chapter_html_sha256"]
        and bool(expected_page_files)
        and expected_asset_files.issubset(files)
        and expected_page_files.issubset(files)
    )


def _freshness_fingerprint(
    *,
    content: _Content,
    chapters_sha256: str,
    source_pdf_sha256: str,
    dpi: int,
    quality: int,
    contract_sha256: str,
) -> str:
    payload = {
        "content_file_sha256": content.file_sha256,
        "content_items_sha256": content.items_sha256,
        "chapters_sha256": chapters_sha256,
        "source_pdf_sha256": source_pdf_sha256,
        "dpi": dpi,
        "quality": quality,
        "contract_sha256": contract_sha256,
    }
    return _sha256_json(payload)


def _workspace_contract_sha256() -> str:
    return _sha256_json(
        {
            "schema": WORKSPACE_SCHEMA,
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "html_filename": "chapter.html",
            "asset_paths": "assets/<stable-basename>",
            "page_names": "pages/page-NNNN.jpg",
            "page_geometry": {
                "contract": PAGE_GEOMETRY_CONTRACT,
                "coordinates": "top-left in displayed page orientation",
                "dimensions": "MinerU layout page_size",
                "pdf_reference": "displayed PDF page.rect width and height",
                "tolerance": PAGE_GEOMETRY_TOLERANCE,
                "rotation": "page.rect already includes PDF rotation; no transform",
            },
            "bbox_coordinates": "page_geometry coordinates scaled directly to rendered pixels",
            "annotation_style": {
                "stroke_width": 4.0,
                "label_font_size": 12.0,
                "label_fill_opacity": 0.5,
            },
        }
    )


def _read_json(path: Path, label: str) -> Any:
    data, _ = _read_json_document(path, label)
    return data


def _read_json_document(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        return read_json_document(
            path,
            label,
            max_bytes=MAX_INPUT_JSON_BYTES,
        )
    except StrictJsonError as exc:
        raise ChapterWorkspaceError(str(exc)) from exc
    except ChapterWorkspaceError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ChapterWorkspaceError(f"cannot read {label}: {path}") from exc


def _require_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ChapterWorkspaceError(f"{key} must be a non-empty string")
    return value


def _require_exact_int(payload: Mapping[str, Any], key: str, expected: int) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        raise ChapterWorkspaceError(f"{key} must equal {expected}")
    return value


def _require_nonnegative_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ChapterWorkspaceError(f"{key} must be a non-negative integer")
    return value


def _require_positive_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ChapterWorkspaceError(f"{key} must be a positive integer")
    return value


def _require_sha(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    _validate_sha(value, key)
    if not isinstance(value, str):
        raise ChapterWorkspaceError(f"{key} must be a lowercase SHA-256")
    return value


def _validate_sha(value: Any, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _SHA256_CHARS:
        raise ChapterWorkspaceError(f"{label} must be a lowercase SHA-256")


def _validate_sha_value(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and not (set(value) - _SHA256_CHARS)
    )


def _items_sha256(items: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_json(list(items))


def _content_source_sha256(
    items: Sequence[Mapping[str, Any]],
    page_geometry: Sequence[Mapping[str, Any]],
) -> str:
    try:
        return content_source_sha256(items, page_geometry)
    except PageGeometryError as exc:
        raise ChapterWorkspaceError(f"normalized content {exc}") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    fd = _open_regular_fd(path, f"file {path}")
    try:
        return _sha256_fd(fd, f"file {path}")
    finally:
        os.close(fd)


def _sha256_fd(fd: int, label: str) -> str:
    digest = hashlib.sha256()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError as exc:
        raise ChapterWorkspaceError(f"cannot read {label}") from exc
    return digest.hexdigest()


def _read_regular_bytes(
    path: Path,
    label: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    fd = _open_regular_fd(path, label)
    try:
        before = os.fstat(fd)
        if max_bytes is not None and before.st_size > max_bytes:
            raise ChapterWorkspaceError(f"{label} exceeds the size limit: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ChapterWorkspaceError(f"{label} exceeds the size limit: {path}")
            chunks.append(chunk)
        after = os.fstat(fd)
        if not _same_snapshot(before, after) or total != before.st_size:
            raise ChapterWorkspaceError(f"{label} changed while being read: {path}")
        return b"".join(chunks)
    except OSError as exc:
        raise ChapterWorkspaceError(f"cannot read {label}: {path}") from exc
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
        raise ChapterWorkspaceError(
            f"cannot safely open {label}: POSIX no-follow support unavailable"
        )
    path = Path(path)
    if path.is_absolute():
        components = path.parts[1:]
        directory_fd = os.open("/", os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    else:
        components = path.parts
        directory_fd = os.open(".", os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    if not components or any(part in {"", ".", ".."} for part in components):
        os.close(directory_fd)
        raise ChapterWorkspaceError(f"cannot safely open {label}: unsafe path")
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(
            components[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ChapterWorkspaceError(f"{label} is not a regular file: {path}")
        return fd
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ChapterWorkspaceError(
                f"{label} is not a regular file: {path}"
            ) from exc
        raise ChapterWorkspaceError(f"cannot safely open {label}: {path}") from exc
    finally:
        os.close(directory_fd)


def _open_relative_file_fd(root: Path, relative: str) -> int:
    if not _validate_workspace_relative(relative):
        raise ChapterWorkspaceError(f"unsafe workspace relative path: {relative!r}")
    directory_fd = os.open(
        root,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    components = PurePosixPath(relative).parts
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ChapterWorkspaceError(
                f"workspace path is not a regular file: {relative}"
            )
        return fd
    finally:
        os.close(directory_fd)


def _require_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.exists() or not path.is_file():
        raise ChapterWorkspaceError(f"{label} is not a regular file: {path}")


def _reject_symlink_ancestors(path: Path, label: str) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ChapterWorkspaceError(f"{label} path contains a symlink: {current}")
        current = current.parent


def _validate_output_parent(output: Path) -> None:
    if output.is_symlink():
        raise ChapterWorkspaceError(f"output directory is a symlink: {output}")
    current = output.parent
    while current != current.parent:
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise ChapterWorkspaceError(f"output parent is unsafe: {current}")
        current = current.parent


def _publish_directory(staging: Path, output: Path) -> None:
    backup: Path | None = None
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_dir():
            raise ChapterWorkspaceError(f"existing output is unsafe: {output}")
        backup = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.backup-", dir=output.parent)
        )
        shutil.rmtree(backup)
        os.replace(output, backup)
    try:
        os.replace(staging, output)
    except BaseException:
        if backup is not None:
            try:
                os.replace(backup, output)
            except OSError as restore_error:
                raise ChapterWorkspaceError(
                    "cannot publish chapter workspace and cannot restore old output"
                ) from restore_error
        raise
    if backup is not None:
        try:
            _remove_path(backup)
        except OSError:
            log.warning(
                "cannot remove previous chapter workspace backup after publish: %s",
                backup,
            )


def _remove_path(path: Path) -> None:
    if not path or str(path) == ".":
        return
    if path.is_symlink() or not path.exists():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


__all__ = [
    "DEFAULT_JPEG_QUALITY",
    "DEFAULT_RENDER_DPI",
    "WORKSPACE_SCHEMA",
    "WORKSPACE_SCHEMA_VERSION",
    "ChapterWorkspaceError",
    "build_chapter_edit_workspace",
    "build_chapter_workspace",
    "materialize_chapter_workspace",
]
