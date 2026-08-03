"""Tests for normalized-content chapter boundary detection."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

import pytest
from pydantic import ConfigDict, Field, RootModel, ValidationError
from typing import Annotated

from epubforge import chapter_segmentation as segmentation
from epubforge.agent_runner import (
    AgentIdentity,
    AgentRunRequest,
    AgentRunResult,
    book_editor_identity,
)
from epubforge.chapter_segmentation import (
    ChapterSegmentationError,
    ChapterSegmentationPublicationError,
    ChapterSegmentationResponse,
    SegmentationFailure,
    SegmentationSuccess,
    segment_chapters,
    validate_boundaries,
    validate_coverage_acknowledgement,
)


class FakeAgentRunner:
    def __init__(self, response: Any) -> None:
        self.identity = book_editor_identity()
        self.response = response
        self.calls: list[AgentRunRequest] = []

    def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        self.calls.append(request)
        return AgentRunResult(
            outputs={"boundaries.json": json.dumps(self.response).encode("utf-8")},
            session_id="ses_segmentation_test",
        )


def _items() -> list[dict[str, Any]]:
    return [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "text",
            "text_level": 1,
            "text": "Contents",
        },
        {
            "content_idx": 1,
            "page_idx": 0,
            "type": "text",
            "text_level": 1,
            "text": "Chapter One",
        },
        {
            "content_idx": 2,
            "page_idx": 0,
            "type": "text",
            "text_level": 1,
            "text": "Chapter Two",
        },
        {
            "content_idx": 3,
            "page_idx": 1,
            "type": "text",
            "text_level": 0,
            "text": "A quotation that resembles a heading",
        },
        {
            "content_idx": 4,
            "page_idx": 1,
            "type": "text",
            "text_level": 1,
            "text": "An Embedded Article",
        },
        {
            "content_idx": 5,
            "page_idx": 2,
            "type": "text",
            "text_level": 1,
            "text": "Chapter One",
        },
        {
            "content_idx": 6,
            "page_idx": 2,
            "type": "text",
            "text_level": 0,
            "text": "The first body paragraph.",
        },
        {
            "content_idx": 7,
            "page_idx": 4,
            "type": "text",
            "text_level": 1,
            "text": "Chapter Two",
        },
        {
            "content_idx": 8,
            "page_idx": 4,
            "type": "text",
            "text_level": 0,
            "text": "The second body paragraph.",
        },
        {
            "content_idx": 9,
            "page_idx": 6,
            "type": "text",
            "text_level": 1,
            "text": "Notes",
        },
        {
            "content_idx": 10,
            "page_idx": 7,
            "type": "image",
            "text": "",
            "img_path": "source.jpg",
        },
    ]


def _items_sha256(items: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _projected_sha256(projected: list[dict[str, Any]]) -> str:
    """Canonical digest of projected items, computed independently of the
    implementation so coverage assertions are not circular."""
    return hashlib.sha256(
        json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _coverage_for(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The exact coverage acknowledgement matching *items* after projection."""
    projected = segmentation._project_content(items)
    return {
        "item_count": len(projected),
        "first_content_idx": projected[0]["content_idx"],
        "last_content_idx": projected[-1]["content_idx"],
        "first_page_idx": projected[0]["page_idx"],
        "last_page_idx": projected[-1]["page_idx"],
        "items_sha256": _projected_sha256(projected),
    }


def _content_sha256(items: list[dict[str, Any]], geometry: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"items": items, "page_geometry": geometry},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_content(tmp_path: Path) -> Path:
    content_path = tmp_path / "02_content" / "content.json"
    content_path.parent.mkdir()
    items = _items()
    items_sha256 = _items_sha256(items)
    page_geometry = [
        {"page_idx": index, "width": 100.0, "height": 120.0} for index in range(8)
    ]
    content_path.write_text(
        json.dumps(
            {
                "schema": "epubforge.mineru-content",
                "schema_version": 2,
                "source_archive_sha256": "a" * 64,
                "source_archive_size": 1,
                "source_kind": "direct",
                "segment_count": 1,
                "page_count": 8,
                "source_pdf_sha256": "a" * 64,
                "items_sha256": items_sha256,
                "normalization": {"contract_version": 2},
                "assets": {},
                "page_geometry": page_geometry,
                "items": items,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return content_path


def _success_response() -> dict[str, Any]:
    return {
        "status": "success",
        "coverage": _coverage_for(_items()),
        "boundaries": [
            {
                "title": "Contents",
                "kind": "frontmatter",
                "start_content_idx": 0,
                "start_page_idx": 0,
                "confidence": 0.9,
                "evidence": "Opening contents section precedes the body.",
            },
            {
                "title": "Chapter One",
                "kind": "chapter",
                "start_content_idx": 5,
                "start_page_idx": 2,
                "confidence": 0.98,
                "evidence": "Heading is followed by body text on the same page.",
            },
            {
                "title": "Chapter Two",
                "kind": "chapter",
                "start_content_idx": 7,
                "start_page_idx": 4,
                "confidence": 0.97,
                "evidence": "Repeated heading pattern starts the next body section.",
            },
            {
                "title": "Notes",
                "kind": "backmatter",
                "start_content_idx": 9,
                "start_page_idx": 6,
                "confidence": 0.88,
                "evidence": "Notes follows the final chapter.",
            },
        ],
    }


def _failure_response(
    code: str, details: str = "Read reported a truncated projection line."
) -> dict[str, Any]:
    return {"status": "failure", "code": code, "details": details}


def test_success_filters_toc_and_embedded_heading_traps(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())

    output_path = segment_chapters(content_path, tmp_path, fake)

    assert output_path == tmp_path / "03_chapters" / "chapters.json"
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert [b["start_content_idx"] for b in artifact["boundaries"]] == [0, 5, 7, 9]
    assert artifact["source_content_sha256"]
    assert artifact["agent_name"] == "book-editor"
    assert artifact["agent_model"] == "openai/gpt-5.6-luna"
    assert artifact["agent_variant"] == "medium"
    assert artifact["session_id"] == "ses_segmentation_test"
    assert len(fake.calls) == 1
    request = fake.calls[0]
    task = request.files["TASK.md"].decode("utf-8").lower()
    projection = request.files["content-projection.json"].decode("utf-8").lower()
    assert "dense table-of-contents title lists are not body starts" in task
    assert "embedded document or article titles" in task
    assert "surrounding content" in task
    assert '"content_idx": 10' in projection
    assert "an embedded article" in projection
    for forbidden in ("pdf", "render-page", "source.jpg", "image inspection"):
        assert forbidden not in projection
    payload = json.loads(request.files["content-projection.json"])
    assert payload["coverage"] == _coverage_for(_items())
    assert payload["coverage"]["item_count"] == len(_items())


def test_fresh_skip_model_prompt_and_force(tmp_path: Path, monkeypatch) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    segment_chapters(content_path, tmp_path, fake)
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 1

    original = fake.identity
    fake.identity = AgentIdentity(
        name=original.name,
        model="another-model",
        variant=original.variant,
        prompt_sha256=original.prompt_sha256,
        fingerprint="b" * 64,
    )
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 2

    monkeypatch.setattr(
        segmentation,
        "SEGMENTATION_TASK",
        segmentation.SEGMENTATION_TASK + "\nChanged.\n",
    )
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 3

    segment_chapters(content_path, tmp_path, fake, force=True)
    assert len(fake.calls) == 4


def test_public_function_runs_semantic_validator_before_publish(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    invalid = _success_response()
    invalid["boundaries"][1]["title"] = "wrong title"
    fake = FakeAgentRunner(invalid)

    with pytest.raises(ChapterSegmentationError, match="exactly match"):
        segment_chapters(content_path, tmp_path, fake)

    assert len(fake.calls) == 1
    assert not (tmp_path / "03_chapters" / "chapters.json").exists()


def test_failed_call_preserves_old_output_and_damaged_output_rebuilds(
    tmp_path: Path,
) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    output_path = segment_chapters(content_path, tmp_path, fake)
    old_bytes = output_path.read_bytes()

    class FailingAgent(FakeAgentRunner):
        def __call__(self, request: AgentRunRequest) -> AgentRunResult:
            raise RuntimeError("provider unavailable")

    with pytest.raises(ChapterSegmentationError, match="provider unavailable"):
        segment_chapters(
            content_path, tmp_path, FailingAgent(_success_response()), force=True
        )
    assert output_path.read_bytes() == old_bytes

    damaged = json.loads(old_bytes)
    damaged["boundaries"][1]["title"] = "tampered"
    output_path.write_text(json.dumps(damaged), encoding="utf-8")
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 2
    assert (
        json.loads(output_path.read_text(encoding="utf-8"))["boundaries"][1]["title"]
        == "Chapter One"
    )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("unknown_idx", "unknown content_idx"),
        ("wrong_page", "page_idx does not match"),
        ("wrong_title", "title must exactly match"),
        ("non_text", "non-empty textual"),
        ("duplicate", "duplicates content_idx"),
        ("backward_page", "cannot go backward"),
        ("front_after_chapter", "Frontmatter"),
        ("back_before_chapter", "preceding chapter"),
        ("chapter_after_back", "follow backmatter"),
        ("no_chapter", "At least one chapter"),
    ],
)
def test_boundary_validation_failures(change: str, match: str) -> None:
    source = _items()
    response = _success_response()["boundaries"]
    if change == "unknown_idx":
        response[1]["start_content_idx"] = 99
    elif change == "wrong_page":
        response[1]["start_page_idx"] = 3
    elif change == "wrong_title":
        response[1]["title"] = "Not Chapter One"
    elif change == "non_text":
        response[1]["start_content_idx"] = 10
        response[1]["start_page_idx"] = 7
        response[1]["title"] = "source.jpg"
    elif change == "duplicate":
        response[2]["start_content_idx"] = 5
    elif change == "backward_page":
        response[2]["start_page_idx"] = 1
    elif change == "front_after_chapter":
        response[2]["kind"] = "frontmatter"
    elif change == "back_before_chapter":
        response[1]["kind"] = "backmatter"
    elif change == "chapter_after_back":
        response[2]["kind"] = "backmatter"
        response[3]["kind"] = "chapter"
    elif change == "no_chapter":
        for boundary in response:
            boundary["kind"] = "frontmatter"

    parsed = ChapterSegmentationResponse.model_validate(
        {
            "status": "success",
            "boundaries": response,
            "coverage": _coverage_for(source),
        }
    )
    assert isinstance(parsed.root, SegmentationSuccess)
    with pytest.raises(ChapterSegmentationError, match=match):
        validate_boundaries(parsed.root.boundaries, source)


def test_strict_response_and_confidence_range() -> None:
    coverage = _coverage_for(_items())
    with pytest.raises(ValidationError):
        ChapterSegmentationResponse.model_validate(
            {
                "status": "success",
                "coverage": coverage,
                "boundaries": [{**_success_response()["boundaries"][1], "extra": True}],
            }
        )
    with pytest.raises(ValidationError):
        ChapterSegmentationResponse.model_validate(
            {
                "status": "success",
                "coverage": coverage,
                "boundaries": [
                    {**_success_response()["boundaries"][1], "confidence": 1.1}
                ],
            }
        )


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "schema_version",
        "source_archive_sha256",
        "source_pdf_sha256",
        "items_sha256",
    ],
)
def test_rejects_tampered_content_contract(tmp_path: Path, field: str) -> None:
    content_path = _write_content(tmp_path)
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    if field == "schema":
        payload[field] = "other.schema"
    elif field == "schema_version":
        payload[field] = 99
    elif field in {"source_archive_sha256", "source_pdf_sha256"}:
        payload[field] = "bad-hash"
    else:
        payload[field] = "b" * 64
    content_path.write_text(json.dumps(payload), encoding="utf-8")

    fake = FakeAgentRunner(_success_response())
    with pytest.raises(ChapterSegmentationError):
        segment_chapters(content_path, tmp_path, fake)
    assert not fake.calls


def test_rejects_missing_source_pdf_sha256(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    payload["source_pdf_sha256"] = None
    content_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChapterSegmentationError):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))


def test_rejects_duplicate_keys_and_unordered_items(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    valid = content_path.read_text(encoding="utf-8")
    duplicate = valid.replace(
        '"schema": "epubforge.mineru-content"',
        '"schema": "epubforge.mineru-content", "schema": "duplicate"',
        1,
    )
    content_path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ChapterSegmentationError, match="Cannot read"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))

    payload = json.loads(valid)
    payload["items"][1], payload["items"][2] = (
        payload["items"][2],
        payload["items"][1],
    )
    payload["items_sha256"] = hashlib.sha256(
        json.dumps(
            payload["items"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    content_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ChapterSegmentationError, match="contiguous ordered"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))


@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
def test_rejects_item_page_outside_page_count_and_non_finite_json(
    tmp_path: Path, non_finite: str
) -> None:
    content_path = _write_content(tmp_path)
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    payload["items"][0]["page_idx"] = payload["page_count"]
    payload["items_sha256"] = _items_sha256(payload["items"])
    content_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ChapterSegmentationError, match="outside page_count"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))

    payload = json.loads(content_path.read_text(encoding="utf-8"))
    payload["items"] = _items()
    payload["items_sha256"] = _items_sha256(payload["items"])
    raw = json.dumps(payload).replace('"text": "Contents"', f'"text": {non_finite}', 1)
    content_path.write_text(raw, encoding="utf-8")
    with pytest.raises(ChapterSegmentationError, match="Cannot read"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))


def test_projection_contract_change_invalidates_fresh_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    segment_chapters(content_path, tmp_path, fake)

    monkeypatch.setattr(
        segmentation,
        "CONTENT_PROJECTION_CONTRACT",
        (5, ("content_idx", "page_idx", "type", "text_level", "text_chunks")),
    )
    segment_chapters(content_path, tmp_path, fake)

    assert len(fake.calls) == 2


def test_page_geometry_change_invalidates_fresh_artifact(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    segment_chapters(content_path, tmp_path, fake)
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    payload["page_geometry"][0]["width"] = 101.0
    content_path.write_text(json.dumps(payload), encoding="utf-8")

    segment_chapters(content_path, tmp_path, fake)

    assert len(fake.calls) == 2


def test_page_geometry_hash_canonicalizes_integer_and_float_dimensions() -> None:
    items = [{"content_idx": 0, "page_idx": 0, "type": "text", "text": "x"}]
    integer_geometry = [{"page_idx": 0, "width": 100, "height": 120}]
    float_geometry = [{"page_idx": 0, "width": 100.0, "height": 120.0}]

    assert segmentation._content_source_sha256(items, integer_geometry) == (
        segmentation._content_source_sha256(items, float_geometry)
    )


def test_symlink_output_is_not_reused(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    output_path = segment_chapters(content_path, tmp_path, fake)
    target_path = tmp_path / "existing-chapters.json"
    target_path.write_bytes(output_path.read_bytes())
    output_path.unlink()
    output_path.symlink_to(target_path)

    segment_chapters(content_path, tmp_path, fake)

    assert len(fake.calls) == 2
    assert not output_path.is_symlink()
    assert target_path.exists()


def test_atomic_publish_failure_keeps_existing_file(
    tmp_path: Path, monkeypatch
) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    output_path = segment_chapters(content_path, tmp_path, fake)
    old_bytes = output_path.read_bytes()

    original_replace = segmentation.os.replace

    def fail_replace(source, destination):
        if Path(destination) == output_path:
            raise OSError("replace failed")
        original_replace(source, destination)

    monkeypatch.setattr(segmentation.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        segment_chapters(content_path, tmp_path, fake, force=True)
    assert output_path.read_bytes() == old_bytes


def test_parent_fsync_failure_rolls_back_previous_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    output_path = segment_chapters(content_path, tmp_path, fake)
    old_bytes = output_path.read_bytes()
    real_fsync = segmentation.os.fsync
    failed = False

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(segmentation.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(OSError, match="directory fsync failed"):
        segment_chapters(content_path, tmp_path, fake, force=True)

    assert output_path.read_bytes() == old_bytes
    assert output_path.stat().st_nlink == 1


def test_parent_fsync_failure_without_previous_artifact_removes_new_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_path = _write_content(tmp_path)
    real_fsync = segmentation.os.fsync
    failed = False

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(segmentation.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(OSError, match="directory fsync failed"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))

    assert not (tmp_path / "03_chapters" / "chapters.json").exists()


def test_failed_rollback_fsync_preserves_recovery_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    output_path = segment_chapters(content_path, tmp_path, fake)
    old_bytes = output_path.read_bytes()
    real_fsync = segmentation.os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(segmentation.os, "fsync", fail_directory_fsync)
    with pytest.raises(ChapterSegmentationPublicationError) as caught:
        segment_chapters(content_path, tmp_path, fake, force=True)

    assert caught.value.evidence
    assert all(path.exists() for path in caught.value.evidence)
    assert output_path.read_bytes() == old_bytes


def test_hardlinked_artifact_is_not_reused(
    tmp_path: Path,
) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    output_path = segment_chapters(content_path, tmp_path, fake)
    backing_path = tmp_path / "chapters-backup.json"
    os.link(output_path, backing_path)

    segment_chapters(content_path, tmp_path, fake)

    assert len(fake.calls) == 2
    assert output_path.stat().st_nlink == 1
    assert backing_path.stat().st_nlink == 1


@pytest.mark.parametrize("parent_kind", ["symlink", "file", "fifo"])
def test_rejects_unsafe_stage3_output_parent(tmp_path: Path, parent_kind: str) -> None:
    content_path = _write_content(tmp_path)
    unsafe_parent = tmp_path / f"unsafe-{parent_kind}"
    if parent_kind == "symlink":
        target = tmp_path / "real-parent"
        target.mkdir()
        unsafe_parent.symlink_to(target, target_is_directory=True)
    elif parent_kind == "file":
        unsafe_parent.write_bytes(b"not a directory")
    else:
        import os

        os.mkfifo(unsafe_parent)

    with pytest.raises(ChapterSegmentationError, match="output parent"):
        segment_chapters(
            content_path, unsafe_parent, FakeAgentRunner(_success_response())
        )


# ---------------------------------------------------------------------------
# Projection format regression tests
# ---------------------------------------------------------------------------


def _projection_for(items: list[dict[str, Any]]) -> bytes:
    """Serialize *items* exactly as the stage-3 runner writes the projection."""
    return segmentation._projection_bytes(items)


def _assert_lines_within_char_budget(projection: bytes) -> None:
    """Every physical line must stay at or below ``_MAX_LINE_CHARS`` chars."""
    lines = projection.decode("utf-8").split("\n")
    assert len(lines) > 2  # more than just schema + newline
    for i, line in enumerate(lines):
        assert len(line) <= segmentation._MAX_LINE_CHARS, (
            f"line {i} too long: {len(line)} > {segmentation._MAX_LINE_CHARS}"
        )


def _assert_lines_within_byte_budget(projection: bytes) -> None:
    """Every physical line must stay under ``_MAX_LINE_BYTES`` bytes."""
    for i, line in enumerate(projection.split(b"\n")):
        assert len(line) < segmentation._MAX_LINE_BYTES, (
            f"line {i} too long: {len(line)} bytes >= {segmentation._MAX_LINE_BYTES}"
        )


def _assert_chunk_lines_within_budget(chunks: list[str]) -> None:
    """Each chunk's pretty-printed JSON line must fit the character budget."""
    for chunk in chunks:
        encoded_text_len = len(json.dumps(chunk, ensure_ascii=False)) - 2
        line_len = segmentation._LINE_OVERHEAD + encoded_text_len
        assert line_len <= segmentation._MAX_LINE_CHARS, (
            f"chunk produces line of {line_len} chars, "
            f"exceeds {segmentation._MAX_LINE_CHARS}"
        )


def _assert_round_trip(projection: bytes, items: list[dict[str, Any]]) -> None:
    """The parsed projection must reconstruct every item losslessly.

    ``text_chunks`` concatenation equals the exact source text, scalar
    metadata survives, and the payload carries the projection schema.
    """
    payload = json.loads(projection.decode("utf-8"))
    assert payload["schema"] == segmentation.CONTENT_PROJECTION_SCHEMA
    assert payload["schema_version"] == segmentation.CONTENT_PROJECTION_SCHEMA_VERSION
    assert len(payload["items"]) == len(items)
    assert payload["coverage"] == _coverage_for(items)
    assert payload["coverage"]["items_sha256"] == _projected_sha256(payload["items"])
    for projected, original in zip(payload["items"], items):
        assert "text" not in projected
        assert projected["content_idx"] == original["content_idx"]
        assert projected["page_idx"] == original["page_idx"]
        assert projected["type"] == original["type"]
        raw_text = original.get("text")
        expected_text = raw_text if isinstance(raw_text, str) else ""
        assert "".join(projected["text_chunks"]) == expected_text
        if expected_text == "":
            assert projected["text_chunks"] == [""]
        text_level = original.get("text_level")
        if isinstance(text_level, (str, int, float)) and not isinstance(
            text_level, bool
        ):
            assert projected["text_level"] == text_level
        else:
            assert projected["text_level"] is None


# (text, expect_multiple_chunks): one scenario per JSON expansion class, plus
# empty and short control texts.  The mixed case interleaves CJK (1 encoded
# char), quote/backslash (2), NUL (6), and ASCII (1) contributions.
_MIXED_PATHOLOGICAL = "".join(
    part for _ in range(200) for part in ("\u4e16", '"', "\\", "\x00", "a")
)
_PATHOLOGICAL_TEXTS: dict[str, tuple[str, bool]] = {
    "long_ascii": ("a" * 5000, True),
    "long_cjk": ("\u4e16\u754c" * 1500, True),
    "nul_controls": ("\x00" * 500, True),
    "quotes_and_backslashes": ('"\\"' * 1000, True),
    "non_bmp": ("\U0001f600" * 2000, True),
    "mixed_pathological": (_MIXED_PATHOLOGICAL, True),
    "empty": ("", False),
    "short": ("Short title", False),
}


@pytest.mark.parametrize("text_key", sorted(_PATHOLOGICAL_TEXTS))
def test_chunked_text_line_byte_bounds_and_lossless(text_key: str) -> None:
    """Pathological text chunking keeps every physical line within both the
    character and UTF-8 byte budgets and reconstructs the exact source text."""
    text, expect_multiple = _PATHOLOGICAL_TEXTS[text_key]
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "paragraph",
            "text_level": 0,
            "text": text,
        }
    ]
    projection = _projection_for(items)
    _assert_lines_within_char_budget(projection)
    _assert_lines_within_byte_budget(projection)
    _assert_round_trip(projection, items)

    chunks = segmentation._chunk_text(text)
    assert "".join(chunks) == text
    if expect_multiple:
        assert len(chunks) > 1
    _assert_chunk_lines_within_budget(chunks)


def test_projection_deterministic() -> None:
    """Identical source items produce byte-identical projection output."""
    items = _items()
    assert _projection_for(items) == _projection_for(items)


@pytest.mark.parametrize(
    "ch",
    [
        "a",  # ASCII plain
        "\u4e16",  # CJK stays as-is
        "\U0001f600",  # non-BMP stays as-is
        '"',  # \" (2 chars)
        "\\",  # \\ (2 chars)
        "\x00",  # \u0000 (6 chars)
        "\x01",  # \u0001 (6 chars)
        "\x08",  # \b (short escape, 2 chars)
        "\x09",  # \t (short escape, 2 chars)
        "\x0a",  # \n (short escape, 2 chars)
        "\x0c",  # \f (short escape, 2 chars)
        "\x0d",  # \r (short escape, 2 chars)
        "\x1f",  # \u001f (6 chars)
        " ",  # U+0020 is not escaped
        "\x7f",  # DEL is not escaped
        "\u0080",  # C1 control is not escaped
    ],
)
def test_encoded_len_contribution_matches_json_dumps(ch: str) -> None:
    """_encoded_len_contribution predicts the exact json.dumps expansion."""
    actual = len(json.dumps(ch, ensure_ascii=False)) - 2
    assert segmentation._encoded_len_contribution(ch) == actual, (
        f"char {ch!r} (U+{ord(ch):04X}): contribution mismatch, "
        f"json.dumps says {actual}"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "\x00" * 400),  # 400 NULs -> 2400 encoded chars
        ("text_level", '"\\' * 1000),  # 2000 chars -> 4000 encoded chars
        ("text_level", 10**2100),  # 2101-digit integer
    ],
)
def test_rejects_oversized_projection_field(field: str, value: Any) -> None:
    """Metadata whose physical line exceeds _MAX_LINE_CHARS is rejected as
    ChapterSegmentationError at projection time."""
    items = [
        {"content_idx": 0, "page_idx": 0, "type": "text", "text_level": 1, "text": "ok"}
    ]
    items[0][field] = value
    with pytest.raises(
        ChapterSegmentationError,
        match=f"field '{field}' would produce a .*-character physical line",
    ):
        segmentation._project_content(items)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "a" * 1800),
        ("text_level", "y" * 1700),
        ("text_level", 10**1800),
        ("type", "\U0001f600" * 1800),
    ],
)
def test_large_valid_projection_field_preserved(field: str, value: Any) -> None:
    """Metadata that fits the line budget projects deterministically within
    both budgets and round-trips exactly."""
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "text",
            "text_level": 1,
            "text": "short",
        }
    ]
    items[0][field] = value
    projection = _projection_for(items)
    _assert_lines_within_char_budget(projection)
    _assert_lines_within_byte_budget(projection)
    payload = json.loads(projection.decode("utf-8"))
    assert payload["items"][0][field] == value


@pytest.mark.parametrize("bad_level", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_text_level(bad_level: float) -> None:
    """Non-finite floats in projected scalars surface as ChapterSegmentationError
    instead of leaking ValueError out of the projection boundary."""
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "text",
            "text_level": bad_level,
            "text": "ok",
        }
    ]
    with pytest.raises(
        ChapterSegmentationError, match="field 'text_level' cannot be serialized"
    ):
        segmentation._project_content(items)


def test_rejects_unserializable_type_value() -> None:
    """A non-JSON-serializable 'type' value surfaces as ChapterSegmentationError."""
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": object(),
            "text_level": 1,
            "text": "ok",
        }
    ]
    with pytest.raises(
        ChapterSegmentationError, match="field 'type' cannot be serialized"
    ):
        segmentation._project_content(items)


def test_projection_bytes_rejects_lone_surrogate_text() -> None:
    """Lone surrogates (legal JSON escapes, invalid UTF-8) fail the projection
    as ChapterSegmentationError at the byte-encoding boundary."""
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "text",
            "text_level": 1,
            "text": "bad \ud800 text",
        }
    ]
    with pytest.raises(ChapterSegmentationError, match="Cannot serialize"):
        segmentation._projection_bytes(items)


@pytest.mark.parametrize("text_level", [1, None, 0.5])
def test_scalar_text_level_values_round_trip(text_level: Any) -> None:
    """Integer, null, and float text_level values project and round-trip."""
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "text",
            "text_level": text_level,
            "text": "a",
        }
    ]
    projection = _projection_for(items)
    _assert_lines_within_char_budget(projection)
    _assert_round_trip(projection, items)


def test_complete_projection_bounds_with_long_metadata() -> None:
    """A complete projection with long metadata and control-character text
    stays within both line budgets and reconstructs losslessly."""
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "x" * 1700,
            "text_level": "y" * 1700,
            "text": "\x00" * 500,
        }
    ]
    projection = _projection_for(items)
    _assert_lines_within_char_budget(projection)
    _assert_lines_within_byte_budget(projection)
    _assert_round_trip(projection, items)


def test_projection_schema_version_in_written_output(tmp_path: Path) -> None:
    """The projection written to the agent carries the schema contract, the
    text_chunks-only item shape, and bounded lines."""
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    segment_chapters(content_path, tmp_path, fake)

    projection_bytes = fake.calls[0].files["content-projection.json"]
    _assert_lines_within_char_budget(projection_bytes)
    _assert_round_trip(projection_bytes, _items())


@pytest.mark.parametrize("attr", ["_MAX_LINE_CHARS", "_MAX_LINE_BYTES"])
def test_serialization_param_change_invalidates_stale_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attr: str
) -> None:
    """Changing a serialization budget in the contract fingerprint
    invalidates a previously-fresh artifact."""
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 1

    lowered = {"_MAX_LINE_CHARS": 1500, "_MAX_LINE_BYTES": 60000}[attr]
    monkeypatch.setattr(segmentation, attr, lowered)
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 2, f"{attr} change should invalidate fresh artifact"


def test_rejects_lone_surrogate_items_in_normalized_content(tmp_path: Path) -> None:
    """Normalized content whose items contain lone surrogates fails as
    ChapterSegmentationError while hashing, not a raw UnicodeEncodeError."""
    content_path = _write_content(tmp_path)
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    payload["items"][0]["text"] = "\ud800"
    # ensure_ascii=True keeps the lone surrogate as a legal JSON escape so the
    # file itself stays valid UTF-8; the sha uses the same encoding.
    payload["items_sha256"] = hashlib.sha256(
        json.dumps(
            payload["items"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    content_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

    with pytest.raises(ChapterSegmentationError, match="cannot be hashed"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))


# ---------------------------------------------------------------------------
# Projection coverage acknowledgement contract
# ---------------------------------------------------------------------------


def test_missing_coverage_acknowledgement_fails(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    response = _success_response()
    del response["coverage"]

    with pytest.raises(ChapterSegmentationError, match="schema validation"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(response))


@pytest.mark.parametrize(
    "tamper",
    [
        "extra_field",
        "missing_field",
        "non_integer_count",
        "invalid_digest",
        "non_integer_page",
    ],
)
def test_malformed_coverage_acknowledgement_fails(tmp_path: Path, tamper: str) -> None:
    content_path = _write_content(tmp_path)
    coverage = _coverage_for(_items())
    if tamper == "extra_field":
        coverage["unexpected"] = True
    elif tamper == "missing_field":
        del coverage["items_sha256"]
    elif tamper == "non_integer_count":
        coverage["item_count"] = "eleven"
    elif tamper == "invalid_digest":
        coverage["items_sha256"] = "zz" * 32
    else:
        coverage["last_page_idx"] = "seven"
    response = _success_response()
    response["coverage"] = coverage

    with pytest.raises(ChapterSegmentationError, match="schema validation"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(response))


@pytest.mark.parametrize(
    "field",
    [
        "item_count",
        "first_content_idx",
        "last_content_idx",
        "first_page_idx",
        "last_page_idx",
        "items_sha256",
    ],
)
def test_mismatched_coverage_acknowledgement_fails(tmp_path: Path, field: str) -> None:
    """A structurally valid acknowledgement with a wrong value fails."""
    content_path = _write_content(tmp_path)
    coverage = _coverage_for(_items())
    wrong: dict[str, Any] = {
        "item_count": len(_items()) + 1,
        "first_content_idx": 1,
        "last_content_idx": len(_items()) - 2,
        "first_page_idx": 1,
        "last_page_idx": 6,
        "items_sha256": "b" * 64,
    }
    coverage[field] = wrong[field]
    response = _success_response()
    response["coverage"] = coverage

    with pytest.raises(ChapterSegmentationError, match="does not match projection"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(response))


def test_stale_coverage_acknowledgement_fails_after_content_change(
    tmp_path: Path,
) -> None:
    """Changing the projected content invalidates an old acknowledgement."""
    content_path = _write_content(tmp_path)
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    payload["items"][0]["text"] = "Contents Changed"
    payload["items_sha256"] = _items_sha256(payload["items"])
    content_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChapterSegmentationError, match="does not match projection"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(_success_response()))


def test_validate_coverage_acknowledgement_accepts_raw_mapping() -> None:
    source = _items()
    validate_coverage_acknowledgement(_coverage_for(source), source)


def test_projection_coverage_digest_is_deterministic() -> None:
    items = _items()
    first = _coverage_for(items)
    second = _coverage_for(items)
    assert first == second
    digest = first["items_sha256"]
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def _projection_digest(items: list[dict[str, Any]]) -> str:
    """The coverage digest as serialized in the production projection."""
    payload = json.loads(_projection_for(items).decode("utf-8"))
    return payload["coverage"]["items_sha256"]


@pytest.mark.parametrize(
    "field",
    ["content_idx", "page_idx", "type", "text_level", "text"],
)
def test_projection_coverage_digest_changes_per_projected_field(field: str) -> None:
    """Changing any projected item field changes the coverage digest.

    Source ``text`` maps to the projected ``text_chunks`` field.  The digest
    comes from the production projection payload and is cross-checked against
    the independent oracle."""
    items = _items()
    changed = _items()
    if field == "content_idx":
        changed[0]["content_idx"] = 1000
    elif field == "page_idx":
        changed[0]["page_idx"] = 100
    elif field == "type":
        changed[0]["type"] = "heading"
    elif field == "text_level":
        changed[0]["text_level"] = 9
    else:
        changed[0]["text"] = "Different Contents"  # maps to text_chunks
    assert _projection_digest(items) != _projection_digest(changed)
    assert _projection_digest(items) == _coverage_for(items)["items_sha256"]
    assert _projection_digest(changed) == _coverage_for(changed)["items_sha256"]


def test_projection_coverage_digest_ignores_unprojected_fields() -> None:
    """Fields outside the projection contract do not enter the digest."""
    items = _items()
    changed = _items()
    changed[10]["img_path"] = "different.jpg"
    assert _projection_digest(items) == _projection_digest(changed)


def test_projection_coverage_block_exact_field_set() -> None:
    """The coverage block carries exactly the acknowledged fields.

    Physical line bounds for the whole projection, coverage block included,
    are already asserted by the full-file budget tests."""
    payload = json.loads(_projection_for(_items()).decode("utf-8"))
    assert set(payload["coverage"]) == {
        "item_count",
        "first_content_idx",
        "last_content_idx",
        "first_page_idx",
        "last_page_idx",
        "items_sha256",
    }


# ---------------------------------------------------------------------------
# Fail-closed discriminated response contract
# ---------------------------------------------------------------------------


def test_discriminated_response_validates_success_and_failure_branches() -> None:
    success = ChapterSegmentationResponse.model_validate(_success_response())
    assert isinstance(success.root, SegmentationSuccess)
    assert success.root.coverage.model_dump() == _coverage_for(_items())
    assert [b.title for b in success.root.boundaries] == [
        "Contents",
        "Chapter One",
        "Chapter Two",
        "Notes",
    ]
    failure = ChapterSegmentationResponse.model_validate(
        _failure_response("tool_failure", "Paddle CLI is unavailable")
    )
    assert isinstance(failure.root, SegmentationFailure)
    assert failure.root.code == "tool_failure"
    assert failure.root.details == "Paddle CLI is unavailable"


def test_task_instructs_complete_inspection_and_fail_closed(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    segment_chapters(content_path, tmp_path, fake)
    task = fake.calls[0].files["TASK.md"].decode("utf-8")
    assert "paginate through it with repeated reads" in task
    assert "end of the file" in task
    for code in segmentation.SEGMENTATION_FAILURE_CODES:
        assert code in task
    assert "never guess" in task
    assert "never minimize boundaries" in task
    assert '"status": "success"' in task
    assert '"status": "failure"' in task
    assert "never carry `coverage` or `boundaries`" in task


@pytest.mark.parametrize(
    "code",
    [
        "input_truncated",
        "projection_unreadable",
        "tool_failure",
        "inspection_incomplete",
    ],
)
def test_each_failure_code_fails_closed(tmp_path: Path, code: str) -> None:
    """Every closed-set failure code raises a precise error and publishes nothing."""
    content_path = _write_content(tmp_path)
    details = "Read reported a truncated projection line."
    with pytest.raises(ChapterSegmentationError, match=code) as caught:
        segment_chapters(
            content_path, tmp_path, FakeAgentRunner(_failure_response(code, details))
        )
    assert details in str(caught.value)
    assert not (tmp_path / "03_chapters" / "chapters.json").exists()


def test_failure_result_preserves_published_artifact(tmp_path: Path) -> None:
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    output_path = segment_chapters(content_path, tmp_path, fake)
    old_bytes = output_path.read_bytes()

    with pytest.raises(ChapterSegmentationError, match="input_truncated"):
        segment_chapters(
            content_path,
            tmp_path,
            FakeAgentRunner(_failure_response("input_truncated")),
            force=True,
        )
    assert output_path.read_bytes() == old_bytes


def test_failure_payload_with_success_fields_fails_schema(tmp_path: Path) -> None:
    """A failure result smuggling coverage or boundaries must be rejected."""
    content_path = _write_content(tmp_path)
    mixed = _failure_response("tool_failure")
    mixed["coverage"] = _coverage_for(_items())
    mixed["boundaries"] = _success_response()["boundaries"]
    with pytest.raises(ChapterSegmentationError, match="schema validation"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(mixed))


def test_success_payload_with_failure_fields_fails_schema(tmp_path: Path) -> None:
    """A success result carrying failure-only fields must be rejected."""
    content_path = _write_content(tmp_path)
    mixed = _success_response()
    mixed["code"] = "tool_failure"
    mixed["details"] = "must not appear on success"
    with pytest.raises(ChapterSegmentationError, match="schema validation"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(mixed))


def test_omitted_status_fails_schema() -> None:
    success = _success_response()
    del success["status"]
    with pytest.raises(ValidationError):
        ChapterSegmentationResponse.model_validate(success)
    failure = _failure_response("tool_failure")
    del failure["status"]
    with pytest.raises(ValidationError):
        ChapterSegmentationResponse.model_validate(failure)


@pytest.mark.parametrize(
    ("tamper", "match"),
    [
        ("unknown_status", "schema validation"),
        ("unknown_code", "schema validation"),
        ("missing_code", "schema validation"),
        ("missing_details", "schema validation"),
        ("empty_details", "schema validation"),
        ("oversized_details", "schema validation"),
    ],
)
def test_malformed_failure_payload_fails_schema(
    tmp_path: Path, tamper: str, match: str
) -> None:
    content_path = _write_content(tmp_path)
    payload = _failure_response("inspection_incomplete")
    if tamper == "unknown_status":
        payload = {
            "status": "maybe",
            "coverage": _coverage_for(_items()),
            "boundaries": _success_response()["boundaries"],
        }
    elif tamper == "unknown_code":
        payload["code"] = "model_made_up"
    elif tamper == "missing_code":
        del payload["code"]
    elif tamper == "missing_details":
        del payload["details"]
    elif tamper == "empty_details":
        payload["details"] = ""
    else:
        payload["details"] = "x" * 501
    with pytest.raises(ChapterSegmentationError, match=match):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(payload))


def test_failure_detected_before_coverage_and_boundary_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid failure result short-circuits before any boundary or coverage
    validation: boundaries from a failure payload can never be accepted."""
    content_path = _write_content(tmp_path)
    validated: list[str] = []

    def spy_coverage(*args: object, **kwargs: object) -> None:
        validated.append("coverage")

    def spy_boundaries(*args: object, **kwargs: object) -> None:
        validated.append("boundaries")

    monkeypatch.setattr(segmentation, "validate_coverage_acknowledgement", spy_coverage)
    monkeypatch.setattr(segmentation, "validate_boundaries", spy_boundaries)

    with pytest.raises(ChapterSegmentationError, match="inspection_incomplete"):
        segment_chapters(
            content_path,
            tmp_path,
            FakeAgentRunner(_failure_response("inspection_incomplete")),
        )
    assert validated == []


def test_success_result_still_runs_semantic_validation(tmp_path: Path) -> None:
    """Failure detection must not swallow the success path: a success payload
    with invalid boundaries still raises the boundary error."""
    content_path = _write_content(tmp_path)
    invalid = _success_response()
    invalid["boundaries"][1]["title"] = "wrong title"
    with pytest.raises(ChapterSegmentationError, match="exactly match"):
        segment_chapters(content_path, tmp_path, FakeAgentRunner(invalid))


def test_response_schema_change_invalidates_fresh_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the discriminated response schema invalidates a fresh artifact."""
    content_path = _write_content(tmp_path)
    fake = FakeAgentRunner(_success_response())
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 1

    class AltSuccess(SegmentationSuccess):
        model_config = ConfigDict(extra="forbid", strict=True)
        marker: str | None = None

    class AltResponse(
        RootModel[
            Annotated[AltSuccess | SegmentationFailure, Field(discriminator="status")]
        ]
    ):
        pass

    monkeypatch.setattr(segmentation, "ChapterSegmentationResponse", AltResponse)
    segment_chapters(content_path, tmp_path, fake)
    assert len(fake.calls) == 2
