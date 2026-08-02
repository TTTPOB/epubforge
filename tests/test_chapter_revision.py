"""Tests for the isolated multimodal chapter revision contract."""

from __future__ import annotations

from pathlib import Path
import importlib.util
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from epubforge import chapter_revision as revision_module
from epubforge.chapter_revision import (
    ChapterRevisionError,
    ChapterRevisionPublicationError,
    ChapterRevisionResponse,
    ParsedRevisionClient,
    revise_all_chapters,
    revise_chapter,
)
from epubforge.chapter_workspace import build_chapter_workspace


_fixture_spec = importlib.util.spec_from_file_location(
    "workspace_fixture", Path(__file__).with_name("test_chapter_workspace.py")
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)


class _FakeRevisionClient:
    model = "luna-medium-test"

    def __init__(self, transform=None, *, failure: str | None = None) -> None:
        self.transform = transform
        self.failure = failure
        self.calls: list[tuple[list[dict[str, Any]], bool]] = []

    def chat_parsed(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format,
        validator=None,
        bypass_cache=False,
    ):
        self.calls.append((messages, bypass_cache))
        if self.failure is not None:
            raise RuntimeError(self.failure)
        user_content = messages[1]["content"]
        assert user_content[0]["type"] == "text"
        seed = user_content[0]["text"].split("COMPLETE HTML:\n", 1)[1]
        corrected = self.transform(seed) if self.transform is not None else seed
        response = ChapterRevisionResponse(corrected_html=corrected)
        if validator is not None:
            validator(response)
        return response


def _workspace(tmp_path: Path) -> Path:
    return build_chapter_workspace(_fixture_module._write_inputs(tmp_path))


def test_multimodal_request_contains_complete_html_and_ordered_images(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    client = _FakeRevisionClient()

    revise_chapter(workspace / "chapters/0002", cast(ParsedRevisionClient, client))

    assert len(client.calls) == 1
    messages, bypass_cache = client.calls[0]
    assert bypass_cache is False
    assert messages[0]["role"] == "system"
    user_content = messages[1]["content"]
    assert "source.pdf" not in str(messages).lower()
    assert "render-page" not in str(messages).lower()
    assert user_content[0]["text"].endswith(
        (workspace / "chapters/0002/chapter.html").read_text()
    )
    image_parts = user_content[1:]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert str(workspace / "chapters/0002") not in str(messages)


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
    client = _FakeRevisionClient(lambda _: corrected)

    revise_chapter(chapter, cast(ParsedRevisionClient, client))

    assert (chapter / "corrected.html").read_text() == corrected
    assert (chapter / "revision.json").is_file()


def test_invalid_reference_or_bbox_preserves_previous_output(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    good = _FakeRevisionClient()
    revise_chapter(chapter, cast(ParsedRevisionClient, good))
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
                cast(ParsedRevisionClient, _FakeRevisionClient(lambda _: invalid)),
                force=True,
            )
        assert (chapter / "corrected.html").read_bytes() == previous


def test_fresh_skip_and_force_bypass(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0003"
    first = _FakeRevisionClient()
    revise_chapter(chapter, cast(ParsedRevisionClient, first))

    skipped = _FakeRevisionClient(failure="fresh output should skip")
    revise_chapter(chapter, cast(ParsedRevisionClient, skipped))
    assert skipped.calls == []

    forced = _FakeRevisionClient()
    revise_chapter(chapter, cast(ParsedRevisionClient, forced), force=True)
    assert forced.calls[0][1] is True


def test_batch_order_and_conservative_failure_stop(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    class FailingSecond(_FakeRevisionClient):
        def chat_parsed(self, messages, **kwargs):
            html = messages[1]["content"][0]["text"]
            if "Chapter &lt;One&gt;" in html:
                raise RuntimeError("second chapter failed")
            return super().chat_parsed(messages, **kwargs)

    report = revise_all_chapters(workspace, cast(ParsedRevisionClient, FailingSecond()))

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
    revise_chapter(chapter, cast(ParsedRevisionClient, _FakeRevisionClient()))
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
            cast(ParsedRevisionClient, _FakeRevisionClient()),
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
    revise_chapter(chapter, cast(ParsedRevisionClient, _FakeRevisionClient()))
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
            cast(ParsedRevisionClient, _FakeRevisionClient()),
            force=True,
        )
    assert caught.value.evidence
    assert all(path.exists() for path in caught.value.evidence)
    assert any(path.name.startswith(".revision-") for path in caught.value.evidence)

    monkeypatch.undo()
    revise_chapter(
        chapter, cast(ParsedRevisionClient, _FakeRevisionClient()), force=True
    )
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
    revise_chapter(chapter, cast(ParsedRevisionClient, _FakeRevisionClient()))

    real_rmtree = revision_module.shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith((".revision-staging-", ".revision-backup-")):
            raise OSError("injected cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(revision_module.shutil, "rmtree", fail_cleanup)
    revise_chapter(
        chapter,
        cast(ParsedRevisionClient, _FakeRevisionClient()),
        force=True,
    )
    assert (chapter / "corrected.html").is_file()
    assert (chapter / "revision.json").is_file()
    monkeypatch.undo()
    revise_chapter(chapter, cast(ParsedRevisionClient, _FakeRevisionClient()))
    assert not any(
        path.name.startswith((".revision-staging-", ".revision-backup-"))
        for path in chapter.iterdir()
    )


def test_concurrent_public_calls_publish_one_coherent_pair(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0003"
    clients = [_FakeRevisionClient(), _FakeRevisionClient()]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                revise_chapter,
                chapter,
                cast(ParsedRevisionClient, client),
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
            cast(ParsedRevisionClient, _FakeRevisionClient(lambda _: corrected)),
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
            cast(ParsedRevisionClient, _FakeRevisionClient()),
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
            cast(ParsedRevisionClient, _FakeRevisionClient()),
        )


def test_symlink_and_image_size_fail_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    chapter = workspace / "chapters/0002"
    page = chapter / "pages/page-0001.jpg"
    page.unlink()
    page.symlink_to(chapter / "chapter.html")
    client = _FakeRevisionClient()
    with pytest.raises(ChapterRevisionError):
        revise_chapter(chapter, cast(ParsedRevisionClient, client))
    assert client.calls == []

    workspace = _workspace(tmp_path / "size")
    chapter = workspace / "chapters/0002"
    monkeypatch.setattr(revision_module, "MAX_IMAGE_BYTES", 1)
    client = _FakeRevisionClient()
    with pytest.raises(ChapterRevisionError):
        revise_chapter(chapter, cast(ParsedRevisionClient, client))
    assert client.calls == []
