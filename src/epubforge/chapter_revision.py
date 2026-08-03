"""Isolated agent revision of one prepared chapter."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from html.parser import HTMLParser
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Literal, cast

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from epubforge.agent_runner import (
    AgentIdentity,
    AgentRunRequest,
    AgentRunner,
    AgentRunnerError,
    BOOK_EDITOR_PROMPT,
)
from epubforge.strict_json import StrictJsonError, parse_json_document


WORKSPACE_SCHEMA = "epubforge.chapter-workspace"
WORKSPACE_SCHEMA_VERSION = 1
REVISION_SCHEMA = "epubforge.chapter-revision"
REVISION_SCHEMA_VERSION = 2
REVISION_CONTRACT_VERSION = 2

MAX_WORKSPACE_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_CHAPTER_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_HTML_ELEMENTS = 20_000
MAX_IMAGE_COUNT = 256
MAX_IMAGES = MAX_IMAGE_COUNT
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024
MAX_ASSET_COUNT = 512
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_FILE_BYTES = 512 * 1024 * 1024
MAX_WORKSPACE_FILES = 20_000
MAX_WORKSPACE_DEPTH = 8
MAX_CHAPTER_COUNT = 4096

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHA256_RE = re.compile(_SHA256_PATTERN)
_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_PART_RE = re.compile(r"^([1-9][0-9]*)/([1-9][0-9]*)$")
_PAGE_RE = re.compile(r"^pages/page-([0-9]{4,})\.jpg$")
_NUMBER_RE = re.compile(r"^[0-9]+$")
_DIMENSION_RE = re.compile(r"^(?:[1-9][0-9]*)(?:\.[0-9]+)?$")

_LOCK_FILENAME = ".chapter-revision.lock"
_STAGING_PREFIX = ".revision-staging-"
_BACKUP_PREFIX = ".revision-backup-"
_RECOVERY_PREFIX = ".revision-recovery-"
_log = logging.getLogger(__name__)

_REMOVABLE_TYPES = frozenset(
    {"header", "page_header", "footer", "page_footer", "page_number", "number"}
)
_VOID_TAGS = frozenset({"br", "col", "hr", "img", "meta"})
_ALLOWED_TAGS = frozenset(
    {
        "html",
        "head",
        "body",
        "title",
        "meta",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "div",
        "span",
        "section",
        "article",
        "header",
        "footer",
        "aside",
        "sup",
        "sub",
        "strong",
        "em",
        "b",
        "i",
        "blockquote",
        "pre",
        "code",
        "ul",
        "ol",
        "li",
        "figure",
        "figcaption",
        "img",
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
        "br",
        "hr",
    }
)
_GLOBAL_ATTRS = frozenset(
    {
        "id",
        "class",
        "role",
        "data-content-idx",
        "data-page-idx",
        "data-type",
        "data-bbox",
        "data-bbox-status",
        "data-content-part",
        "data-uncertain",
    }
)
_TAG_ATTRS: dict[str, frozenset[str]] = {
    "html": frozenset({"lang"}),
    "meta": frozenset({"charset", "name", "content"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "col": frozenset({"span"}),
    "th": frozenset({"colspan", "rowspan", "scope", "abbr"}),
    "td": frozenset({"colspan", "rowspan"}),
}

REVISION_TASK_PREFIX = """# Chapter correction task

Mode: `revision`

Read `chapter.json`, `chapter.html`, referenced files under `assets/`, and the
annotated JPEG files under `pages/` in the order listed below. Python has
pre-seeded `corrected.html` with the complete source HTML. Edit only
`corrected.html`; keep it as one complete HTML document.

Correct headings and section boundaries, semantic block tags, reading order,
OCR text, repeated headers or footers, page numbers, tables, figures, captions,
footnotes, and paragraphs that continue across pages when the supplied evidence
supports the change.

Keep every substantive `data-content-idx` reference. You may remove a reference
only when the evidence identifies it as a repeated header, footer, or page
number. One element may use an ordered space-separated list to merge content
IDs. A split may repeat one ID only with `data-content-part="n/N"`; use every
part exactly once and in order.

Keep every original `data-bbox` numeric value. If a rectangle is unreliable,
keep its value and add `data-bbox-status="needs-repair"`. Never create
coordinates. Do not guess unsupported text. Mark uncertain text with
`<span data-uncertain="true">...</span>`. Remove only identified repeated
headers, footers, or page numbers. Keep local asset `src` values unchanged.

Ordered annotated page evidence:
"""


class ChapterRevisionError(ValueError):
    """Raised when a chapter workspace or revision violates its contract."""


class ChapterRevisionPublicationError(ChapterRevisionError):
    """Raised when publication or rollback leaves actionable recovery evidence."""

    def __init__(self, message: str, *, evidence: tuple[Path, ...] = ()) -> None:
        self.evidence = evidence
        suffix = ""
        if evidence:
            suffix = " Evidence: " + ", ".join(str(path) for path in evidence)
        super().__init__(message + suffix)


class ChapterRevisionRecord(BaseModel):
    """Metadata proving which inputs produced ``corrected.html``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: Literal["epubforge.chapter-revision"] = Field(
        default=REVISION_SCHEMA, alias="schema"
    )
    schema_version: Literal[2] = REVISION_SCHEMA_VERSION
    agent_name: str = Field(min_length=1)
    agent_model: str = Field(min_length=1)
    agent_variant: str = Field(min_length=1)
    agent_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    workspace_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_chapter_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_chapter_html_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_images_sha256: dict[str, str]
    source_assets_sha256: dict[str, str]
    corrected_html_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_hash_maps(self) -> ChapterRevisionRecord:
        if any(
            not _SHA256_RE.fullmatch(value)
            for value in (
                *self.source_images_sha256.values(),
                *self.source_assets_sha256.values(),
            )
        ):
            raise ValueError("source file hashes must be lowercase SHA-256 values")
        return self


@dataclass(frozen=True)
class ChapterRevisionFailure:
    """One failed chapter in a batch report."""

    chapter: Path
    error: str


@dataclass
class ChapterRevisionReport:
    """Batch outcome; paths retain ordinal order."""

    completed: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[Path] = field(default_factory=list)
    errors: dict[Path, str] = field(default_factory=dict)

    @property
    def failures(self) -> list[ChapterRevisionFailure]:
        return [ChapterRevisionFailure(path, self.errors[path]) for path in self.failed]


@dataclass(frozen=True)
class _WorkspaceFingerprint:
    content_sha256: str
    content_file_sha256: str
    chapters_sha256: str
    source_pdf_sha256: str


class _RootChapterEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ordinal: int = Field(ge=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    kind: Literal["frontmatter", "chapter", "backmatter"]
    start_content_idx: int = Field(ge=0)
    end_content_idx: int = Field(ge=0)
    start_page_idx: int = Field(ge=0)
    end_page_idx: int = Field(ge=0)
    chapter_sha256: str = Field(pattern=_SHA256_PATTERN)


class _RootManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: str = Field(alias="schema", min_length=1)
    schema_version: int
    source_fingerprints: dict[str, str]
    freshness_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    render_dpi: int = Field(ge=1)
    jpeg_quality: int = Field(ge=1, le=100)
    chapters: list[_RootChapterEntry] = Field(min_length=1)
    files_sha256: dict[str, str] = Field(min_length=1)


class _ChapterManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: str = Field(alias="schema", min_length=1)
    schema_version: int
    source_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_content_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_chapters_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_pdf_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_page_count: int = Field(ge=1)
    ordinal: int = Field(ge=1)
    title: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    start_content_idx: int = Field(ge=0)
    end_content_idx: int = Field(ge=0)
    start_page_idx: int = Field(ge=0)
    end_page_idx: int = Field(ge=0)
    chapter_html_sha256: str = Field(pattern=_SHA256_PATTERN)
    pages: list[str] = Field(min_length=1)
    assets: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _SourceElement:
    content_id: str
    bbox: tuple[str, str, str, str] | None
    removable: bool


@dataclass(frozen=True)
class _SeedInfo:
    source: dict[str, _SourceElement]
    assets: frozenset[str]
    referenced_assets: frozenset[str]


@dataclass(frozen=True)
class _ChapterInput:
    workspace_dir: Path
    chapter_dir: Path
    root_manifest: _RootManifest
    root_manifest_sha256: str
    chapter_manifest: _ChapterManifest
    chapter_manifest_bytes: bytes
    chapter_manifest_sha256: str
    chapter_html: str
    chapter_html_sha256: str
    page_bytes: tuple[tuple[str, bytes, str], ...]
    asset_bytes: tuple[tuple[str, bytes, str], ...]
    seed: _SeedInfo


@dataclass(frozen=True)
class _WorkspaceInput:
    root: Path
    manifest: _RootManifest
    manifest_sha256: str
    entries: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _HTMLNode:
    tag: str
    attrs: dict[str, str]
    children: tuple[_HTMLNode | str, ...]


class _HTMLParseError(ValueError):
    pass


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[_HTMLNode | str] = []
        self.stack: list[tuple[str, dict[str, str], list[_HTMLNode | str]]] = []
        self.doctype_count = 0

    def _children(self) -> list[_HTMLNode | str]:
        return self.stack[-1][2] if self.stack else self.root

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower() != "doctype html":
            raise _HTMLParseError("only the HTML doctype is allowed")
        self.doctype_count += 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=tag.lower() in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=True)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        lowered = tag.lower()
        if lowered in _VOID_TAGS and not self_closing:
            self_closing = True
        if any(name is None or value is None for name, value in attrs):
            raise _HTMLParseError("boolean HTML attributes are not allowed")
        normalized: dict[str, str] = {}
        for name, value in attrs:
            assert name is not None and value is not None
            key = name.lower()
            if key in normalized:
                raise _HTMLParseError(f"duplicate attribute: {key}")
            normalized[key] = value
        children: list[_HTMLNode | str] = []
        self._children().append(_HTMLNode(lowered, normalized, tuple(children)))
        if not self_closing:
            self.stack.append((lowered, normalized, children))
        else:
            current = self._children()
            node = current[-1]
            if not isinstance(node, _HTMLNode):
                raise _HTMLParseError("internal HTML tree error")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if not self.stack or self.stack[-1][0] != lowered:
            raise _HTMLParseError(f"unexpected closing tag: {lowered}")
        name, attrs, children = self.stack.pop()
        node = _HTMLNode(name, attrs, tuple(children))
        target = self._children()
        if not target or not isinstance(target[-1], _HTMLNode):
            raise _HTMLParseError("invalid element nesting")
        target[-1] = node

    def handle_data(self, data: str) -> None:
        self._children().append(data)

    def handle_comment(self, data: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        raise _HTMLParseError("processing instructions are not allowed")

    def close(self) -> None:
        super().close()
        if self.stack:
            raise _HTMLParseError(f"unclosed tag: {self.stack[-1][0]}")


def revise_chapter(
    chapter_dir: str | Path,
    agent_runner: AgentRunner,
    *,
    force: bool = False,
) -> Path:
    """Revise one ``04_edit/chapters/NNNN`` directory.

    The return value always points to the published ``corrected.html``.  A
    fresh validated output returns without starting an agent run.
    """
    chapter_path = _absolute_path(Path(chapter_dir))
    workspace_dir = _workspace_for_chapter(chapter_path)
    workspace = _load_workspace(workspace_dir)
    entry = _entry_for_chapter(workspace, chapter_path)
    result = _revise_one(
        workspace,
        entry,
        agent_runner,
        force=force,
    )
    return result[0]


def revise_all_chapters(
    edit_dir: str | Path,
    agent_runner: AgentRunner,
    *,
    force: bool = False,
    continue_on_error: bool = False,
) -> ChapterRevisionReport:
    """Revise chapters in ordinal order and report each outcome.

    The default stops after the first failure.  ``continue_on_error=True``
    lets later chapters run while preserving all successful publications.
    """
    workspace_dir = _resolve_edit_dir(Path(edit_dir))
    workspace = _load_workspace(workspace_dir)
    report = ChapterRevisionReport()
    for entry in workspace.entries:
        chapter_path = workspace.root / PurePosixPath(cast(str, entry["path"]))
        try:
            path, skipped = _revise_one(
                workspace,
                entry,
                agent_runner,
                force=force,
            )
        except Exception as exc:
            report.failed.append(chapter_path)
            report.errors[chapter_path] = str(exc)
            if not continue_on_error:
                break
        else:
            if skipped:
                report.skipped.append(path.parent)
            else:
                report.completed.append(path.parent)
    return report


def _revise_one(
    workspace: _WorkspaceInput,
    entry: Mapping[str, Any],
    agent_runner: AgentRunner,
    *,
    force: bool,
) -> tuple[Path, bool]:
    chapter_path = workspace.root / PurePosixPath(cast(str, entry["path"]))
    with _chapter_lock(chapter_path):
        _cleanup_orphans(chapter_path)
        return _revise_one_locked(workspace, entry, agent_runner, force=force)


def _revise_one_locked(
    workspace: _WorkspaceInput,
    entry: Mapping[str, Any],
    agent_runner: AgentRunner,
    *,
    force: bool,
) -> tuple[Path, bool]:
    identity = _resolve_agent_identity(agent_runner)
    chapter_path = workspace.root / PurePosixPath(cast(str, entry["path"]))
    chapter = _load_chapter(workspace, entry, chapter_path)
    task = _revision_task(chapter)
    prompt_hash = _prompt_sha256(identity, task)
    contract_hash = _contract_sha256()
    corrected_path = chapter_path / "corrected.html"
    revision_path = chapter_path / "revision.json"

    if not force and _fresh_output(
        chapter,
        corrected_path,
        revision_path,
        identity=identity,
        prompt_hash=prompt_hash,
        contract_hash=contract_hash,
    ):
        return corrected_path, True

    try:
        files = _agent_workspace_files(chapter, task)
        result = agent_runner(
            AgentRunRequest(
                title=f"epubforge chapter {chapter.chapter_manifest.ordinal:04d}",
                prompt=BOOK_EDITOR_PROMPT,
                files=files,
                output_limits={"corrected.html": MAX_HTML_BYTES},
                forbidden_roots=(workspace.root.parent,),
            )
        )
        corrected_bytes = result.outputs.get("corrected.html")
        if not isinstance(corrected_bytes, bytes):
            raise ChapterRevisionError("agent did not return corrected.html")
        try:
            corrected_html = corrected_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChapterRevisionError("corrected HTML is not UTF-8") from exc
        _validate_corrected_html(corrected_html, chapter.seed)
    except ChapterRevisionError:
        raise
    except AgentRunnerError as exc:
        raise ChapterRevisionError(f"chapter revision agent failed: {exc}") from exc
    except Exception as exc:
        raise ChapterRevisionError(f"chapter revision agent failed: {exc}") from exc

    if len(corrected_bytes) > MAX_HTML_BYTES:
        raise ChapterRevisionError("corrected HTML exceeds the size limit")
    corrected_hash = _sha256_bytes(corrected_bytes)
    record = ChapterRevisionRecord(
        agent_name=identity.name,
        agent_model=identity.model,
        agent_variant=identity.variant,
        agent_fingerprint=identity.fingerprint,
        prompt_sha256=prompt_hash,
        contract_sha256=contract_hash,
        session_id=result.session_id,
        workspace_manifest_sha256=chapter.root_manifest_sha256,
        source_chapter_manifest_sha256=chapter.chapter_manifest_sha256,
        source_chapter_html_sha256=chapter.chapter_html_sha256,
        source_images_sha256={name: digest for name, _, digest in chapter.page_bytes},
        source_assets_sha256={
            name: digest
            for name, _, digest in chapter.asset_bytes
            if name in chapter.seed.referenced_assets
        },
        corrected_html_sha256=corrected_hash,
    )
    _publish_pair(
        chapter_path,
        corrected_bytes,
        (record.model_dump_json(indent=2, by_alias=True) + "\n").encode("utf-8"),
    )
    return corrected_path, False


def _revision_task(chapter: _ChapterInput) -> str:
    ordered_pages = "\n".join(
        f"{position}. `{relative}`"
        for position, (relative, _, _) in enumerate(chapter.page_bytes, start=1)
    )
    return REVISION_TASK_PREFIX + ordered_pages + "\n\nWrite no other files.\n"


def _agent_workspace_files(chapter: _ChapterInput, task: str) -> dict[str, bytes]:
    files = {
        "TASK.md": task.encode("utf-8"),
        "chapter.json": chapter.chapter_manifest_bytes,
        "chapter.html": chapter.chapter_html.encode("utf-8"),
        "corrected.html": chapter.chapter_html.encode("utf-8"),
    }
    files.update({name: data for name, data, _ in chapter.page_bytes})
    files.update(
        {
            name: data
            for name, data, _ in chapter.asset_bytes
            if name in chapter.seed.referenced_assets
        }
    )
    return files


def _prompt_sha256(identity: AgentIdentity, task: str) -> str:
    return _sha256_json(
        {
            "version": REVISION_CONTRACT_VERSION,
            "agent_prompt_sha256": identity.prompt_sha256,
            "invocation_prompt": BOOK_EDITOR_PROMPT,
            "task": task,
        }
    )


def _contract_sha256() -> str:
    return _sha256_json(
        {
            "version": REVISION_CONTRACT_VERSION,
            "output_file": "corrected.html",
            "html_tags": sorted(_ALLOWED_TAGS),
            "html_attributes": sorted(_GLOBAL_ATTRS),
            "reference_contract": {
                "merge": "ordered space-separated data-content-idx tokens",
                "split": "data-content-part=n/N with complete 1..N parts",
                "bbox_status": "needs-repair",
            },
        }
    )


@contextmanager
def _chapter_lock(chapter_dir: Path) -> Iterator[None]:
    """Serialize every public read and publication for one chapter."""
    _ensure_directory(chapter_dir, "chapter directory")
    lock_path = chapter_dir / _LOCK_FILENAME
    try:
        if lock_path.is_symlink():
            raise ChapterRevisionError(f"chapter lock is a symlink: {lock_path}")
    except OSError as exc:
        raise ChapterRevisionError(f"cannot inspect chapter lock: {lock_path}") from exc
    lock = FileLock(str(lock_path))
    try:
        lock.acquire()
    except Exception as exc:
        raise ChapterRevisionError(
            f"cannot acquire chapter revision lock: {lock_path}"
        ) from exc
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception as exc:
            _log.warning(
                "could not release chapter revision lock %s: %s", lock_path, exc
            )


def _cleanup_orphans(chapter_dir: Path) -> None:
    """Remove only generated staging and backup leftovers while locked."""
    try:
        entries = list(os.scandir(chapter_dir))
    except OSError as exc:
        raise ChapterRevisionError(
            f"cannot inspect revision leftovers: {chapter_dir}"
        ) from exc
    for entry in entries:
        if not (
            entry.name.startswith(_STAGING_PREFIX)
            or entry.name.startswith(_BACKUP_PREFIX)
        ):
            continue
        path = Path(entry.path)
        try:
            if entry.is_symlink():
                raise ChapterRevisionError(f"revision leftover is a symlink: {path}")
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(path)
            elif entry.is_file(follow_symlinks=False):
                path.unlink()
            else:
                raise ChapterRevisionError(f"revision leftover is not regular: {path}")
        except ChapterRevisionError:
            raise
        except OSError as exc:
            _log.warning("could not clean revision leftover %s: %s", path, exc)


def _fresh_output(
    chapter: _ChapterInput,
    corrected_path: Path,
    revision_path: Path,
    *,
    identity: AgentIdentity,
    prompt_hash: str,
    contract_hash: str,
) -> bool:
    for path in (corrected_path, revision_path):
        try:
            if path.is_symlink():
                raise ChapterRevisionError(f"output is a symlink: {path}")
            os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ChapterRevisionError(f"cannot inspect output: {path}") from exc

    try:
        corrected_bytes = _read_snapshot(
            corrected_path,
            "corrected HTML",
            max_bytes=MAX_HTML_BYTES,
            require_single_link=True,
        )
        revision_bytes = _read_snapshot(
            revision_path,
            "chapter revision metadata",
            max_bytes=MAX_CHAPTER_MANIFEST_BYTES,
            require_single_link=True,
        )
        payload = parse_json_document(
            revision_bytes,
            label="chapter revision metadata",
            max_bytes=MAX_CHAPTER_MANIFEST_BYTES,
        )
        record = ChapterRevisionRecord.model_validate(payload)
        if (
            record.agent_name != identity.name
            or record.agent_model != identity.model
            or record.agent_variant != identity.variant
            or record.agent_fingerprint != identity.fingerprint
            or record.prompt_sha256 != prompt_hash
            or record.contract_sha256 != contract_hash
            or record.workspace_manifest_sha256 != chapter.root_manifest_sha256
            or record.source_chapter_manifest_sha256 != chapter.chapter_manifest_sha256
            or record.source_chapter_html_sha256 != chapter.chapter_html_sha256
            or record.source_images_sha256
            != {name: digest for name, _, digest in chapter.page_bytes}
            or record.source_assets_sha256
            != {
                name: digest
                for name, _, digest in chapter.asset_bytes
                if name in chapter.seed.referenced_assets
            }
            or record.corrected_html_sha256 != _sha256_bytes(corrected_bytes)
        ):
            return False
        corrected_html = corrected_bytes.decode("utf-8")
        _validate_corrected_html(corrected_html, chapter.seed)
    except (
        ChapterRevisionError,
        OSError,
        UnicodeError,
        StrictJsonError,
        ValidationError,
    ):
        return False
    return True


def _load_workspace(root: Path) -> _WorkspaceInput:
    root = _absolute_path(root)
    _ensure_directory(root, "chapter workspace")
    manifest_path = root / "manifest.json"
    manifest_bytes = _read_snapshot(
        manifest_path,
        "chapter workspace manifest",
        max_bytes=MAX_WORKSPACE_MANIFEST_BYTES,
    )
    try:
        payload = parse_json_document(
            manifest_bytes,
            label="chapter workspace manifest",
            max_bytes=MAX_WORKSPACE_MANIFEST_BYTES,
        )
        manifest = _RootManifest.model_validate(payload)
    except (StrictJsonError, ValidationError, ValueError) as exc:
        raise ChapterRevisionError("chapter workspace manifest is invalid") from exc
    _validate_root_manifest(manifest)
    validated_entries = tuple(sorted(manifest.chapters, key=lambda item: item.ordinal))
    entries = tuple(entry.model_dump() for entry in validated_entries)
    _validate_root_entries(entries)
    chapter_prefixes = tuple(f"{cast(str, entry['path'])}/" for entry in entries)
    if any(
        not relative.startswith(chapter_prefixes) for relative in manifest.files_sha256
    ):
        raise ChapterRevisionError("workspace file hash is outside a listed chapter")

    actual_files = _list_files(root)
    expected_files = set(manifest.files_sha256)
    output_files = {f"{entry['path']}/corrected.html" for entry in entries} | {
        f"{entry['path']}/revision.json" for entry in entries
    }
    allowed_files = expected_files | {"manifest.json"} | output_files
    auxiliary_files = {
        relative
        for relative in actual_files
        if _is_revision_auxiliary(relative, entries)
    }
    unknown = actual_files - allowed_files - auxiliary_files
    missing = expected_files - actual_files
    if unknown:
        raise ChapterRevisionError(
            f"workspace contains unlisted files: {sorted(unknown)!r}"
        )
    if missing:
        raise ChapterRevisionError(f"workspace is missing files: {sorted(missing)!r}")
    for relative, expected_hash in manifest.files_sha256.items():
        _validate_relative_path(relative, "workspace manifest file")
        actual_hash = _hash_snapshot(
            root / PurePosixPath(relative),
            f"workspace file {relative}",
            max_bytes=MAX_WORKSPACE_FILE_BYTES,
        )
        if actual_hash != expected_hash:
            raise ChapterRevisionError(f"workspace file hash mismatch: {relative}")
    return _WorkspaceInput(
        root=root,
        manifest=manifest,
        manifest_sha256=_sha256_bytes(manifest_bytes),
        entries=entries,
    )


def _validate_root_manifest(manifest: _RootManifest) -> None:
    if manifest.schema_name != WORKSPACE_SCHEMA:
        raise ChapterRevisionError("chapter workspace has an invalid schema")
    if manifest.schema_version != WORKSPACE_SCHEMA_VERSION:
        raise ChapterRevisionError("chapter workspace has an invalid schema version")
    expected_fingerprints = {
        "content_sha256",
        "content_file_sha256",
        "chapters_sha256",
        "source_pdf_sha256",
    }
    if set(manifest.source_fingerprints) != expected_fingerprints:
        raise ChapterRevisionError("workspace source fingerprints are invalid")
    for key, value in manifest.source_fingerprints.items():
        if not _SHA256_RE.fullmatch(value):
            raise ChapterRevisionError(f"workspace fingerprint is not SHA-256: {key}")
    if not (72 <= manifest.render_dpi <= 300):
        raise ChapterRevisionError("workspace render DPI is outside its contract")
    for relative, digest in manifest.files_sha256.items():
        _validate_relative_path(relative, "workspace manifest file")
        if (
            relative == "manifest.json"
            or not relative.startswith("chapters/")
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise ChapterRevisionError("workspace file hash map is invalid")


def _validate_root_entries(entries: tuple[dict[str, Any], ...]) -> None:
    if len(entries) > MAX_CHAPTER_COUNT:
        raise ChapterRevisionError("workspace contains too many chapters")
    ordinals: list[int] = []
    for entry in entries:
        required = {
            "ordinal",
            "path",
            "title",
            "kind",
            "start_content_idx",
            "end_content_idx",
            "start_page_idx",
            "end_page_idx",
            "chapter_sha256",
        }
        if set(entry) != required:
            raise ChapterRevisionError(
                "workspace chapter entry has an invalid contract"
            )
        ordinal = entry["ordinal"]
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 1
            or entry["path"] != f"chapters/{ordinal:04d}"
        ):
            raise ChapterRevisionError("workspace chapter ordinal or path is invalid")
        for key in (
            "start_content_idx",
            "end_content_idx",
            "start_page_idx",
            "end_page_idx",
        ):
            value = entry[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ChapterRevisionError(f"workspace chapter range is invalid: {key}")
        if entry["end_content_idx"] < entry["start_content_idx"]:
            raise ChapterRevisionError("workspace chapter content range is reversed")
        if entry["end_page_idx"] < entry["start_page_idx"]:
            raise ChapterRevisionError("workspace chapter page range is reversed")
        if not isinstance(entry["title"], str) or not entry["title"]:
            raise ChapterRevisionError("workspace chapter title is invalid")
        if entry["kind"] not in {"frontmatter", "chapter", "backmatter"}:
            raise ChapterRevisionError("workspace chapter kind is invalid")
        if not _SHA256_RE.fullmatch(cast(str, entry["chapter_sha256"])):
            raise ChapterRevisionError("workspace chapter hash is invalid")
        ordinals.append(ordinal)
    if ordinals != sorted(set(ordinals)):
        raise ChapterRevisionError("workspace chapter ordinals are not unique")


def _is_revision_auxiliary(
    relative: str,
    entries: tuple[dict[str, Any], ...],
) -> bool:
    for entry in entries:
        prefix = f"{entry['path']}/"
        if not relative.startswith(prefix):
            continue
        remainder = relative[len(prefix) :]
        if remainder == _LOCK_FILENAME:
            return True
        first_component = remainder.split("/", 1)[0]
        if first_component.startswith(
            (_STAGING_PREFIX, _BACKUP_PREFIX, _RECOVERY_PREFIX)
        ):
            return True
    return False


def _load_chapter(
    workspace: _WorkspaceInput,
    entry: Mapping[str, Any],
    chapter_dir: Path,
) -> _ChapterInput:
    _ensure_directory(chapter_dir, "chapter directory")
    chapter_manifest_path = chapter_dir / "chapter.json"
    chapter_manifest_bytes = _read_snapshot(
        chapter_manifest_path,
        "chapter manifest",
        max_bytes=MAX_CHAPTER_MANIFEST_BYTES,
    )
    chapter_relative = _relative(workspace.root, chapter_manifest_path)
    expected_chapter_hash = workspace.manifest.files_sha256.get(chapter_relative)
    if expected_chapter_hash is None:
        raise ChapterRevisionError("chapter manifest is absent from workspace hashes")
    chapter_hash = _sha256_bytes(chapter_manifest_bytes)
    if chapter_hash != expected_chapter_hash or chapter_hash != entry["chapter_sha256"]:
        raise ChapterRevisionError("chapter manifest hash mismatch")
    try:
        payload = parse_json_document(
            chapter_manifest_bytes,
            label="chapter manifest",
            max_bytes=MAX_CHAPTER_MANIFEST_BYTES,
        )
        chapter_manifest = _ChapterManifest.model_validate(payload)
    except (StrictJsonError, ValidationError, ValueError) as exc:
        raise ChapterRevisionError("chapter manifest is invalid") from exc
    _validate_chapter_manifest(workspace, entry, chapter_manifest)

    html_path = chapter_dir / "chapter.html"
    html_bytes = _read_snapshot(html_path, "chapter HTML", max_bytes=MAX_HTML_BYTES)
    html_relative = _relative(workspace.root, html_path)
    expected_html_hash = workspace.manifest.files_sha256.get(html_relative)
    html_hash = _sha256_bytes(html_bytes)
    if (
        expected_html_hash != html_hash
        or chapter_manifest.chapter_html_sha256 != html_hash
    ):
        raise ChapterRevisionError("chapter HTML hash mismatch")
    try:
        chapter_html = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChapterRevisionError("chapter HTML is not UTF-8") from exc

    assets = frozenset(chapter_manifest.assets)
    if len(assets) != len(chapter_manifest.assets) or len(assets) > MAX_ASSET_COUNT:
        raise ChapterRevisionError("chapter asset list is invalid")
    _validate_asset_paths(assets)
    asset_bytes: list[tuple[str, bytes, str]] = []
    for relative in sorted(assets):
        path = chapter_dir / PurePosixPath(relative)
        root_relative = _relative(workspace.root, path)
        expected_hash = workspace.manifest.files_sha256.get(root_relative)
        if expected_hash is None:
            raise ChapterRevisionError(
                f"chapter asset is absent from hashes: {relative}"
            )
        data = _read_snapshot(
            path, f"chapter asset {relative}", max_bytes=MAX_ASSET_BYTES
        )
        actual_hash = _sha256_bytes(data)
        if actual_hash != expected_hash:
            raise ChapterRevisionError(f"chapter asset hash mismatch: {relative}")
        asset_bytes.append((relative, data, actual_hash))

    pages = _validate_page_paths(chapter_manifest.pages)
    if len(pages) > min(MAX_IMAGE_COUNT, MAX_IMAGES):
        raise ChapterRevisionError("chapter contains too many page images")
    page_numbers: list[int] = []
    for page in pages:
        match = _PAGE_RE.fullmatch(page)
        if match is None:
            raise ChapterRevisionError(f"invalid annotated page path: {page}")
        page_numbers.append(int(match.group(1)))
    if any(
        number < chapter_manifest.start_page_idx
        or number > chapter_manifest.end_page_idx
        for number in page_numbers
    ):
        raise ChapterRevisionError("annotated page is outside the chapter page range")
    page_bytes: list[tuple[str, bytes, str]] = []
    total_image_bytes = 0
    for relative in pages:
        path = chapter_dir / PurePosixPath(relative)
        data = _read_snapshot(
            path, f"annotated page {relative}", max_bytes=MAX_IMAGE_BYTES
        )
        total_image_bytes += len(data)
        if total_image_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ChapterRevisionError(
                "chapter page images exceed the total size limit"
            )
        root_relative = _relative(workspace.root, path)
        expected_hash = workspace.manifest.files_sha256.get(root_relative)
        digest = _sha256_bytes(data)
        if expected_hash is None or digest != expected_hash:
            raise ChapterRevisionError(f"annotated page hash mismatch: {relative}")
        page_bytes.append((relative, data, digest))

    seed = _validate_seed_html(chapter_html, assets)
    return _ChapterInput(
        workspace_dir=workspace.root,
        chapter_dir=chapter_dir,
        root_manifest=workspace.manifest,
        root_manifest_sha256=workspace.manifest_sha256,
        chapter_manifest=chapter_manifest,
        chapter_manifest_bytes=chapter_manifest_bytes,
        chapter_manifest_sha256=chapter_hash,
        chapter_html=chapter_html,
        chapter_html_sha256=html_hash,
        page_bytes=tuple(page_bytes),
        asset_bytes=tuple(asset_bytes),
        seed=seed,
    )


def is_chapter_revision_fresh(
    edit_dir: str | Path,
    *,
    agent_identity: AgentIdentity,
) -> bool:
    """Return whether every chapter has a valid current revision.

    The preflight reuses the workspace, chapter, and revision validators used
    by :func:`revise_all_chapters`. It does not construct an agent runner.
    """
    if not isinstance(agent_identity, AgentIdentity):
        raise ChapterRevisionError("a valid agent identity is required")
    workspace = _load_workspace(_resolve_edit_dir(Path(edit_dir)))
    contract_hash = _contract_sha256()
    for entry in workspace.entries:
        chapter_path = workspace.root / PurePosixPath(cast(str, entry["path"]))
        chapter = _load_chapter(workspace, entry, chapter_path)
        prompt_hash = _prompt_sha256(agent_identity, _revision_task(chapter))
        if not _fresh_output(
            chapter,
            chapter_path / "corrected.html",
            chapter_path / "revision.json",
            identity=agent_identity,
            prompt_hash=prompt_hash,
            contract_hash=contract_hash,
        ):
            return False
    return True


def _validate_chapter_manifest(
    workspace: _WorkspaceInput,
    entry: Mapping[str, Any],
    manifest: _ChapterManifest,
) -> None:
    if manifest.schema_name != WORKSPACE_SCHEMA or manifest.schema_version != 1:
        raise ChapterRevisionError("chapter manifest has an invalid schema")
    if manifest.ordinal != entry["ordinal"]:
        raise ChapterRevisionError("chapter manifest ordinal mismatch")
    for field_name, root_key in (
        ("source_content_sha256", "content_sha256"),
        ("source_content_file_sha256", "content_file_sha256"),
        ("source_chapters_sha256", "chapters_sha256"),
        ("source_pdf_sha256", "source_pdf_sha256"),
    ):
        if (
            getattr(manifest, field_name)
            != workspace.manifest.source_fingerprints[root_key]
        ):
            raise ChapterRevisionError(
                f"chapter source fingerprint mismatch: {field_name}"
            )
    for field_name in (
        "title",
        "kind",
        "start_content_idx",
        "end_content_idx",
        "start_page_idx",
        "end_page_idx",
    ):
        if getattr(manifest, field_name) != entry[field_name]:
            raise ChapterRevisionError(f"chapter entry mismatch: {field_name}")
    if manifest.start_content_idx > manifest.end_content_idx:
        raise ChapterRevisionError("chapter content range is reversed")
    if manifest.start_page_idx > manifest.end_page_idx:
        raise ChapterRevisionError("chapter page range is reversed")


def _validate_seed_html(html_text: str, assets: frozenset[str]) -> _SeedInfo:
    root = _parse_and_validate_html(html_text, assets, label="chapter HTML")
    source: dict[str, _SourceElement] = {}
    referenced_assets = {
        node.attrs["src"]
        for node in _walk_nodes(root)
        if node.tag == "img" and "src" in node.attrs
    }
    for node in _walk_nodes(root):
        raw = node.attrs.get("data-content-idx")
        if raw is None:
            if "data-content-part" in node.attrs:
                raise ChapterRevisionError(
                    "data-content-part requires data-content-idx"
                )
            continue
        tokens = _content_ids(raw)
        if len(tokens) != 1:
            raise ChapterRevisionError("seed HTML must use one content ID per element")
        content_id = tokens[0]
        if content_id in source:
            raise ChapterRevisionError(f"seed HTML duplicates content ID: {content_id}")
        source[content_id] = _SourceElement(
            content_id=content_id,
            bbox=_bbox(node.attrs.get("data-bbox")),
            removable=(
                node.tag in {"header", "footer"}
                or node.attrs.get("data-type", "").lower() in _REMOVABLE_TYPES
            ),
        )
    if not source:
        raise ChapterRevisionError("chapter HTML has no content references")
    return _SeedInfo(
        source=source,
        assets=assets,
        referenced_assets=frozenset(referenced_assets),
    )


def _validate_corrected_html(html_text: str, seed: _SeedInfo) -> None:
    if len(html_text.encode("utf-8")) > MAX_HTML_BYTES:
        raise ChapterRevisionError("corrected HTML exceeds the size limit")
    root = _parse_and_validate_html(html_text, seed.assets, label="corrected HTML")
    corrected_assets = frozenset(
        node.attrs["src"]
        for node in _walk_nodes(root)
        if node.tag == "img" and "src" in node.attrs
    )
    if corrected_assets != seed.referenced_assets:
        raise ChapterRevisionError("corrected HTML changes local asset references")
    occurrences: dict[str, list[tuple[_HTMLNode, str | None]]] = {
        content_id: [] for content_id in seed.source
    }
    source_order = {content_id: index for index, content_id in enumerate(seed.source)}
    for node in _walk_nodes(root):
        raw = node.attrs.get("data-content-idx")
        if raw is None:
            if "data-content-part" in node.attrs or "data-bbox" in node.attrs:
                raise ChapterRevisionError(
                    "unreferenced element carries source metadata"
                )
            continue
        tokens = _content_ids(raw)
        for token in tokens:
            if token not in seed.source:
                raise ChapterRevisionError(
                    f"corrected HTML invents content ID: {token}"
                )
        if len(tokens) > 1 and [source_order[token] for token in tokens] != sorted(
            source_order[token] for token in tokens
        ):
            raise ChapterRevisionError("merged content IDs are not in source order")
        part = node.attrs.get("data-content-part")
        if part is not None and len(tokens) != 1:
            raise ChapterRevisionError("merged elements cannot carry split metadata")
        for token in tokens:
            occurrences[token].append((node, part))
        _validate_output_bbox(node, tokens, seed.source)

    for content_id, source_element in seed.source.items():
        values = occurrences[content_id]
        if not values:
            if source_element.removable:
                continue
            raise ChapterRevisionError(
                f"corrected HTML removes substantive content ID: {content_id}"
            )
        parts = [part for _, part in values]
        if len(values) == 1 and parts[0] is None:
            continue
        if any(part is None for part in parts):
            raise ChapterRevisionError(
                f"content ID {content_id} has mixed split metadata"
            )
        parsed_parts = [_parse_part(cast(str, part)) for part in parts]
        totals = {total for _, total in parsed_parts}
        numbers = [number for number, _ in parsed_parts]
        if len(totals) != 1 or numbers != list(range(1, next(iter(totals)) + 1)):
            raise ChapterRevisionError(
                f"content ID {content_id} has invalid split parts"
            )

    body = _find_single(root, "body")
    visible_text = _node_text(body).strip()
    if not visible_text and not any(
        node.tag in {"img", "table"} for node in _walk_nodes(body)
    ):
        raise ChapterRevisionError("corrected HTML has no substantive visible content")


def _validate_output_bbox(
    node: _HTMLNode,
    tokens: list[str],
    source: Mapping[str, _SourceElement],
) -> None:
    bbox = _bbox(node.attrs.get("data-bbox"))
    status = node.attrs.get("data-bbox-status")
    if status is not None and status != "needs-repair":
        raise ChapterRevisionError("data-bbox-status must be needs-repair")
    if status is not None and bbox is None:
        raise ChapterRevisionError("data-bbox-status requires data-bbox")
    if bbox is None:
        if len(tokens) == 1 and source[tokens[0]].bbox is not None:
            raise ChapterRevisionError(
                f"corrected HTML dropped data-bbox for content ID {tokens[0]}"
            )
        return
    expected: list[tuple[str, str, str, str]] = []
    for token in tokens:
        original = source[token].bbox
        if original is None:
            raise ChapterRevisionError(f"corrected HTML invents a bbox for {token}")
        expected.append(original)
    if any(value != expected[0] for value in expected[1:]) or bbox != expected[0]:
        raise ChapterRevisionError("corrected HTML changes a source data-bbox")


def _parse_and_validate_html(
    html_text: str,
    assets: frozenset[str],
    *,
    label: str,
) -> _HTMLNode:
    if "\x00" in html_text:
        raise ChapterRevisionError(f"{label} contains a NUL character")
    parser = _TreeParser()
    try:
        parser.feed(html_text)
        parser.close()
    except (_HTMLParseError, ValueError) as exc:
        raise ChapterRevisionError(f"{label} is malformed HTML") from exc
    elements = [child for child in parser.root if isinstance(child, _HTMLNode)]
    if parser.doctype_count > 1 or len(elements) != 1 or elements[0].tag != "html":
        raise ChapterRevisionError(f"{label} must contain one html element")
    if any(isinstance(child, str) and child.strip() for child in parser.root):
        raise ChapterRevisionError(f"{label} has text outside html")
    root = elements[0]
    if sum(1 for _ in _walk_nodes(root)) > MAX_HTML_ELEMENTS:
        raise ChapterRevisionError(f"{label} contains too many HTML elements")
    element_counts = {
        tag: sum(1 for node in _walk_nodes(root) if node.tag == tag)
        for tag in ("html", "head", "body")
    }
    if element_counts != {"html": 1, "head": 1, "body": 1}:
        raise ChapterRevisionError(f"{label} must contain one html, head, and body")
    html_children = [child for child in root.children if isinstance(child, _HTMLNode)]
    if len(html_children) != 2 or [child.tag for child in html_children] != [
        "head",
        "body",
    ]:
        raise ChapterRevisionError(
            f"{label} must contain one head followed by one body"
        )
    if any(isinstance(child, str) and child.strip() for child in root.children):
        raise ChapterRevisionError(f"{label} has text directly under html")
    title_nodes = [node for node in _walk_nodes(root) if node.tag == "title"]
    if len(title_nodes) != 1 or not _node_text(title_nodes[0]).strip():
        raise ChapterRevisionError(f"{label} must contain one non-empty title")

    ids: set[str] = set()
    for node in _walk_nodes(root):
        _validate_node_attributes(node, assets, ids, label)
    head = html_children[0]
    if any(
        isinstance(child, _HTMLNode) and child.tag not in {"title", "meta"}
        for child in head.children
    ):
        raise ChapterRevisionError(f"{label} has non-head content in head")
    for node in _walk_nodes(root):
        if node.tag in {"table", "ul", "ol"}:
            _validate_container(node, label)
    return root


def _validate_node_attributes(
    node: _HTMLNode,
    assets: frozenset[str],
    ids: set[str],
    label: str,
) -> None:
    if node.tag not in _ALLOWED_TAGS:
        raise ChapterRevisionError(f"{label} uses a forbidden HTML tag: {node.tag}")
    allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(node.tag, frozenset())
    for name, value in node.attrs.items():
        if name not in allowed:
            raise ChapterRevisionError(f"{label} uses forbidden attribute: {name}")
        lowered_value = value.strip().lower()
        if (
            "\x00" in value
            or lowered_value.startswith(("//", "javascript:", "vbscript:", "data:"))
            or "://" in lowered_value
        ):
            raise ChapterRevisionError(f"{label} contains an unsafe URL or attribute")
        if name.startswith("on") or name in {"action", "formaction", "target"}:
            raise ChapterRevisionError(f"{label} uses an active attribute")
    if "id" in node.attrs:
        identifier = node.attrs["id"]
        if not _ID_RE.fullmatch(identifier) or identifier in ids:
            raise ChapterRevisionError(f"{label} has a duplicate or invalid element id")
        ids.add(identifier)
    if "data-content-idx" in node.attrs:
        _content_ids(node.attrs["data-content-idx"])
    if "data-content-part" in node.attrs:
        if not _PART_RE.fullmatch(node.attrs["data-content-part"]):
            raise ChapterRevisionError(f"{label} has invalid data-content-part")
    if "data-page-idx" in node.attrs:
        if any(
            not _NUMBER_RE.fullmatch(value)
            for value in node.attrs["data-page-idx"].split()
        ):
            raise ChapterRevisionError(f"{label} has invalid data-page-idx")
    if "data-bbox" in node.attrs:
        _bbox(node.attrs["data-bbox"])
    if "data-bbox-status" in node.attrs:
        if node.attrs["data-bbox-status"] != "needs-repair":
            raise ChapterRevisionError(f"{label} has invalid data-bbox-status")
        if "data-bbox" not in node.attrs:
            raise ChapterRevisionError(f"{label} has a bbox status without data-bbox")
    if "data-uncertain" in node.attrs:
        if node.tag != "span" or node.attrs["data-uncertain"] != "true":
            raise ChapterRevisionError(f"{label} has invalid uncertainty marker")
    if node.tag == "img":
        src = node.attrs.get("src")
        if src is None or src not in assets or "alt" not in node.attrs:
            raise ChapterRevisionError(f"{label} has an invalid local image reference")
    if node.tag in _VOID_TAGS and node.children:
        raise ChapterRevisionError(f"{label} has children under a void element")
    if node.tag in {"th", "td"}:
        for attr in ("colspan", "rowspan"):
            if attr in node.attrs:
                value = node.attrs[attr]
                if not _NUMBER_RE.fullmatch(value) or int(value) < 1:
                    raise ChapterRevisionError(f"{label} has invalid {attr}")
    for attr in ("width", "height"):
        if attr in node.attrs and not _DIMENSION_RE.fullmatch(node.attrs[attr]):
            raise ChapterRevisionError(f"{label} has invalid {attr}")
    if "span" in node.attrs and (
        not _NUMBER_RE.fullmatch(node.attrs["span"]) or int(node.attrs["span"]) < 1
    ):
        raise ChapterRevisionError(f"{label} has invalid span")


def _validate_container(node: _HTMLNode, label: str) -> None:
    if node.tag == "table":
        rows: list[_HTMLNode] = []
        caption_count = 0
        for child in _element_children(node):
            if child.tag == "caption":
                caption_count += 1
                if caption_count > 1:
                    raise ChapterRevisionError(f"{label} has multiple table captions")
                continue
            if child.tag == "tr":
                rows.append(child)
            elif child.tag == "colgroup":
                columns = _element_children(child)
                if not columns or any(column.tag != "col" for column in columns):
                    raise ChapterRevisionError(f"{label} has invalid table columns")
            elif child.tag in {"thead", "tbody", "tfoot"}:
                for group_child in _element_children(child):
                    if group_child.tag != "tr":
                        raise ChapterRevisionError(
                            f"{label} has invalid table row grouping"
                        )
                    rows.append(group_child)
            else:
                raise ChapterRevisionError(f"{label} has invalid direct table content")
        if not rows:
            raise ChapterRevisionError(f"{label} has no table rows")
        for row in rows:
            cells = _element_children(row)
            if not cells or any(cell.tag not in {"th", "td"} for cell in cells):
                raise ChapterRevisionError(f"{label} has an invalid table row")
            for cell in cells:
                if any(
                    descendant.tag in {"table", "tr", "thead", "tbody", "tfoot"}
                    for descendant in _walk_nodes(cell)
                    if descendant is not cell
                ):
                    raise ChapterRevisionError(f"{label} nests a table inside a cell")
    else:
        children = _element_children(node)
        if not children or any(child.tag != "li" for child in children):
            raise ChapterRevisionError(f"{label} has an invalid list")


def _content_ids(raw: str) -> list[str]:
    tokens = raw.split()
    if not tokens or any(not _CONTENT_ID_RE.fullmatch(token) for token in tokens):
        raise ChapterRevisionError("data-content-idx contains an invalid content ID")
    if len(tokens) != len(set(tokens)):
        raise ChapterRevisionError("data-content-idx repeats a content ID")
    return tokens


def _bbox(raw: str | None) -> tuple[str, str, str, str] | None:
    if raw is None:
        return None
    values = [part.strip() for part in raw.split(",")]
    if len(values) != 4 or any(not part for part in values):
        raise ChapterRevisionError("data-bbox must contain four coordinates")
    normalized: list[str] = []
    try:
        for value in values:
            number = float(value)
            if not (number >= 0 and number < float("inf")):
                raise ValueError
            normalized.append(format(number, ".15g"))
    except ValueError as exc:
        raise ChapterRevisionError("data-bbox contains an invalid coordinate") from exc
    return cast(tuple[str, str, str, str], tuple(normalized))


def _parse_part(raw: str) -> tuple[int, int]:
    match = _PART_RE.fullmatch(raw)
    if match is None:
        raise ChapterRevisionError("invalid data-content-part")
    return int(match.group(1)), int(match.group(2))


def _walk_nodes(node: _HTMLNode) -> Iterator[_HTMLNode]:
    yield node
    for child in node.children:
        if isinstance(child, _HTMLNode):
            yield from _walk_nodes(child)


def _element_children(node: _HTMLNode) -> list[_HTMLNode]:
    return [child for child in node.children if isinstance(child, _HTMLNode)]


def _node_text(node: _HTMLNode) -> str:
    chunks: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            chunks.append(child)
        else:
            chunks.append(_node_text(child))
    return " ".join(chunks)


def _find_single(node: _HTMLNode, tag: str) -> _HTMLNode:
    found = [child for child in _walk_nodes(node) if child.tag == tag]
    if len(found) != 1:
        raise ChapterRevisionError(f"expected one {tag} element")
    return found[0]


def _validate_asset_paths(assets: frozenset[str]) -> None:
    for relative in assets:
        _validate_relative_path(relative, "chapter asset")
        if not relative.startswith("assets/") or PurePosixPath(relative).name == "":
            raise ChapterRevisionError(f"invalid chapter asset path: {relative}")


def _validate_page_paths(pages: list[str]) -> list[str]:
    if len(pages) != len(set(pages)):
        raise ChapterRevisionError("chapter page list contains duplicates")
    numbers: list[int] = []
    for relative in pages:
        match = _PAGE_RE.fullmatch(relative)
        if match is None:
            raise ChapterRevisionError(f"invalid annotated page path: {relative}")
        numbers.append(int(match.group(1)))
    if numbers != sorted(numbers):
        raise ChapterRevisionError("annotated pages are not in page order")
    return pages


def _entry_for_chapter(workspace: _WorkspaceInput, chapter_dir: Path) -> dict[str, Any]:
    chapter_dir = _absolute_path(chapter_dir)
    for entry in workspace.entries:
        candidate = workspace.root / PurePosixPath(cast(str, entry["path"]))
        if candidate == chapter_dir:
            return entry
    raise ChapterRevisionError(
        "chapter directory is not listed in the workspace manifest"
    )


def _workspace_for_chapter(chapter_dir: Path) -> Path:
    if (
        chapter_dir.parent.name != "chapters"
        or chapter_dir.parent.parent.name != "04_edit"
    ):
        raise ChapterRevisionError("chapter directory must be under 04_edit/chapters")
    return chapter_dir.parent.parent


def _resolve_edit_dir(path: Path) -> Path:
    candidate = _absolute_path(path)
    if candidate.name == "04_edit":
        return candidate
    return candidate / "04_edit"


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _relative(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ChapterRevisionError("workspace path escaped its root") from exc
    return relative.as_posix()


def _validate_relative_path(relative: str, label: str) -> None:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
    ):
        raise ChapterRevisionError(f"{label} is unsafe: {relative!r}")


def _ensure_directory(path: Path, label: str) -> None:
    fd = _open_path(path, label, directory=True)
    os.close(fd)


def _open_path(path: Path, label: str, *, directory: bool) -> int:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise ChapterRevisionError(
            f"cannot safely open {label}: POSIX O_NOFOLLOW is unavailable"
        )
    absolute = _absolute_path(path)
    components = absolute.parts[1:]
    if not components or any(part in {"", ".", ".."} for part in components):
        raise ChapterRevisionError(f"cannot safely open {label}: unsafe path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        directory_fd = os.open("/", flags)
    except OSError as exc:
        raise ChapterRevisionError(f"cannot safely open {label}") from exc
    fd: int | None = None
    try:
        for component in components[:-1] if directory else components[:-1]:
            try:
                next_fd = os.open(
                    component,
                    flags,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise ChapterRevisionError(f"cannot safely open {label}") from exc
            os.close(directory_fd)
            directory_fd = next_fd
        final_flags = flags if directory else os.O_RDONLY | os.O_NOFOLLOW
        try:
            fd = os.open(components[-1], final_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ChapterRevisionError(f"cannot safely open {label}") from exc
        mode = os.fstat(fd).st_mode
        if directory and not stat.S_ISDIR(mode):
            raise ChapterRevisionError(f"{label} is not a directory")
        if not directory and not stat.S_ISREG(mode):
            raise ChapterRevisionError(f"{label} is not a regular file")
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        os.close(directory_fd)


def _read_snapshot(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    require_single_link: bool = False,
) -> bytes:
    fd = _open_path(path, label, directory=False)
    try:
        before = os.fstat(fd)
        if require_single_link and before.st_nlink != 1:
            raise ChapterRevisionError(f"{label} must have exactly one hard link")
        if before.st_size > max_bytes:
            raise ChapterRevisionError(f"{label} exceeds the size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ChapterRevisionError(f"{label} exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (
            not _same_snapshot(before, after)
            or (require_single_link and after.st_nlink != 1)
            or total != before.st_size
        ):
            raise ChapterRevisionError(f"{label} changed while being read")
        return b"".join(chunks)
    except OSError as exc:
        raise ChapterRevisionError(f"cannot read {label}") from exc
    finally:
        os.close(fd)


def _hash_snapshot(path: Path, label: str, *, max_bytes: int) -> str:
    fd = _open_path(path, label, directory=False)
    try:
        before = os.fstat(fd)
        if before.st_size > max_bytes:
            raise ChapterRevisionError(f"{label} exceeds the size limit")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ChapterRevisionError(f"{label} exceeds the size limit")
            digest.update(chunk)
        after = os.fstat(fd)
        if not _same_snapshot(before, after) or total != before.st_size:
            raise ChapterRevisionError(f"{label} changed while being read")
        return digest.hexdigest()
    except OSError as exc:
        raise ChapterRevisionError(f"cannot read {label}") from exc
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


def _list_files(root: Path) -> set[str]:
    files: set[str] = set()
    directories: list[tuple[Path, int]] = [(root, 0)]
    while directories:
        current, depth = directories.pop()
        if depth > MAX_WORKSPACE_DEPTH:
            raise ChapterRevisionError("workspace directory depth exceeds the limit")
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise ChapterRevisionError(
                f"cannot enumerate workspace: {current}"
            ) from exc
        for entry in entries:
            relative = _relative(root, Path(entry.path))
            _validate_relative_path(relative, "workspace path")
            if entry.is_symlink():
                raise ChapterRevisionError(f"workspace contains a symlink: {relative}")
            if entry.is_dir(follow_symlinks=False):
                directories.append((Path(entry.path), depth + 1))
            elif entry.is_file(follow_symlinks=False):
                files.add(relative)
                if len(files) > MAX_WORKSPACE_FILES:
                    raise ChapterRevisionError("workspace contains too many files")
            else:
                raise ChapterRevisionError(
                    f"workspace contains a non-regular entry: {relative}"
                )
    return files


def _publish_pair(chapter_dir: Path, corrected: bytes, revision: bytes) -> None:
    targets = (chapter_dir / "corrected.html", chapter_dir / "revision.json")
    for target in targets:
        try:
            if target.is_symlink():
                raise ChapterRevisionError(f"output is a symlink: {target}")
        except OSError as exc:
            raise ChapterRevisionError(f"cannot inspect output: {target}") from exc
    for target in targets:
        _target_exists(target)

    staging_dir: Path | None = None
    backup_dir: Path | None = None
    backed_up: list[Path] = []
    installed: list[Path] = []
    try:
        staging_dir = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=chapter_dir))
        for target, data in zip(targets, (corrected, revision), strict=True):
            _write_fsync(staging_dir / target.name, data)
        _fsync_directory(staging_dir)

        backup_dir = Path(tempfile.mkdtemp(prefix=_BACKUP_PREFIX, dir=chapter_dir))
        for target in targets:
            if _target_exists(target):
                os.replace(target, backup_dir / target.name)
                backed_up.append(target)

        for target in targets:
            os.replace(staging_dir / target.name, target)
            installed.append(target)
        _fsync_directory(chapter_dir)
    except BaseException as publication_error:
        rollback_errors: list[BaseException] = []
        for target in reversed(installed):
            try:
                os.unlink(target)
            except BaseException as exc:
                rollback_errors.append(exc)
        for target in backed_up:
            backup = (backup_dir / target.name) if backup_dir is not None else None
            if backup is None or not backup.exists():
                rollback_errors.append(
                    ChapterRevisionError(f"missing backup for {target.name}")
                )
                continue
            try:
                os.replace(backup, target)
            except BaseException as exc:
                rollback_errors.append(exc)
        try:
            _fsync_directory(chapter_dir)
        except BaseException as exc:
            rollback_errors.append(exc)

        if rollback_errors:
            evidence = tuple(
                path
                for path in (staging_dir, backup_dir)
                if path is not None and path.exists()
            )
            raise ChapterRevisionPublicationError(
                "chapter publication failed and rollback failed; inspect recovery evidence",
                evidence=evidence,
            ) from publication_error
        _cleanup_published_path(staging_dir, "failed revision staging")
        _cleanup_published_path(backup_dir, "failed revision backup")
        raise ChapterRevisionError(
            "chapter publication failed; the previous output pair was restored"
        ) from publication_error
    else:
        _cleanup_published_path(staging_dir, "revision staging")
        _cleanup_published_path(backup_dir, "revision backup")


def _target_exists(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ChapterRevisionError(
            f"cannot inspect publication target: {path}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ChapterRevisionError(f"publication target is a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ChapterRevisionError(f"publication target is not a regular file: {path}")
    if info.st_nlink != 1:
        raise ChapterRevisionError(
            f"publication target must have exactly one hard link: {path}"
        )
    return True


def _write_fsync(path: Path, data: bytes) -> None:
    fd: int | None = None
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd is not None:
            os.close(fd)


def _cleanup_published_path(path: Path | None, label: str) -> None:
    if path is None or not path.exists():
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        _log.warning("could not clean %s %s: %s", label, path, exc)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ChapterRevisionError("cannot fsync chapter directory") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise ChapterRevisionError("cannot fsync chapter directory") from exc
    finally:
        os.close(fd)


def _resolve_agent_identity(agent_runner: AgentRunner) -> AgentIdentity:
    identity = getattr(agent_runner, "identity", None)
    if not isinstance(identity, AgentIdentity):
        raise ChapterRevisionError("agent_runner.identity must be AgentIdentity")
    return identity


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


__all__ = [
    "ChapterRevisionError",
    "ChapterRevisionFailure",
    "ChapterRevisionPublicationError",
    "ChapterRevisionRecord",
    "ChapterRevisionReport",
    "is_chapter_revision_fresh",
    "MAX_HTML_BYTES",
    "MAX_HTML_ELEMENTS",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_COUNT",
    "MAX_TOTAL_IMAGE_BYTES",
    "REVISION_TASK_PREFIX",
    "revise_all_chapters",
    "revise_chapter",
]
