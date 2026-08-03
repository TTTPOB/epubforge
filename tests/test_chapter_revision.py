"""Tests for the isolated multimodal chapter revision contract."""

from __future__ import annotations

from pathlib import Path
import importlib.util
import hashlib
import json
import os
import re
from typing import Any
from concurrent.futures import ThreadPoolExecutor

import pytest

from epubforge import chapter_revision as revision_module
from epubforge.agent_runner import (
    AgentIdentity,
    AgentRunRequest,
    AgentRunResult,
    book_editor_identity,
)
from epubforge.chapter_revision import (
    ChapterRevisionError,
    ChapterRevisionPublicationError,
    revise_all_chapters,
    revise_chapter,
)
from epubforge.chapter_workspace import build_chapter_workspace
from epubforge.paddle_contract import (
    LAYOUT_MODEL,
    PAGE_CANDIDATE_COORDINATE_SPACE,
    PAGE_EVIDENCE_COORDINATE_SPACE,
    TEXT_MODEL,
    stable_candidate_id,
)


_fixture_spec = importlib.util.spec_from_file_location(
    "workspace_fixture", Path(__file__).with_name("test_chapter_workspace.py")
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)


class _FakeAgentRunner:
    def __init__(self, transform=None, *, failure: str | None = None) -> None:
        self.identity = book_editor_identity()
        self.transform = transform
        self.failure = failure
        self.calls: list[AgentRunRequest] = []

    def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        self.calls.append(request)
        if self.failure is not None:
            raise RuntimeError(self.failure)
        seed = request.files["corrected.html"].decode("utf-8")
        corrected = self.transform(seed) if self.transform is not None else seed
        return AgentRunResult(
            outputs={"corrected.html": corrected.encode("utf-8")},
            session_id="ses_revision_test",
        )


def _workspace(tmp_path: Path) -> Path:
    return build_chapter_workspace(_fixture_module._write_inputs(tmp_path))


def test_multimodal_request_contains_complete_html_and_ordered_images(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    agent_runner = _FakeAgentRunner()

    revise_chapter(workspace / "chapters/0002", agent_runner)

    assert len(agent_runner.calls) == 1
    request = agent_runner.calls[0]
    assert "source.pdf" not in str(request.files).lower()
    assert "render-page" not in str(request.files).lower()
    assert (
        request.files["chapter.html"]
        == (workspace / "chapters/0002/chapter.html").read_bytes()
    )
    assert request.files["corrected.html"] == request.files["chapter.html"]
    assert "chapter.json" in request.files
    assert "TASK.md" in request.files
    assert [name for name in request.files if name.startswith("pages/")] == [
        "pages/page-0001.jpg",
        "pages/page-0001.annotated.jpg",
        "pages/page-0001.meta.json",
    ]
    assert [name for name in request.files if name.startswith("assets/")] == [
        "assets/abc-cover.png"
    ]
    assert str(workspace / "chapters/0002") not in str(request.files)


def test_valid_heading_header_removal_and_bbox_marker_publish(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0001"
    seed = (chapter / "chapter.html").read_text()
    corrected = seed.replace(
        ' id="content-00000001" data-content-idx="1" data-page-idx="0" '
        'data-type="header" data-bbox="5,25,60,35"',
        "",
    )
    corrected = corrected.replace("<h1", "<h2", 1).replace("</h1>", "</h2>", 1)
    corrected = corrected.replace(
        'data-bbox="5,5,60,18"',
        'data-bbox="5,5,60,18" data-bbox-status="needs-repair"',
        1,
    )
    client = _FakeAgentRunner(lambda _: corrected)

    revise_chapter(chapter, client)

    assert (chapter / "corrected.html").read_text() == corrected
    record = json.loads((chapter / "revision.json").read_text())
    assert record["schema_version"] == 3
    assert record["agent_name"] == "book-editor"
    assert record["agent_model"] == "openai/gpt-5.6-luna"
    assert record["agent_variant"] == "medium"
    assert record["session_id"] == "ses_revision_test"
    assert record["bbox_repairs_sha256"] is None
    assert record["paddle_evidence_sha256"] is None
    assert record["applied_bbox_repairs"] == []
    assert "notes" not in record
    assert "open_questions" not in record


def test_invalid_reference_or_bbox_preserves_previous_output(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    good = _FakeAgentRunner()
    revise_chapter(chapter, good)
    previous = (chapter / "corrected.html").read_bytes()
    seed = (chapter / "chapter.html").read_text()

    for invalid in (
        seed.replace('data-content-idx="3"', 'data-content-idx="999"', 1),
        seed.replace('data-bbox="5,5,70,18"', 'data-bbox="5,5,70,19"', 1),
        seed.replace("<p ", '<p onclick="alert(1)" ', 1),
    ):
        with pytest.raises(ChapterRevisionError):
            revise_chapter(
                chapter,
                _FakeAgentRunner(lambda _: invalid),
                force=True,
            )
        assert (chapter / "corrected.html").read_bytes() == previous


def test_fresh_skip_and_force_rerun(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0003"
    first = _FakeAgentRunner()
    revise_chapter(chapter, first)

    skipped = _FakeAgentRunner(failure="fresh output should skip")
    revise_chapter(chapter, skipped)
    assert skipped.calls == []

    changed = _FakeAgentRunner()
    identity = changed.identity
    changed.identity = AgentIdentity(
        name=identity.name,
        model=identity.model,
        variant=identity.variant,
        prompt_sha256=identity.prompt_sha256,
        fingerprint="d" * 64,
    )
    revise_chapter(chapter, changed)
    assert len(changed.calls) == 1

    forced = _FakeAgentRunner()
    revise_chapter(chapter, forced, force=True)
    assert len(forced.calls) == 1


def test_batch_order_and_conservative_failure_stop(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    class FailingSecond(_FakeAgentRunner):
        def __call__(self, request: AgentRunRequest) -> AgentRunResult:
            html = request.files["chapter.html"].decode("utf-8")
            if "Chapter &lt;One&gt;" in html:
                raise RuntimeError("second chapter failed")
            return super().__call__(request)

    report = revise_all_chapters(workspace, FailingSecond())

    assert [path.name for path in report.completed] == ["0001"]
    assert [path.name for path in report.failed] == ["0002"]
    assert (workspace / "chapters/0001/corrected.html").is_file()
    assert not (workspace / "chapters/0003/corrected.html").exists()


@pytest.mark.parametrize("failure_call", [1, 2, 3, 4])
def test_each_replace_failure_restores_the_complete_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())
    old_corrected = (chapter / "corrected.html").read_bytes()
    old_revision = (chapter / "revision.json").read_bytes()
    real_replace = revision_module.os.replace
    calls = 0

    def fail_once(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected replace failure")
        real_replace(source, target)

    monkeypatch.setattr(revision_module.os, "replace", fail_once)
    with pytest.raises(ChapterRevisionError):
        revise_chapter(
            chapter,
            _FakeAgentRunner(),
            force=True,
        )
    assert (chapter / "corrected.html").read_bytes() == old_corrected
    assert (chapter / "revision.json").read_bytes() == old_revision


def test_rollback_failure_preserves_actionable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())
    real_replace = revision_module.os.replace
    calls = 0

    def fail_publication_and_rollback(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("injected publication and rollback failure")
        real_replace(source, target)

    monkeypatch.setattr(revision_module.os, "replace", fail_publication_and_rollback)
    with pytest.raises(ChapterRevisionPublicationError) as caught:
        revise_chapter(
            chapter,
            _FakeAgentRunner(),
            force=True,
        )
    assert caught.value.evidence
    assert all(path.exists() for path in caught.value.evidence)
    assert any(path.name.startswith(".revision-") for path in caught.value.evidence)

    monkeypatch.undo()
    revise_chapter(chapter, _FakeAgentRunner(), force=True)
    assert not any(
        path.name.startswith((".revision-staging-", ".revision-backup-"))
        for path in chapter.iterdir()
    )


def test_successful_pair_survives_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())

    real_rmtree = revision_module.shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith((".revision-staging-", ".revision-backup-")):
            raise OSError("injected cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(revision_module.shutil, "rmtree", fail_cleanup)
    revise_chapter(
        chapter,
        _FakeAgentRunner(),
        force=True,
    )
    assert (chapter / "corrected.html").is_file()
    assert (chapter / "revision.json").is_file()
    monkeypatch.undo()
    revise_chapter(chapter, _FakeAgentRunner())
    assert not any(
        path.name.startswith((".revision-staging-", ".revision-backup-"))
        for path in chapter.iterdir()
    )


def test_concurrent_public_calls_publish_one_coherent_pair(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0003"
    clients = [_FakeAgentRunner(), _FakeAgentRunner()]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                revise_chapter,
                chapter,
                client,
            )
            for client in clients
        ]
        results = [future.result() for future in futures]

    assert results == [chapter / "corrected.html"] * 2
    assert sum(len(client.calls) for client in clients) == 1
    revision = json.loads((chapter / "revision.json").read_text())
    assert revision["corrected_html_sha256"] == revision_module._sha256_bytes(
        (chapter / "corrected.html").read_bytes()
    )


def test_reverse_split_order_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0003"
    seed = (chapter / "chapter.html").read_text()
    start = seed.index('<h1 id="content-00000007"')
    end = seed.index("</h1>", start) + len("</h1>")
    block = seed[start:end]
    first = block.replace(
        ">Notes</h1>",
        ' data-content-part="2/2">Notes second</h1>',
    ).replace('id="content-00000007"', 'id="split-second"')
    second = block.replace(
        ">Notes</h1>",
        ' data-content-part="1/2">Notes first</h1>',
    ).replace('id="content-00000007"', 'id="split-first"')
    corrected = seed[:start] + first + second + seed[end:]

    with pytest.raises(ChapterRevisionError):
        revise_chapter(
            chapter,
            _FakeAgentRunner(lambda _: corrected),
            force=True,
        )


def test_malformed_root_manifest_is_wrapped_before_sorting(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["chapters"][0]["ordinal"] = "1"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ChapterRevisionError):
        revise_chapter(
            workspace / "chapters/0001",
            _FakeAgentRunner(),
        )


def test_malformed_nested_chapter_manifest_is_wrapped(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    chapter_manifest_path = workspace / "chapters/0001/chapter.json"
    chapter_manifest = json.loads(chapter_manifest_path.read_text())
    chapter_manifest["ordinal"] = "1"
    chapter_manifest_path.write_text(json.dumps(chapter_manifest))
    digest = hashlib.sha256(chapter_manifest_path.read_bytes()).hexdigest()
    root_manifest_path = workspace / "manifest.json"
    root_manifest = json.loads(root_manifest_path.read_text())
    root_manifest["chapters"][0]["chapter_sha256"] = digest
    root_manifest["files_sha256"]["chapters/0001/chapter.json"] = digest
    root_manifest_path.write_text(json.dumps(root_manifest))

    with pytest.raises(ChapterRevisionError):
        revise_chapter(
            workspace / "chapters/0001",
            _FakeAgentRunner(),
        )


def test_symlink_and_image_size_fail_before_agent_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    page = chapter / "pages/page-0001.jpg"
    page.unlink()
    page.symlink_to(chapter / "chapter.html")
    client = _FakeAgentRunner()
    with pytest.raises(ChapterRevisionError):
        revise_chapter(chapter, client)
    assert client.calls == []

    workspace = _workspace(tmp_path / "size")
    chapter = workspace / "chapters/0002"
    monkeypatch.setattr(revision_module, "MAX_IMAGE_BYTES", 1)
    client = _FakeAgentRunner()
    with pytest.raises(ChapterRevisionError):
        revise_chapter(chapter, client)
    assert client.calls == []


@pytest.mark.parametrize("target_name", ["corrected.html", "revision.json"])
def test_hardlinked_published_output_is_rejected_and_preserved(
    tmp_path: Path, target_name: str
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())
    target = chapter / target_name
    previous = target.read_bytes()
    linked_copy = tmp_path / f"linked-{target_name}"
    os.link(target, linked_copy)

    with pytest.raises(ChapterRevisionError, match="hard link"):
        revise_chapter(chapter, _FakeAgentRunner(), force=True)

    assert target.read_bytes() == previous
    assert linked_copy.read_bytes() == previous


def test_hardlinked_source_asset_remains_valid_for_agent_handoff(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    asset = chapter / "assets/abc-cover.png"
    external_asset = tmp_path / "external-asset.png"
    external_asset.write_bytes(asset.read_bytes())
    asset.unlink()
    os.link(external_asset, asset)

    client = _FakeAgentRunner()
    revise_chapter(chapter, client)

    assert len(client.calls) == 1
    assert (chapter / "corrected.html").is_file()


class _RepairAgentRunner:
    """Fake agent that returns repair and evidence outputs alongside HTML."""

    def __init__(
        self,
        transform=None,
        *,
        repairs=None,
        evidence=None,
        failure: str | None = None,
    ) -> None:
        self.identity = book_editor_identity()
        self.transform = transform
        self.repairs = repairs
        self.evidence = evidence
        self.failure = failure
        self.calls: list[AgentRunRequest] = []

    def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        self.calls.append(request)
        if self.failure is not None:
            raise RuntimeError(self.failure)
        seed = request.files["corrected.html"].decode("utf-8")
        corrected = self.transform(seed) if self.transform is not None else seed
        outputs = {"corrected.html": corrected.encode("utf-8")}
        if self.repairs is not None:
            outputs["bbox-repairs.json"] = json.dumps(
                self.repairs, ensure_ascii=False
            ).encode("utf-8")
        if self.evidence is not None:
            outputs["paddle-evidence.json"] = json.dumps(
                self.evidence, ensure_ascii=False
            ).encode("utf-8")
        return AgentRunResult(outputs=outputs, session_id="ses_repair_test")


def _chapter_meta(workspace: Path, page_idx: int = 1) -> dict[str, Any]:
    path = workspace / f"chapters/0002/pages/page-{page_idx:04d}.meta.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate(
    page_idx: int, x0: float, y0: float, x1: float, y1: float
) -> dict[str, Any]:
    return {
        "id": stable_candidate_id(page_idx, "FIGURE", [x0, y0, x1, y1]),
        "type": "FIGURE",
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "score": 0.9,
    }


def _evidence_payload(
    workspace: Path,
    page_idx: int,
    boxes: list[dict[str, Any]],
    *,
    source_image: str | None = None,
    source_image_sha256: str | None = None,
    raster: dict[str, int] | None = None,
) -> dict[str, Any]:
    meta = _chapter_meta(workspace, page_idx)
    image = (
        dict(raster)
        if raster is not None
        else {"width": meta["raster"]["width"], "height": meta["raster"]["height"]}
    )
    if source_image_sha256 is None:
        clean_bytes = (
            workspace / f"chapters/0002/pages/page-{page_idx:04d}.jpg"
        ).read_bytes()
        source_image_sha256 = hashlib.sha256(clean_bytes).hexdigest()
    return {
        "schema_version": 1,
        "pages": [
            {
                "page_idx": page_idx,
                "source_image": source_image or f"pages/page-{page_idx:04d}.jpg",
                "source_image_sha256": source_image_sha256,
                "image": dict(image),
                "evidence": {
                    "schema_version": 1,
                    "page": page_idx,
                    "image": dict(image),
                    "coordinate_space": PAGE_EVIDENCE_COORDINATE_SPACE,
                    "layout_boxes": [],
                    "db_lines": [],
                },
                "candidate": {
                    "schema_version": 3,
                    "page": page_idx,
                    "image": dict(image),
                    "coordinate_space": dict(PAGE_CANDIDATE_COORDINATE_SPACE),
                    "inference_params": {
                        "layout_model": LAYOUT_MODEL,
                        "text_model": TEXT_MODEL,
                        "source_sha256": source_image_sha256,
                    },
                    "boxes": boxes,
                },
            }
        ],
    }


def _element_tag(html: str, content_idx: int) -> str:
    marker = f'data-content-idx="{content_idx}"'
    position = html.index(marker)
    start = html.rfind("<", 0, position)
    end = html.find(">", position)
    return html[start : end + 1]


def _bbox_attr(html: str, content_idx: int) -> list[float] | None:
    tag = _element_tag(html, content_idx)
    match = re.search(r'data-bbox="([^"]+)"', tag)
    if match is None:
        return None
    return [float(value) for value in match.group(1).split(",")]


def _expected_canonical(
    workspace: Path, page_idx: int, pixel_box: list[float]
) -> list[float]:
    meta = _chapter_meta(workspace, page_idx)
    scale_x = meta["canonical"]["width"] / meta["raster"]["width"]
    scale_y = meta["canonical"]["height"] / meta["raster"]["height"]
    return [
        pixel_box[0] * scale_x,
        pixel_box[1] * scale_y,
        pixel_box[2] * scale_x,
        pixel_box[3] * scale_y,
    ]


def test_valid_replace_repair_applies_canonical_bbox(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    meta = _chapter_meta(workspace)
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Figure box covers the caption area.",
            }
        ],
    }
    evidence = _evidence_payload(workspace, 1, [candidate_box])
    client = _RepairAgentRunner(repairs=repairs, evidence=evidence)

    revise_chapter(chapter, client)

    corrected = (chapter / "corrected.html").read_text(encoding="utf-8")
    applied = _bbox_attr(corrected, 5)
    assert applied is not None
    expected = _expected_canonical(workspace, 1, [40, 100, 150, 160])
    assert applied == pytest.approx(expected, abs=1e-5)
    assert applied != [5.0, 45.0, 75.0, 80.0]

    record = json.loads((chapter / "revision.json").read_text(encoding="utf-8"))
    assert record["schema_version"] == 3
    assert len(record["applied_bbox_repairs"]) == 1
    applied_record = record["applied_bbox_repairs"][0]
    assert applied_record["data_content_idx"] == 5
    assert applied_record["data_content_part"] is None
    assert applied_record["page_idx"] == 1
    assert applied_record["operation"] == "replace"
    assert applied_record["candidate_ids"] == [candidate_box["id"]]
    assert [float(value) for value in applied_record["bbox"]] == pytest.approx(
        applied, abs=1e-5
    )
    assert applied_record["reason"] == "Figure box covers the caption area."
    assert (
        record["bbox_repairs_sha256"]
        == hashlib.sha256(
            json.dumps(repairs, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    )
    assert (
        record["paddle_evidence_sha256"]
        == hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    )


def test_valid_union_repair_combines_candidates(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    first = _candidate(1, 40, 100, 100, 130)
    second = _candidate(1, 60, 120, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [first["id"], second["id"]],
                "operation": "union",
                "reason": "Two partial detections merge into the figure box.",
            }
        ],
    }
    evidence = _evidence_payload(workspace, 1, [first, second])

    revise_chapter(chapter, _RepairAgentRunner(repairs=repairs, evidence=evidence))

    corrected = (chapter / "corrected.html").read_text(encoding="utf-8")
    applied = _bbox_attr(corrected, 5)
    expected = _expected_canonical(workspace, 1, [40, 100, 150, 160])
    assert applied == pytest.approx(expected, abs=1e-5)


def test_empty_repairs_publish_unchanged_html(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    seed = (chapter / "chapter.html").read_text()

    client = _RepairAgentRunner(
        repairs={"schema_version": 1, "repairs": []},
        evidence=_evidence_payload(workspace, 1, []),
    )
    revise_chapter(chapter, client)

    assert (chapter / "corrected.html").read_text() == seed
    record = json.loads((chapter / "revision.json").read_text(encoding="utf-8"))
    assert record["applied_bbox_repairs"] == []
    assert record["bbox_repairs_sha256"] is not None
    assert record["paddle_evidence_sha256"] is not None

    # Absent repair file is treated as empty repairs as well.
    revise_chapter(chapter, _FakeAgentRunner(), force=True)
    record = json.loads((chapter / "revision.json").read_text(encoding="utf-8"))
    assert record["applied_bbox_repairs"] == []
    assert record["bbox_repairs_sha256"] is None
    assert record["paddle_evidence_sha256"] is None


def test_repairs_without_evidence_are_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": ["p0001-figure-3f2a9c1d4e"],
                "operation": "replace",
                "reason": "Needs evidence.",
            }
        ],
    }

    with pytest.raises(ChapterRevisionError, match="requires paddle-evidence"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(repairs=repairs),
        )
    assert not (chapter / "corrected.html").exists()


def test_repair_unknown_candidate_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": ["p0001-figure-deadbeef00"],
                "operation": "replace",
                "reason": "Bad candidate reference.",
            }
        ],
    }

    with pytest.raises(ChapterRevisionError, match="unknown candidate"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(
                repairs=repairs,
                evidence=_evidence_payload(workspace, 1, [candidate_box]),
            ),
        )


def test_repair_page_mismatch_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 0,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Wrong page.",
            }
        ],
    }

    with pytest.raises(ChapterRevisionError, match="data-page-idx"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(
                repairs=repairs,
                evidence=_evidence_payload(workspace, 1, [candidate_box]),
            ),
        )


def test_repair_duplicate_target_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "First.",
            },
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Second.",
            },
        ],
    }

    with pytest.raises(ChapterRevisionError, match="duplicate repair target"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(
                repairs=repairs,
                evidence=_evidence_payload(workspace, 1, [candidate_box]),
            ),
        )


def test_repair_missing_target_element_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 2,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Content 2 lives in another chapter.",
            }
        ],
    }

    with pytest.raises(ChapterRevisionError, match="targets no output element"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(
                repairs=repairs,
                evidence=_evidence_payload(workspace, 1, [candidate_box]),
            ),
        )


def test_repair_evidence_raster_mismatch_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Resized evidence.",
            }
        ],
    }
    evidence = _evidence_payload(
        workspace,
        1,
        [candidate_box],
        raster={"width": 200, "height": 240},
    )

    with pytest.raises(ChapterRevisionError, match="does not match page 1 metadata"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(repairs=repairs, evidence=evidence),
        )


def test_repair_evidence_source_image_must_be_clean_workspace_page(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Wrong image source.",
            }
        ],
    }
    evidence = _evidence_payload(
        workspace, 1, [candidate_box], source_image="pages/page-0002.jpg"
    )

    with pytest.raises(ChapterRevisionError, match="clean workspace page image"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(repairs=repairs, evidence=evidence),
        )


def test_repair_targets_needs_repair_element_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    seed = (chapter / "chapter.html").read_text()
    marked = seed.replace(
        'data-bbox="5,45,75,80"',
        'data-bbox="5,45,75,80" data-bbox-status="needs-repair"',
        1,
    )
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Conflict with needs-repair.",
            }
        ],
    }

    with pytest.raises(ChapterRevisionError, match="needs-repair element"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(
                transform=lambda _: marked,
                repairs=repairs,
                evidence=_evidence_payload(workspace, 1, [candidate_box]),
            ),
        )


def test_changed_bbox_without_matching_repair_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    seed = (chapter / "chapter.html").read_text()
    tampered = seed.replace('data-bbox="5,45,75,80"', 'data-bbox="5,45,80,90"', 1)
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Agent also hand-edited the bbox.",
            }
        ],
    }

    with pytest.raises(ChapterRevisionError, match="changes a source data-bbox"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(
                transform=lambda _: tampered,
                repairs=repairs,
                evidence=_evidence_payload(workspace, 1, [candidate_box]),
            ),
        )


def test_repair_record_freshness_and_tamper_detection(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Freshness round trip.",
            }
        ],
    }
    evidence = _evidence_payload(workspace, 1, [candidate_box])
    first = _RepairAgentRunner(repairs=repairs, evidence=evidence)
    revise_chapter(chapter, first)

    # A fresh rerun must not invoke the agent again.
    skipped = _RepairAgentRunner(failure="fresh output should skip")
    revise_chapter(chapter, skipped)
    assert skipped.calls == []

    # Tampering with the applied bbox invalidates freshness.
    corrected_path = chapter / "corrected.html"
    record = json.loads((chapter / "revision.json").read_text(encoding="utf-8"))
    applied_bbox = record["applied_bbox_repairs"][0]["bbox"]
    original_attr = 'data-bbox="' + ",".join(applied_bbox) + '"'
    assert original_attr in corrected_path.read_text()
    tampered_attr = original_attr.replace(applied_bbox[2], "200")
    corrected_path.write_text(
        corrected_path.read_text().replace(original_attr, tampered_attr, 1),
        encoding="utf-8",
    )
    rerun = _RepairAgentRunner(failure="tampered output must rerun the agent")
    with pytest.raises(ChapterRevisionError, match="agent failed"):
        revise_chapter(chapter, rerun)
    assert len(rerun.calls) == 1


def test_repair_forged_candidate_id_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    forged = _candidate(1, 40, 100, 150, 160)
    forged["id"] = "p0001-figure-3f2a9c1d4e"  # regex-valid but not derived
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [forged["id"]],
                "operation": "replace",
                "reason": "Forged candidate.",
            }
        ],
    }

    with pytest.raises(ChapterRevisionError, match="recomputed stable ID"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(
                repairs=repairs,
                evidence=_evidence_payload(workspace, 1, [forged]),
            ),
        )


def test_repair_evidence_missing_source_image_hash_is_rejected(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Missing image hash.",
            }
        ],
    }
    evidence = _evidence_payload(workspace, 1, [candidate_box])
    del evidence["pages"][0]["source_image_sha256"]

    with pytest.raises(ChapterRevisionError, match="source image hash"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(repairs=repairs, evidence=evidence),
        )


def test_repair_evidence_source_image_hash_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Stale image hash.",
            }
        ],
    }
    evidence = _evidence_payload(
        workspace,
        1,
        [candidate_box],
        source_image_sha256="f" * 64,
    )

    with pytest.raises(ChapterRevisionError, match="clean page image snapshot"):
        revise_chapter(
            chapter,
            _RepairAgentRunner(repairs=repairs, evidence=evidence),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["applied_bbox_repairs"][0].pop("reason"),
        lambda payload: payload["applied_bbox_repairs"][0].__setitem__(
            "bbox", payload["applied_bbox_repairs"][0]["bbox"][:3]
        ),
        lambda payload: payload["applied_bbox_repairs"][0].update(
            {"operation": "invented"}
        ),
    ],
)
def test_corrupted_applied_repairs_make_record_stale(tmp_path: Path, mutate) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Corruption round trip.",
            }
        ],
    }
    evidence = _evidence_payload(workspace, 1, [candidate_box])
    revise_chapter(
        chapter,
        _RepairAgentRunner(repairs=repairs, evidence=evidence),
    )
    revision_path = chapter / "revision.json"
    payload = json.loads(revision_path.read_text(encoding="utf-8"))
    mutate(payload)
    revision_path.write_text(json.dumps(payload), encoding="utf-8")

    rerun = _FakeAgentRunner(failure="corrupted record must rerun the agent")
    with pytest.raises(ChapterRevisionError, match="agent failed"):
        revise_chapter(chapter, rerun)
    assert len(rerun.calls) == 1


def test_crash_recovery_restores_previous_pair_from_backup(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())
    previous_corrected = (chapter / "corrected.html").read_bytes()
    previous_revision = (chapter / "revision.json").read_bytes()

    backup = chapter / ".revision-backup-crash"
    backup.mkdir()
    os.replace(chapter / "corrected.html", backup / "corrected.html")
    os.replace(chapter / "revision.json", backup / "revision.json")

    skipped = _FakeAgentRunner(failure="restored fresh output must skip")
    revise_chapter(chapter, skipped)
    assert skipped.calls == []
    assert (chapter / "corrected.html").read_bytes() == previous_corrected
    assert (chapter / "revision.json").read_bytes() == previous_revision
    assert not backup.exists()


def test_crash_recovery_restores_coherent_pair_when_one_target_missing(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())
    previous_corrected = (chapter / "corrected.html").read_bytes()
    previous_revision = (chapter / "revision.json").read_bytes()

    backup = chapter / ".revision-backup-crash"
    backup.mkdir()
    os.replace(chapter / "corrected.html", backup / "corrected.html")

    skipped = _FakeAgentRunner(failure="restored fresh output must skip")
    revise_chapter(chapter, skipped)
    assert skipped.calls == []
    assert (chapter / "corrected.html").read_bytes() == previous_corrected
    assert (chapter / "revision.json").read_bytes() == previous_revision
    assert not backup.exists()


def test_crash_recovery_restores_pair_from_staging(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())
    previous_corrected = (chapter / "corrected.html").read_bytes()
    previous_revision = (chapter / "revision.json").read_bytes()

    staging = chapter / ".revision-staging-crash"
    staging.mkdir()
    (staging / "corrected.html").write_bytes(previous_corrected)
    (staging / "revision.json").write_bytes(previous_revision)
    (chapter / "corrected.html").unlink()
    (chapter / "revision.json").unlink()

    skipped = _FakeAgentRunner(failure="restored fresh output must skip")
    revise_chapter(chapter, skipped)
    assert skipped.calls == []
    assert (chapter / "corrected.html").read_bytes() == previous_corrected
    assert (chapter / "revision.json").read_bytes() == previous_revision
    assert not staging.exists()


def test_crash_recovery_raises_when_no_complete_copy(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    revise_chapter(chapter, _FakeAgentRunner())

    backup = chapter / ".revision-backup-crash"
    backup.mkdir()
    os.replace(chapter / "corrected.html", backup / "corrected.html")
    (chapter / "revision.json").unlink()

    with pytest.raises(ChapterRevisionError, match="no complete recovery copy"):
        revise_chapter(chapter, _FakeAgentRunner(failure="must not run"))
    assert backup.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda page: page["evidence"].update({"coordinate_space": "tampered"}),
        lambda page: page["candidate"].update({"coordinate_space": {"source": "tampered"}}),
        lambda page: page["candidate"]["inference_params"].update(
            {"source_sha256": "e" * 64}
        ),
        lambda page: page["candidate"]["inference_params"].update(
            {"layout_model": "evil-model"}
        ),
        lambda page: page["candidate"].pop("inference_params"),
    ],
)
def test_repair_evidence_contract_tampering_is_rejected(
    tmp_path: Path, mutate
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    candidate_box = _candidate(1, 40, 100, 150, 160)
    repairs = {
        "schema_version": 1,
        "repairs": [
            {
                "data_content_idx": 5,
                "data_content_part": None,
                "page_idx": 1,
                "candidate_ids": [candidate_box["id"]],
                "operation": "replace",
                "reason": "Contract tampering.",
            }
        ],
    }
    evidence = _evidence_payload(workspace, 1, [candidate_box])
    mutate(evidence["pages"][0])

    with pytest.raises(ChapterRevisionError):
        revise_chapter(
            chapter,
            _RepairAgentRunner(repairs=repairs, evidence=evidence),
        )
    assert not (chapter / "corrected.html").exists()


def test_revision_rejects_stale_workspace_contract(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contract_sha256"] = "a" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    client = _FakeAgentRunner()
    with pytest.raises(ChapterRevisionError, match="contract_sha256"):
        revise_chapter(workspace / "chapters/0002", client)
    assert client.calls == []


def _tamper_page_meta(workspace: Path, mutate) -> None:
    relative = "chapters/0002/pages/page-0001.meta.json"
    meta_path = workspace / relative
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    mutate(payload)
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files_sha256"][relative] = hashlib.sha256(
        meta_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda meta: meta.update({"schema": "epubforge.evil"}), "invalid schema"),
        (lambda meta: meta.update({"schema_version": 2}), "invalid schema version"),
        (lambda meta: meta.update({"page_idx": 7}), "invalid page_idx"),
        (lambda meta: meta["canonical"].update({"width": 0}), "canonical width"),
        (lambda meta: meta["canonical"].update({"height": -3}), "canonical height"),
        (lambda meta: meta["canonical"].update({"width": "100"}), "canonical width"),
        (lambda meta: meta["raster"].update({"width": 0}), "raster width"),
        (lambda meta: meta["raster"].update({"width": 100.5}), "raster width"),
        (lambda meta: meta["raster"].update({"height": "200"}), "raster height"),
        (lambda meta: meta["conversion"].update({"origin": "bottom-left"}), "origin"),
        (lambda meta: meta["conversion"].update({"x_axis": "left"}), "x_axis"),
        (lambda meta: meta["conversion"].update({"y_axis": "up"}), "y_axis"),
        (lambda meta: meta["conversion"].update({"scale_x": 99.0}), "scale"),
        (lambda meta: meta.pop("conversion"), "conversion"),
    ],
)
def test_revision_rejects_tampered_page_meta(
    tmp_path: Path, mutate, message: str
) -> None:
    workspace = _workspace(tmp_path)
    _tamper_page_meta(workspace, mutate)
    chapter = workspace / "chapters/0002"
    client = _FakeAgentRunner()

    with pytest.raises(ChapterRevisionError, match=message):
        revise_chapter(chapter, client)
    assert client.calls == []
