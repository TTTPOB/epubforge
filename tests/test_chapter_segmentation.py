"""Tests for normalized-content chapter boundary detection."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

import pytest
from pydantic import ValidationError

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
    segment_chapters,
    validate_boundaries,
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
        ]
    }


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
    assert '"content_idx":10' in projection
    assert "an embedded article" in projection
    for forbidden in ("pdf", "render-page", "source.jpg", "image inspection"):
        assert forbidden not in projection


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

    parsed = ChapterSegmentationResponse.model_validate({"boundaries": response})
    with pytest.raises(ChapterSegmentationError, match=match):
        validate_boundaries(parsed.boundaries, source)


def test_strict_response_and_confidence_range() -> None:
    with pytest.raises(ValidationError):
        ChapterSegmentationResponse.model_validate(
            {"boundaries": [{**_success_response()["boundaries"][1], "extra": True}]}
        )
    with pytest.raises(ValidationError):
        ChapterSegmentationResponse.model_validate(
            {
                "boundaries": [
                    {**_success_response()["boundaries"][1], "confidence": 1.1}
                ]
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
        (3, ("content_idx", "page_idx", "type", "text_level", "text")),
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
