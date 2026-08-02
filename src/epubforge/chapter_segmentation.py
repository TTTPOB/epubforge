"""Find chapter starts in normalized MinerU content.

This module intentionally stops at ordered boundary detection.  It does not
change the normalized content or construct Semantic IR chapters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Literal, Protocol, cast

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from epubforge.llm.client import LLMClient
from epubforge.mineru_content import CONTENT_SCHEMA, CONTENT_SCHEMA_VERSION
from epubforge.page_geometry import (
    PageGeometryError,
    content_source_sha256,
    normalize_page_geometry,
)
from epubforge.strict_json import (
    DEFAULT_MAX_JSON_BYTES,
    StrictJsonError,
    read_json_document,
)


CHAPTERS_SCHEMA = "epubforge.chapter-segmentation"
CHAPTERS_SCHEMA_VERSION = 1
MAX_INPUT_JSON_BYTES = DEFAULT_MAX_JSON_BYTES

# Keep this prompt independent from transport and source-file details.  The
# prompt fingerprint is calculated at call time so a prompt or projection edit
# invalidates a previously published result without a migration step.
SYSTEM_PROMPT = """Identify chapter starts in ordered normalized book content.
Find front matter, chapters, and back matter. Use text_level and type as clues. Dense TOC title lists are not body starts. Running headers, quotations, and embedded document/article titles can resemble chapters; use surrounding content. Do not repair OCR, text, bounding boxes, or order. Preserve exact title text and source indices. Return only structured boundaries."""

USER_PROMPT_PREFIX = """Return the ordered starts for the content items below. Each item has a stable content_idx, a zero-based page_idx, type, optional text_level, and text. Include only real starts.

CONTENT ITEMS:
"""

# The projection contract belongs in freshness fingerprints because changing
# any projected field changes the model input even when the prose stays put.
CONTENT_PROJECTION_CONTRACT: tuple[int, tuple[str, ...]] = (
    2,
    ("content_idx", "page_idx", "type", "text_level", "text"),
)

_TEXTUAL_TYPES = frozenset(
    {
        "text",
        "title",
        "heading",
        "paragraph",
        "list",
        "list_item",
        "caption",
        "footnote",
        "text_block",
    }
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SOURCE_CONTENT_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "source_archive_sha256",
        "source_archive_size",
        "source_kind",
        "segment_count",
        "page_count",
        "source_pdf_sha256",
        "items_sha256",
        "normalization",
        "assets",
        "page_geometry",
        "items",
    }
)


class ChapterSegmentationError(ValueError):
    """Raised when source content or chapter boundaries violate the contract."""


class ChapterBoundary(BaseModel):
    """One ordered start boundary returned by the chapter detector."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1)
    kind: Literal["frontmatter", "chapter", "backmatter"]
    start_content_idx: int = Field(ge=0)
    start_page_idx: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1, max_length=500)


class ChapterSegmentationResponse(BaseModel):
    """Strict structured response expected from the LLM."""

    model_config = ConfigDict(extra="forbid", strict=True)

    boundaries: list[ChapterBoundary] = Field(min_length=1)


class ChapterSegmentationArtifact(BaseModel):
    """Published chapter boundaries and the inputs that produced them."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: Literal["epubforge.chapter-segmentation"] = Field(
        default=CHAPTERS_SCHEMA,
        alias="schema",
    )
    schema_version: Literal[1] = CHAPTERS_SCHEMA_VERSION
    source_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    model: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    boundaries: list[ChapterBoundary] = Field(min_length=1)


class _NormalizedContent(BaseModel):
    """Strict reader for the committed MinerU content artifact contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: str = Field(alias="schema")
    schema_version: int
    source_archive_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_archive_size: int = Field(ge=0)
    source_kind: Literal["direct", "segmented"]
    segment_count: int = Field(ge=1)
    page_count: int = Field(ge=0)
    source_pdf_sha256: str = Field(pattern=_SHA256_PATTERN)
    items_sha256: str = Field(pattern=_SHA256_PATTERN)
    normalization: dict[str, Any]
    assets: dict[str, dict[str, Any]]
    page_geometry: list[dict[str, Any]] = Field(min_length=1)
    items: list[dict[str, Any]] = Field(min_length=1)


class ParsedChatClient(Protocol):
    """Small protocol that keeps the public function easy to test."""

    def chat_parsed(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        response_format: type[ChapterSegmentationResponse],
        validator: Callable[[ChapterSegmentationResponse], bool | None] | None = None,
        bypass_cache: bool = False,
    ) -> ChapterSegmentationResponse: ...


def segment_chapters(
    content_path: str | Path,
    output_dir: str | Path,
    llm_client: LLMClient | ParsedChatClient,
    *,
    force: bool = False,
) -> Path:
    """Detect chapter starts and publish ``03_chapters/chapters.json``.

    ``output_dir`` names the work directory.  Passing a directory already
    named ``03_chapters`` also works and writes ``chapters.json`` inside it.
    The old artifact remains untouched if the LLM, validation, or atomic
    publish step fails.
    """
    source_path = _resolve_content_path(Path(content_path))
    output_path = _resolve_output_path(Path(output_dir))
    source_items, page_geometry = _load_source_items(source_path)
    source_sha256 = _content_source_sha256(source_items, page_geometry)
    current_model = _resolve_model(llm_client)
    prompt_sha256 = _prompt_sha256()
    contract_sha256 = _contract_sha256()

    if not force:
        existing = _load_fresh_artifact(
            output_path,
            source_items=source_items,
            source_sha256=source_sha256,
            model=current_model,
            prompt_sha256=prompt_sha256,
            contract_sha256=contract_sha256,
        )
        if existing is not None:
            return output_path

    messages = _build_messages(source_items)

    def validate_response(value: ChapterSegmentationResponse) -> None:
        response = ChapterSegmentationResponse.model_validate(value)
        validate_boundaries(response.boundaries, source_items)

    try:
        raw_response = llm_client.chat_parsed(
            messages,
            response_format=ChapterSegmentationResponse,
            validator=validate_response,
            bypass_cache=force,
        )
        response = ChapterSegmentationResponse.model_validate(raw_response)
        validate_boundaries(response.boundaries, source_items)
    except ValidationError as exc:
        raise ChapterSegmentationError(
            f"LLM chapter boundary response failed schema validation: {exc}"
        ) from exc
    except ChapterSegmentationError:
        raise

    artifact = ChapterSegmentationArtifact(
        source_content_sha256=source_sha256,
        model=current_model,
        prompt_sha256=prompt_sha256,
        contract_sha256=contract_sha256,
        boundaries=response.boundaries,
    )
    _write_artifact_atomic(output_path, artifact)
    return output_path


def is_chapter_segmentation_fresh(
    content_path: str | Path,
    output_dir: str | Path,
    *,
    model: str,
) -> bool:
    """Return whether the published chapter plan matches current inputs.

    This preflight validates the same source, model, prompt, contract, and
    boundary checks as :func:`segment_chapters` without constructing an LLM
    client or making a provider request.
    """
    source_path = _resolve_content_path(Path(content_path))
    output_path = _resolve_output_path(Path(output_dir))
    source_items, page_geometry = _load_source_items(source_path)
    source_sha256 = _content_source_sha256(source_items, page_geometry)
    if not isinstance(model, str) or not model.strip():
        raise ChapterSegmentationError("An LLM model is required for freshness checks")
    return (
        _load_fresh_artifact(
            output_path,
            source_items=source_items,
            source_sha256=source_sha256,
            model=model,
            prompt_sha256=_prompt_sha256(),
            contract_sha256=_contract_sha256(),
        )
        is not None
    )


def validate_boundaries(
    boundaries: Sequence[ChapterBoundary],
    source_items: Sequence[Mapping[str, Any]],
) -> None:
    """Validate boundary references, ordering, and front/chapter/back phases."""
    item_by_idx = _index_source_items(source_items)
    if not boundaries:
        raise ChapterSegmentationError("At least one chapter boundary is required")

    previous_content_idx: int | None = None
    previous_page_idx: int | None = None
    seen_indices: set[int] = set()
    seen_chapter = False
    seen_backmatter = False

    for position, boundary in enumerate(boundaries):
        if not isinstance(boundary, ChapterBoundary):
            try:
                boundary = ChapterBoundary.model_validate(boundary)
            except ValidationError as exc:
                raise ChapterSegmentationError(
                    f"Boundary {position} failed schema validation: {exc}"
                ) from exc

        content_idx = boundary.start_content_idx
        page_idx = boundary.start_page_idx
        if content_idx in seen_indices:
            raise ChapterSegmentationError(
                f"Boundary {position} duplicates content_idx {content_idx}"
            )
        seen_indices.add(content_idx)

        if previous_content_idx is not None and content_idx <= previous_content_idx:
            raise ChapterSegmentationError(
                "Boundary content_idx values must strictly increase"
            )
        if previous_page_idx is not None and page_idx < previous_page_idx:
            raise ChapterSegmentationError("Boundary page order cannot go backward")
        previous_content_idx = content_idx
        previous_page_idx = page_idx

        item = item_by_idx.get(content_idx)
        if item is None:
            raise ChapterSegmentationError(
                f"Boundary {position} references unknown content_idx {content_idx}"
            )
        if item["page_idx"] != page_idx:
            raise ChapterSegmentationError(
                f"Boundary {position} page_idx does not match content item {content_idx}"
            )
        item_text = item.get("text")
        if (
            not _is_textual_item(item)
            or not isinstance(item_text, str)
            or not item_text.strip()
        ):
            raise ChapterSegmentationError(
                f"Boundary {position} must reference a non-empty textual item"
            )
        if boundary.title != item_text:
            raise ChapterSegmentationError(
                f"Boundary {position} title must exactly match content item {content_idx}"
            )

        if boundary.kind == "frontmatter":
            if seen_chapter or seen_backmatter:
                raise ChapterSegmentationError(
                    "Frontmatter boundaries must precede chapter boundaries"
                )
        elif boundary.kind == "chapter":
            if seen_backmatter:
                raise ChapterSegmentationError(
                    "Chapter boundaries cannot follow backmatter boundaries"
                )
            seen_chapter = True
        else:
            if not seen_chapter:
                raise ChapterSegmentationError(
                    "Backmatter boundaries require a preceding chapter boundary"
                )
            seen_backmatter = True

    if not seen_chapter:
        raise ChapterSegmentationError("At least one chapter boundary is required")


def _resolve_content_path(path: Path) -> Path:
    resolved = path / "content.json" if path.is_dir() else path
    if not resolved.is_file():
        raise ChapterSegmentationError(
            f"Normalized content file is missing: {resolved}"
        )
    return resolved


def _resolve_output_path(output_dir: Path) -> Path:
    chapter_dir = (
        output_dir if output_dir.name == "03_chapters" else output_dir / "03_chapters"
    )
    return chapter_dir / "chapters.json"


def _load_source_items(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload, _ = read_json_document(
            path,
            "normalized content",
            max_bytes=MAX_INPUT_JSON_BYTES,
        )
    except (OSError, StrictJsonError, ValueError) as exc:
        raise ChapterSegmentationError(
            f"Cannot read normalized content: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ChapterSegmentationError("Normalized content must be a JSON object")
    if set(payload) != _SOURCE_CONTENT_KEYS:
        raise ChapterSegmentationError(
            "Normalized content has an unexpected top-level contract"
        )
    try:
        content = _NormalizedContent.model_validate(payload)
    except ValidationError as exc:
        raise ChapterSegmentationError(
            "Normalized content failed its top-level contract"
        ) from exc
    if content.schema_name != CONTENT_SCHEMA:
        raise ChapterSegmentationError("Normalized content has an invalid schema")
    if content.schema_version != CONTENT_SCHEMA_VERSION:
        raise ChapterSegmentationError(
            "Normalized content has an invalid schema version"
        )
    if content.items_sha256 != _items_sha256(content.items):
        raise ChapterSegmentationError(
            "Normalized content items_sha256 does not match items"
        )

    normalized = list(content.items)
    _validate_ordered_source_items(normalized, page_count=content.page_count)
    geometry = _validate_page_geometry(
        list(content.page_geometry), page_count=content.page_count
    )
    return normalized, list(geometry)


def _validate_ordered_source_items(
    items: Sequence[Mapping[str, Any]], *, page_count: int
) -> None:
    previous_page_idx: int | None = None
    for position, item in enumerate(items):
        content_idx = item.get("content_idx")
        page_idx = item.get("page_idx")
        item_type = item.get("type")
        if (
            not isinstance(content_idx, int)
            or isinstance(content_idx, bool)
            or content_idx != position
        ):
            raise ChapterSegmentationError(
                "Normalized content items must have contiguous ordered content_idx values"
            )
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx < 0:
            raise ChapterSegmentationError(
                f"Content item {position} has an invalid page_idx"
            )
        if page_idx >= page_count:
            raise ChapterSegmentationError(
                f"Content item {position} page_idx is outside page_count"
            )
        if previous_page_idx is not None and page_idx < previous_page_idx:
            raise ChapterSegmentationError(
                "Normalized content page order cannot go backward"
            )
        if not isinstance(item_type, str) or not item_type.strip():
            raise ChapterSegmentationError(
                f"Content item {position} has an invalid type"
            )
        previous_page_idx = page_idx


def _validate_page_geometry(
    geometry: Sequence[Mapping[str, Any]], *, page_count: int
) -> tuple[dict[str, int | float], ...]:
    try:
        return normalize_page_geometry(geometry, page_count=page_count)
    except PageGeometryError as exc:
        raise ChapterSegmentationError(f"Normalized content {exc}") from exc


def _index_source_items(
    source_items: Sequence[Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    item_by_idx: dict[int, Mapping[str, Any]] = {}
    previous_idx: int | None = None
    for position, item in enumerate(source_items):
        content_idx = item.get("content_idx")
        if not isinstance(content_idx, int) or isinstance(content_idx, bool):
            raise ChapterSegmentationError(
                f"Content item {position} has an invalid content_idx"
            )
        if content_idx in item_by_idx:
            raise ChapterSegmentationError(f"Duplicate content_idx {content_idx}")
        if previous_idx is not None and content_idx <= previous_idx:
            raise ChapterSegmentationError(
                "Source content_idx values must strictly increase"
            )
        item_by_idx[content_idx] = item
        previous_idx = content_idx
    return item_by_idx


def _is_textual_item(item: Mapping[str, Any]) -> bool:
    item_type = item.get("type")
    return isinstance(item_type, str) and item_type.strip().lower() in _TEXTUAL_TYPES


def _build_messages(
    source_items: Sequence[Mapping[str, Any]],
) -> list[ChatCompletionMessageParam]:
    projected: list[dict[str, Any]] = []
    for item in source_items:
        text_level = item.get("text_level")
        if not isinstance(text_level, (str, int, float)) or isinstance(
            text_level, bool
        ):
            text_level = None
        text = item.get("text")
        if not isinstance(text, str):
            text = ""
        values: dict[str, Any] = {
            "content_idx": item["content_idx"],
            "page_idx": item["page_idx"],
            "type": item["type"],
            "text_level": text_level,
            "text": text,
        }
        projected.append(
            {field: values[field] for field in CONTENT_PROJECTION_CONTRACT[1]}
        )
    user_content = USER_PROMPT_PREFIX + json.dumps(
        projected,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return cast(
        list[ChatCompletionMessageParam],
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )


def _prompt_sha256() -> str:
    version, fields = CONTENT_PROJECTION_CONTRACT
    fingerprint_payload = {
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt_prefix": USER_PROMPT_PREFIX,
        "content_projection": {"version": version, "fields": list(fields)},
    }
    return _sha256_text(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _contract_sha256() -> str:
    schema = ChapterSegmentationResponse.model_json_schema()
    encoded = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _items_sha256(items: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        list(items),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_source_sha256(
    items: Sequence[Mapping[str, Any]],
    page_geometry: Sequence[Mapping[str, Any]],
) -> str:
    try:
        return content_source_sha256(items, page_geometry)
    except PageGeometryError as exc:
        raise ChapterSegmentationError(f"Normalized content {exc}") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_model(llm_client: LLMClient | ParsedChatClient) -> str:
    candidate = getattr(llm_client, "model", None)
    resolved = candidate if isinstance(candidate, str) else None
    if not resolved:
        raise ChapterSegmentationError(
            "An LLM model is required; configure llm_client.model"
        )
    return resolved


def _load_fresh_artifact(
    path: Path,
    *,
    source_items: Sequence[Mapping[str, Any]],
    source_sha256: str,
    model: str,
    prompt_sha256: str,
    contract_sha256: str,
) -> ChapterSegmentationArtifact | None:
    try:
        payload, _ = read_json_document(
            path,
            "chapter segmentation artifact",
            max_bytes=MAX_INPUT_JSON_BYTES,
        )
        artifact = ChapterSegmentationArtifact.model_validate(payload)
        if (
            artifact.schema_name != CHAPTERS_SCHEMA
            or artifact.schema_version != CHAPTERS_SCHEMA_VERSION
            or artifact.source_content_sha256 != source_sha256
            or artifact.model != model
            or artifact.prompt_sha256 != prompt_sha256
            or artifact.contract_sha256 != contract_sha256
        ):
            return None
        validate_boundaries(artifact.boundaries, source_items)
    except (
        OSError,
        StrictJsonError,
        ValueError,
        ValidationError,
        ChapterSegmentationError,
    ):
        return None
    return artifact


def _write_artifact_atomic(path: Path, artifact: ChapterSegmentationArtifact) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = artifact.model_dump_json(indent=2, by_alias=True) + "\n"
    fd: int | None = None
    temporary_path: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if fd is not None:
            os.close(fd)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


# Names kept as small conveniences for later pipeline wiring and callers that
# prefer describing the response rather than the operation.
ChapterSegmentationResult = ChapterSegmentationResponse
segment_mineru_chapters = segment_chapters


__all__ = [
    "CHAPTERS_SCHEMA",
    "CHAPTERS_SCHEMA_VERSION",
    "CONTENT_PROJECTION_CONTRACT",
    "SYSTEM_PROMPT",
    "USER_PROMPT_PREFIX",
    "ChapterBoundary",
    "ChapterSegmentationArtifact",
    "ChapterSegmentationError",
    "ChapterSegmentationResponse",
    "ChapterSegmentationResult",
    "is_chapter_segmentation_fresh",
    "segment_chapters",
    "segment_mineru_chapters",
    "validate_boundaries",
]
