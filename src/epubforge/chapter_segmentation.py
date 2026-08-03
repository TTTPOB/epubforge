"""Find chapter starts in normalized MinerU content.

This module intentionally stops at ordered boundary detection.  It does not
change the normalized content or render chapter workspaces.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from epubforge.agent_runner import (
    AgentIdentity,
    AgentRunRequest,
    AgentRunner,
    AgentRunnerError,
    BOOK_EDITOR_PROMPT,
)
from epubforge.mineru_content import CONTENT_SCHEMA, CONTENT_SCHEMA_VERSION
from epubforge.page_geometry import (
    PageGeometryError,
    content_source_sha256,
    normalize_page_geometry,
)
from epubforge.strict_json import (
    DEFAULT_MAX_JSON_BYTES,
    StrictJsonError,
    parse_json_document,
    read_json_document,
)


CHAPTERS_SCHEMA = "epubforge.chapter-segmentation"
CHAPTERS_SCHEMA_VERSION = 2
CONTENT_PROJECTION_SCHEMA = "epubforge.chapter-content-projection"
CONTENT_PROJECTION_SCHEMA_VERSION = 3
MAX_INPUT_JSON_BYTES = DEFAULT_MAX_JSON_BYTES

SEGMENTATION_TASK = """# Chapter segmentation task

Mode: `segmentation`

Read `content-projection.json`. Each item carries its `content_idx`, `page_idx`,
`type`, `text_level`, and `text_chunks`. Concatenate all `text_chunks` strings
in order to recover the exact original text. Identify ordered starts for front
matter, chapters, and back matter. Use `text_level`, `type`, and surrounding content
as evidence. Dense table-of-contents title lists are not body starts.
Running headers, quotations, and embedded document or article titles can
resemble chapter starts.

The projection's top-level `coverage` object identifies the complete input
range and content: `item_count`, the first and last `content_idx`, the first
and last `page_idx`, and `items_sha256`, a SHA-256 digest of the projected
items. Inspect the entire projection before answering. Copy every `coverage`
value exactly from the projection's `coverage` object into your
`boundaries.json`; never compute, guess, or alter them. Missing, stale, or
mismatched coverage fails the run.

Write `boundaries.json` as one JSON object with exactly this shape.  The
`coverage` values shown below are illustrative placeholders; replace every
one with the exact value copied from the projection's `coverage` object:

```json
{
  "coverage": {
    "item_count": 1,
    "first_content_idx": 0,
    "last_content_idx": 0,
    "first_page_idx": 0,
    "last_page_idx": 0,
    "items_sha256": "64 hex characters copied from the projection"
  },
  "boundaries": [
    {
      "title": "exact source text",
      "kind": "chapter",
      "start_content_idx": 0,
      "start_page_idx": 0,
      "confidence": 0.0,
      "evidence": "concise reason"
    }
  ]
}
```

Preserve exact title text and source indices. Keep boundaries in source order.
Use only `frontmatter`, `chapter`, or `backmatter` for `kind`, and include at
least one `chapter` boundary. Do not repair OCR, text, geometry, or content
order. Write no other files.
"""

# The projection contract belongs in freshness fingerprints because changing
# any projected field changes the model input even when the prose stays put.
CONTENT_PROJECTION_CONTRACT: tuple[int, tuple[str, ...]] = (
    4,
    ("content_idx", "page_idx", "type", "text_level", "text_chunks"),
)

# The coverage acknowledgement is a deterministic identity/range check, not a
# cryptographic proof that the model read every item.  The field set and the
# digest canonicalization belong in the contract fingerprint so format
# changes invalidate previously-fresh artifacts.
COVERAGE_SCHEMA = "epubforge.chapter-projection-coverage"
COVERAGE_SCHEMA_VERSION = 1
_COVERAGE_FIELDS = (
    "item_count",
    "first_content_idx",
    "last_content_idx",
    "first_page_idx",
    "last_page_idx",
    "items_sha256",
)

# Physical line budget: every chunk line must stay comfortably below
# OpenCode's 2000-character read truncation.  With ``ensure_ascii=False``
# each CJK / non-BMP character occupies 1 JSON character, so the UTF-8
# byte total also stays safely under grep's 65536-byte record limit.
# ``allow_nan=False`` ensures non-finite floats never appear in any line.
#
# The chunker uses a per-character encoded-length prediction so the budget
# is enforced by construction even for repeated JSON-escaped control
# characters, quotes, and backslashes.
_MAX_LINE_CHARS = 1900
_MAX_LINE_BYTES = 65536  # grep's record limit; checked with ``<``

# Structural overhead per chunk line in the pretty-printed JSON:
#   8 spaces (indent level 4) + '"' + <encoded text> + '",'
_LINE_OVERHEAD = 11

# Serialization parameters for the projection writer.  The contract
# fingerprint mirrors every one of them, so keep the two in sync when
# changing the projection format.
_PROJECTION_ENSURE_ASCII = False
_PROJECTION_SORT_KEYS = True
_PROJECTION_INDENT = 2
_PROJECTION_ALLOW_NAN = False

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


class ChapterSegmentationPublicationError(ChapterSegmentationError):
    """Raised when publication rollback leaves recovery evidence."""

    def __init__(self, message: str, *, evidence: tuple[Path, ...] = ()) -> None:
        self.evidence = evidence
        suffix = ""
        if evidence:
            suffix = " Evidence: " + ", ".join(str(path) for path in evidence)
        super().__init__(message + suffix)


class ChapterBoundary(BaseModel):
    """One ordered start boundary returned by the chapter detector."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1)
    kind: Literal["frontmatter", "chapter", "backmatter"]
    start_content_idx: int = Field(ge=0)
    start_page_idx: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1, max_length=500)


class ProjectionCoverageAcknowledgement(BaseModel):
    """Exact input identity/range the agent claims to have inspected.

    This is a deterministic acknowledgement, not a cryptographic proof that
    the model read every projected item.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    item_count: int = Field(ge=1)
    first_content_idx: int = Field(ge=0)
    last_content_idx: int = Field(ge=0)
    first_page_idx: int = Field(ge=0)
    last_page_idx: int = Field(ge=0)
    items_sha256: str = Field(pattern=_SHA256_PATTERN)


class ChapterSegmentationResponse(BaseModel):
    """Strict contents accepted from the agent's ``boundaries.json``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    boundaries: list[ChapterBoundary] = Field(min_length=1)
    coverage: ProjectionCoverageAcknowledgement


class ChapterSegmentationArtifact(BaseModel):
    """Published chapter boundaries and the inputs that produced them."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: Literal["epubforge.chapter-segmentation"] = Field(
        default=CHAPTERS_SCHEMA,
        alias="schema",
    )
    schema_version: Literal[2] = CHAPTERS_SCHEMA_VERSION
    source_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    agent_name: str = Field(min_length=1)
    agent_model: str = Field(min_length=1)
    agent_variant: str = Field(min_length=1)
    agent_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
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


def segment_chapters(
    content_path: str | Path,
    output_dir: str | Path,
    agent_runner: AgentRunner,
    *,
    force: bool = False,
) -> Path:
    """Detect chapter starts and publish ``03_chapters/chapters.json``.

    ``output_dir`` names the work directory.  Passing a directory already
    named ``03_chapters`` also works and writes ``chapters.json`` inside it.
    The old artifact remains untouched if the agent, validation, or atomic
    publish step fails.
    """
    source_path = _resolve_content_path(Path(content_path))
    output_path = _resolve_output_path(Path(output_dir))
    _ensure_output_directory(output_path.parent)
    source_items, page_geometry = _load_source_items(source_path)
    source_sha256 = _content_source_sha256(source_items, page_geometry)
    identity = _resolve_agent_identity(agent_runner)
    prompt_sha256 = _prompt_sha256(identity)
    contract_sha256 = _contract_sha256()

    if not force:
        existing = _load_fresh_artifact(
            output_path,
            source_items=source_items,
            source_sha256=source_sha256,
            identity=identity,
            prompt_sha256=prompt_sha256,
            contract_sha256=contract_sha256,
        )
        if existing is not None:
            return output_path

    try:
        result = agent_runner(
            AgentRunRequest(
                title="epubforge chapter segmentation",
                prompt=BOOK_EDITOR_PROMPT,
                files={
                    "TASK.md": SEGMENTATION_TASK.encode("utf-8"),
                    "content-projection.json": _projection_bytes(source_items),
                },
                output_limits={"boundaries.json": MAX_INPUT_JSON_BYTES},
                forbidden_roots=(source_path.parent.parent, output_path.parent.parent),
            )
        )
        response = _parse_agent_response(result.outputs.get("boundaries.json"))
        validate_coverage_acknowledgement(response.coverage, source_items)
        validate_boundaries(response.boundaries, source_items)
    except ValidationError as exc:
        raise ChapterSegmentationError(
            f"agent chapter boundary response failed schema validation: {exc}"
        ) from exc
    except ChapterSegmentationError:
        raise
    except AgentRunnerError as exc:
        raise ChapterSegmentationError(f"book-editor agent failed: {exc}") from exc
    except Exception as exc:
        raise ChapterSegmentationError(f"book-editor agent failed: {exc}") from exc

    artifact = ChapterSegmentationArtifact(
        source_content_sha256=source_sha256,
        agent_name=identity.name,
        agent_model=identity.model,
        agent_variant=identity.variant,
        agent_fingerprint=identity.fingerprint,
        prompt_sha256=prompt_sha256,
        contract_sha256=contract_sha256,
        session_id=result.session_id,
        boundaries=response.boundaries,
    )
    _write_artifact_atomic(output_path, artifact)
    return output_path


def is_chapter_segmentation_fresh(
    content_path: str | Path,
    output_dir: str | Path,
    *,
    agent_identity: AgentIdentity,
) -> bool:
    """Return whether the published chapter plan matches current inputs.

    This preflight validates the same source, agent, prompt, contract, and
    boundary checks as :func:`segment_chapters` without constructing a runner.
    """
    source_path = _resolve_content_path(Path(content_path))
    output_path = _resolve_output_path(Path(output_dir))
    source_items, page_geometry = _load_source_items(source_path)
    source_sha256 = _content_source_sha256(source_items, page_geometry)
    if not isinstance(agent_identity, AgentIdentity):
        raise ChapterSegmentationError("a valid agent identity is required")
    return (
        _load_fresh_artifact(
            output_path,
            source_items=source_items,
            source_sha256=source_sha256,
            identity=agent_identity,
            prompt_sha256=_prompt_sha256(agent_identity),
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


def validate_coverage_acknowledgement(
    coverage: ProjectionCoverageAcknowledgement | Mapping[str, Any],
    source_items: Sequence[Mapping[str, Any]],
) -> None:
    """Require the agent's coverage acknowledgement to match the projection.

    Every expected field must match exactly.  This is a deterministic
    identity/range acknowledgement, not a cryptographic proof that the model
    read every projected item.  Call it before accepting any boundary.
    """
    if not isinstance(coverage, ProjectionCoverageAcknowledgement):
        try:
            coverage = ProjectionCoverageAcknowledgement.model_validate(coverage)
        except ValidationError as exc:
            raise ChapterSegmentationError(
                f"Agent coverage acknowledgement failed schema validation: {exc}"
            ) from exc
    expected = _projection_coverage(_project_content(source_items))
    for field in _COVERAGE_FIELDS:
        actual = getattr(coverage, field)
        if actual != expected[field]:
            raise ChapterSegmentationError(
                f"Agent coverage {field} {actual} does not match "
                f"projection {expected[field]}"
            )


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
    try:
        items_match = content.items_sha256 == _items_sha256(content.items)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ChapterSegmentationError(
            f"Normalized content items cannot be hashed: {exc}"
        ) from exc
    if not items_match:
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


def _encoded_len_contribution(ch: str) -> int:
    """Return the number of JSON characters one code point occupies.

    Under ``ensure_ascii=False``, the Python ``json`` encoder:
    - escapes ``"`` as ``\"`` (2 chars) and ``\\`` as ``\\\\`` (2 chars)
    - escapes ``\\b`` ``\\f`` ``\\n`` ``\\r`` ``\\t`` as 2-char sequences
    - escapes every other U+0000–U+001F control as ``\\uXXXX`` (6 chars)
    - outputs every remaining BMP / non-BMP code point as-is (1 char)
    """
    cp = ord(ch)
    if ch == '"' or ch == "\\":
        return 2  # \" or \\
    if ch in ("\b", "\f", "\n", "\r", "\t"):
        return 2  # \b \f \n \r \t
    if cp <= 0x1F:
        return 6  # \uXXXX
    return 1


def _chunk_text(text: str) -> list[str]:
    """Split *text* so every chunk line stays within ``_MAX_LINE_CHARS``.

    Each chunk is serialized as a JSON string and placed on a pretty-printed
    line.  The chunker tracks the cumulative encoded length
    character-by-character and breaks before the physical line would exceed
    ``_MAX_LINE_CHARS``.  This is provably correct for any input because
    ``_encoded_len_contribution`` predicts the exact JSON-encoded expansion
    of every code point.

    The concatenation of all chunks equals the original *text* byte-for-byte.
    An empty string produces ``[""]`` so every item always carries at least
    one chunk.
    """
    if not text:
        return [""]
    max_encoded = _MAX_LINE_CHARS - _LINE_OVERHEAD

    chunks: list[str] = []
    pos = 0
    n = len(text)

    while pos < n:
        encoded_len = 0
        chunk_start = pos
        while pos < n:
            contrib = _encoded_len_contribution(text[pos])
            if encoded_len + contrib > max_encoded:
                break
            encoded_len += contrib
            pos += 1

        if pos == chunk_start:
            # Edge case: a single character still fits (6 <= max_encoded).
            pos += 1

        chunks.append(text[chunk_start:pos])

    return chunks


def _validate_projection_field(value: Any, *, field_name: str) -> None:
    """Raise if *value* would produce a physical projection line exceeding
    ``_MAX_LINE_CHARS`` or the ``_MAX_LINE_BYTES`` UTF-8 byte limit.

    Accepts every JSON-serializable scalar (``str``, ``int``, ``float``,
    ``None``) and uses ``json.dumps`` with the projection's serialization
    parameters to build the exact physical line.  Non-finite floats raise
    ``ChapterSegmentationError`` (``allow_nan=False`` defense-in-depth);
    unserializable objects and lone surrogates that cannot be UTF-8 encoded
    raise it too, so every projection scalar failure surfaces as
    ``ChapterSegmentationError`` at the projection boundary.
    """
    try:
        encoded_value = json.dumps(
            value,
            ensure_ascii=_PROJECTION_ENSURE_ASCII,
            allow_nan=_PROJECTION_ALLOW_NAN,
        )
    except (TypeError, ValueError) as exc:
        raise ChapterSegmentationError(
            f"Projection field '{field_name}' cannot be serialized: {exc}"
        ) from exc

    # Build the complete line as it appears at indent level 3 (6 spaces).
    line = f'      "{field_name}": {encoded_value},'

    if len(line) > _MAX_LINE_CHARS:
        raise ChapterSegmentationError(
            f"Projection field '{field_name}' would produce a "
            f"{len(line)}-character physical line, exceeding the "
            f"{_MAX_LINE_CHARS}-character budget.  The source content "
            f"has invalid metadata for this field."
        )

    try:
        line_bytes = line.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ChapterSegmentationError(
            f"Projection field '{field_name}' cannot be encoded as UTF-8: {exc}"
        ) from exc
    if len(line_bytes) >= _MAX_LINE_BYTES:
        raise ChapterSegmentationError(
            f"Projection field '{field_name}' would produce a "
            f"{len(line_bytes)}-byte physical line, exceeding the "
            f"{_MAX_LINE_BYTES - 1}-byte record limit.  The source content "
            f"has invalid metadata for this field."
        )


def _project_content(
    source_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
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

        item_type = item["type"]
        _validate_projection_field(item_type, field_name="type")
        _validate_projection_field(text_level, field_name="text_level")

        chunks = _chunk_text(text)
        values: dict[str, Any] = {
            "content_idx": item["content_idx"],
            "page_idx": item["page_idx"],
            "type": item_type,
            "text_level": text_level,
            "text_chunks": chunks,
        }
        projected.append(
            {field: values[field] for field in CONTENT_PROJECTION_CONTRACT[1]}
        )
    return projected


def _projection_coverage(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute deterministic coverage metadata over canonical projected items.

    ``items`` must already be projected (see :func:`_project_content`).  The
    digest uses the same canonical JSON serialization as ``_items_sha256``,
    which is independent of the pretty-printed projection layout.
    """
    if not items:
        raise ChapterSegmentationError(
            "Cannot compute coverage for an empty projection"
        )
    return {
        "item_count": len(items),
        "first_content_idx": items[0]["content_idx"],
        "last_content_idx": items[-1]["content_idx"],
        "first_page_idx": items[0]["page_idx"],
        "last_page_idx": items[-1]["page_idx"],
        "items_sha256": _items_sha256(items),
    }


def _projection_bytes(source_items: Sequence[Mapping[str, Any]]) -> bytes:
    try:
        projected = _project_content(source_items)
        payload = {
            "schema": CONTENT_PROJECTION_SCHEMA,
            "schema_version": CONTENT_PROJECTION_SCHEMA_VERSION,
            "coverage": _projection_coverage(projected),
            "items": projected,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=_PROJECTION_ENSURE_ASCII,
            sort_keys=_PROJECTION_SORT_KEYS,
            indent=_PROJECTION_INDENT,
            allow_nan=_PROJECTION_ALLOW_NAN,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        # Defense-in-depth: every projected field passes _validate_projection_field
        # and every chunk line fits the character budget, so this only triggers on
        # future field additions or content not covered by the per-field checks
        # (for example lone surrogates inside item text or the coverage digest).
        raise ChapterSegmentationError(
            f"Cannot serialize the content projection: {exc}"
        ) from exc
    return encoded + b"\n"


def _prompt_sha256(identity: AgentIdentity) -> str:
    version, fields = CONTENT_PROJECTION_CONTRACT
    fingerprint_payload = {
        "agent_prompt_sha256": identity.prompt_sha256,
        "invocation_prompt": BOOK_EDITOR_PROMPT,
        "task": SEGMENTATION_TASK,
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
    contract = {
        "response_schema": ChapterSegmentationResponse.model_json_schema(),
        "projection_schema": CONTENT_PROJECTION_SCHEMA,
        "projection_schema_version": CONTENT_PROJECTION_SCHEMA_VERSION,
        "coverage": {
            "schema": COVERAGE_SCHEMA,
            "schema_version": COVERAGE_SCHEMA_VERSION,
            "fields": list(_COVERAGE_FIELDS),
            "items_digest_serialization": {
                "ensure_ascii": False,
                "sort_keys": True,
                "separators": (",", ":"),
                "allow_nan": False,
            },
        },
        "serialization": {
            "max_line_chars": _MAX_LINE_CHARS,
            "max_line_bytes": _MAX_LINE_BYTES,
            "line_overhead": _LINE_OVERHEAD,
            "indent": _PROJECTION_INDENT,
            "ensure_ascii": _PROJECTION_ENSURE_ASCII,
            "sort_keys": _PROJECTION_SORT_KEYS,
            "allow_nan": _PROJECTION_ALLOW_NAN,
        },
    }
    encoded = json.dumps(
        contract,
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


def _resolve_agent_identity(agent_runner: AgentRunner) -> AgentIdentity:
    identity = getattr(agent_runner, "identity", None)
    if not isinstance(identity, AgentIdentity):
        raise ChapterSegmentationError("agent_runner.identity must be AgentIdentity")
    return identity


def _parse_agent_response(raw: object) -> ChapterSegmentationResponse:
    if not isinstance(raw, bytes):
        raise ChapterSegmentationError("agent did not return boundaries.json")
    try:
        payload = parse_json_document(
            raw,
            label="agent chapter boundaries",
            max_bytes=MAX_INPUT_JSON_BYTES,
        )
        return ChapterSegmentationResponse.model_validate(payload)
    except StrictJsonError as exc:
        raise ChapterSegmentationError("agent boundaries.json is invalid JSON") from exc


def _load_fresh_artifact(
    path: Path,
    *,
    source_items: Sequence[Mapping[str, Any]],
    source_sha256: str,
    identity: AgentIdentity,
    prompt_sha256: str,
    contract_sha256: str,
) -> ChapterSegmentationArtifact | None:
    try:
        payload, _ = read_json_document(
            path,
            "chapter segmentation artifact",
            max_bytes=MAX_INPUT_JSON_BYTES,
            require_single_link=True,
        )
        artifact = ChapterSegmentationArtifact.model_validate(payload)
        if (
            artifact.schema_name != CHAPTERS_SCHEMA
            or artifact.schema_version != CHAPTERS_SCHEMA_VERSION
            or artifact.source_content_sha256 != source_sha256
            or artifact.agent_name != identity.name
            or artifact.agent_model != identity.model
            or artifact.agent_variant != identity.variant
            or artifact.agent_fingerprint != identity.fingerprint
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
    serialized = artifact.model_dump_json(indent=2, by_alias=True) + "\n"
    parent_fd = _open_or_create_directory(path.parent)
    temporary_name: str | None = None
    backup_name: str | None = None
    recovery_name: str | None = None
    descriptor: int | None = None
    preserve_evidence = False
    try:
        for _ in range(20):
            candidate = f".{path.name}.{secrets.token_hex(12)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor is None or temporary_name is None:
            raise ChapterSegmentationError(
                "cannot create chapter artifact temporary file"
            )
        data = serialized.encode("utf-8")
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise ChapterSegmentationError("cannot write chapter artifact")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            backup_name = f".{path.name}.{secrets.token_hex(12)}.backup"
            _link_at(parent_fd, path.name, backup_name)
        _replace_at(
            parent_fd,
            temporary_name,
            path.name,
            Path(path.parent) / temporary_name,
            path,
        )
        temporary_name = None
        try:
            os.fsync(parent_fd)
        except OSError as publish_error:
            recovery_name = f".{path.name}.{secrets.token_hex(12)}.recovery"
            try:
                recovery_name = _rollback_artifact_publication(
                    parent_fd,
                    path,
                    backup_name=backup_name,
                    recovery_name=recovery_name,
                )
            except OSError as rollback_error:
                preserve_evidence = True
                evidence = _publication_evidence(
                    path,
                    parent_fd,
                    backup_name=backup_name,
                    recovery_name=recovery_name,
                )
                raise ChapterSegmentationPublicationError(
                    "chapter artifact publication failed and rollback was not durable",
                    evidence=evidence,
                ) from rollback_error
            raise publish_error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        if not preserve_evidence:
            for name in (backup_name, recovery_name):
                if name is not None:
                    try:
                        os.unlink(name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
        os.close(parent_fd)


def _link_at(parent_fd: int, source_name: str, target_name: str) -> None:
    os.link(
        source_name,
        target_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
        follow_symlinks=False,
    )


def _rollback_artifact_publication(
    parent_fd: int,
    path: Path,
    *,
    backup_name: str | None,
    recovery_name: str,
) -> str | None:
    recovery_candidate: str | None = recovery_name
    if backup_name is None:
        _link_at(parent_fd, path.name, recovery_name)
        os.unlink(path.name, dir_fd=parent_fd)
    else:
        try:
            _link_at(parent_fd, path.name, recovery_name)
        except OSError:
            recovery_candidate = None
        os.unlink(path.name, dir_fd=parent_fd)
        _link_at(parent_fd, backup_name, path.name)
    os.fsync(parent_fd)
    return recovery_candidate if backup_name is not None else recovery_name


def _publication_evidence(
    path: Path,
    parent_fd: int,
    *,
    backup_name: str | None,
    recovery_name: str | None,
) -> tuple[Path, ...]:
    names = (path.name, backup_name, recovery_name)
    evidence: list[Path] = []
    for name in names:
        if name is None:
            continue
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            continue
        evidence.append(path.parent / name)
    return tuple(evidence)


def _ensure_output_directory(path: Path) -> None:
    descriptor = _open_or_create_directory(path)
    os.close(descriptor)


def _open_or_create_directory(path: Path) -> int:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise ChapterSegmentationError(
            "cannot safely open output directory: POSIX no-follow support unavailable"
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    components = absolute.parts[1:]
    if not components or any(part in {"", ".", ".."} for part in components):
        raise ChapterSegmentationError(f"output directory path is unsafe: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open("/", flags)
    except OSError as exc:
        raise ChapterSegmentationError("cannot open output directory root") from exc
    try:
        for component in components:
            try:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ChapterSegmentationError(
                        f"output parent is unsafe: {absolute}"
                    ) from exc
                try:
                    next_fd = os.open(component, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise ChapterSegmentationError(
                        f"output parent is unsafe: {absolute}"
                    ) from exc
            except OSError as exc:
                raise ChapterSegmentationError(
                    f"output parent is unsafe: {absolute}"
                ) from exc
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _replace_at(
    parent_fd: int,
    source_name: str,
    target_name: str,
    source_path: Path,
    target_path: Path,
) -> None:
    try:
        os.replace(
            source_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except TypeError:
        os.replace(source_path, target_path)


__all__ = [
    "CHAPTERS_SCHEMA",
    "CHAPTERS_SCHEMA_VERSION",
    "CONTENT_PROJECTION_SCHEMA",
    "CONTENT_PROJECTION_CONTRACT",
    "COVERAGE_SCHEMA",
    "COVERAGE_SCHEMA_VERSION",
    "SEGMENTATION_TASK",
    "ChapterBoundary",
    "ChapterSegmentationArtifact",
    "ChapterSegmentationError",
    "ChapterSegmentationPublicationError",
    "ChapterSegmentationResponse",
    "ProjectionCoverageAcknowledgement",
    "is_chapter_segmentation_fresh",
    "segment_chapters",
    "validate_boundaries",
    "validate_coverage_acknowledgement",
]
