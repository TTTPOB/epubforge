"""Tests for deterministic MinerU chapter workspaces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
from typing import Any

import pytest
import pymupdf

from epubforge import chapter_workspace as workspace
from epubforge.agent_runner import book_editor_identity
from epubforge.chapter_workspace import (
    ChapterWorkspaceError,
    build_chapter_workspace,
)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _content_source_sha256(
    items: list[dict[str, Any]], geometry: list[dict[str, Any]]
) -> str:
    return _sha256_json({"items": items, "page_geometry": geometry})


def _write_pdf(path: Path) -> str:
    document = pymupdf.open()
    try:
        for index in range(3):
            page = document.new_page(width=100, height=120)
            page.draw_rect(page.rect, fill=(1, 1, 1))
            page.insert_text((8, 20), f"Page {index}", fontsize=10)
        document.save(str(path))
    finally:
        document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_inputs(tmp_path: Path) -> Path:
    work = tmp_path / "book"
    source_dir = work / "source"
    content_dir = work / "02_content"
    chapters_dir = work / "03_chapters"
    source_dir.mkdir(parents=True)
    content_dir.mkdir()
    chapters_dir.mkdir()
    source_pdf = source_dir / "source.pdf"
    pdf_sha = _write_pdf(source_pdf)
    image = b"synthetic image"
    image_sha = hashlib.sha256(image).hexdigest()
    (content_dir / "assets").mkdir()
    (content_dir / "assets" / "abc-cover.png").write_bytes(image)
    items = [
        {
            "content_idx": 0,
            "page_idx": 0,
            "type": "text",
            "text_level": 1,
            "text": "Cover & <Title>",
            "bbox": [5, 5, 60, 18],
        },
        {
            "content_idx": 1,
            "page_idx": 0,
            "type": "header",
            "text": "Header",
            "bbox": [5, 25, 60, 35],
        },
        {
            "content_idx": 2,
            "page_idx": 0,
            "type": "table",
            "html": "<table><tbody><tr><td>5 &lt; 6</td></tr></tbody></table>",
            "text": "Table text",
            "bbox": [5, 40, 80, 60],
        },
        {
            "content_idx": 3,
            "page_idx": 1,
            "type": "text",
            "text_level": 1,
            "text": "Chapter <One>",
            "bbox": [5, 5, 70, 18],
        },
        {
            "content_idx": 4,
            "page_idx": 1,
            "type": "text",
            "text_level": 0,
            "text": "Body & <text>",
            "bbox": [5, 25, 90, 40],
        },
        {
            "content_idx": 5,
            "page_idx": 1,
            "type": "image",
            "text": "Figure <caption>",
            "img_path": "assets/abc-cover.png",
            "image_caption": ["Caption & more"],
            "bbox": [5, 45, 75, 80],
        },
        {
            "content_idx": 6,
            "page_idx": 1,
            "type": "footnote",
            "text": "Footnote",
            "bbox": [5, 85, 70, 100],
        },
        {
            "content_idx": 7,
            "page_idx": 2,
            "type": "heading",
            "text_level": 1,
            "text": "Notes",
            "bbox": [5, 5, 50, 18],
        },
    ]
    page_geometry = [
        {"page_idx": index, "width": 100.0, "height": 120.0} for index in range(3)
    ]
    content = {
        "schema": "epubforge.mineru-content",
        "schema_version": 2,
        "source_archive_sha256": "a" * 64,
        "source_archive_size": 1,
        "source_kind": "direct",
        "segment_count": 1,
        "page_count": 3,
        "source_pdf_sha256": pdf_sha,
        "items_sha256": _sha256_json(items),
        "normalization": {"contract_version": 2},
        "page_geometry": page_geometry,
        "assets": {"assets/abc-cover.png": {"sha256": image_sha, "size": len(image)}},
        "items": items,
    }
    content_path = content_dir / "content.json"
    content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    boundaries = [
        {
            "title": "Cover & <Title>",
            "kind": "frontmatter",
            "start_content_idx": 0,
            "start_page_idx": 0,
            "confidence": 0.9,
            "evidence": "Opening front matter.",
        },
        {
            "title": "Chapter <One>",
            "kind": "chapter",
            "start_content_idx": 3,
            "start_page_idx": 1,
            "confidence": 0.9,
            "evidence": "Body chapter.",
        },
        {
            "title": "Notes",
            "kind": "backmatter",
            "start_content_idx": 7,
            "start_page_idx": 2,
            "confidence": 0.9,
            "evidence": "Closing notes.",
        },
    ]
    identity = book_editor_identity()
    chapter_plan = {
        "schema": "epubforge.chapter-segmentation",
        "schema_version": 2,
        "source_content_sha256": _content_source_sha256(items, page_geometry),
        "agent_name": identity.name,
        "agent_model": identity.model,
        "agent_variant": identity.variant,
        "agent_fingerprint": identity.fingerprint,
        "prompt_sha256": "b" * 64,
        "contract_sha256": "c" * 64,
        "boundaries": boundaries,
    }
    (chapters_dir / "chapters.json").write_text(
        json.dumps(chapter_plan, ensure_ascii=False), encoding="utf-8"
    )
    return work


def test_builds_ranges_html_assets_and_annotated_pages(tmp_path: Path) -> None:
    work = _write_inputs(tmp_path)
    output = build_chapter_workspace(work)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["chapters"]] == [
        "chapters/0001",
        "chapters/0002",
        "chapters/0003",
    ]
    first = json.loads((output / "chapters/0001/chapter.json").read_text())
    second = json.loads((output / "chapters/0002/chapter.json").read_text())
    last = json.loads((output / "chapters/0003/chapter.json").read_text())
    assert (first["start_content_idx"], first["end_content_idx"]) == (0, 2)
    assert (second["start_content_idx"], second["end_content_idx"]) == (3, 6)
    assert (last["start_content_idx"], last["end_content_idx"]) == (7, 7)
    assert first["pages"] == ["pages/page-0000.jpg"]
    assert second["pages"] == ["pages/page-0001.jpg"]
    assert last["pages"] == ["pages/page-0002.jpg"]
    assert second["assets"] == ["assets/abc-cover.png"]

    seed = (output / "chapters/0002/chapter.html").read_text(encoding="utf-8")
    assert 'id="content-00000003"' in seed
    assert 'data-content-idx="3"' in seed
    assert "Chapter &lt;One&gt;" in seed
    assert "Body &amp; &lt;text&gt;" in seed
    assert 'src="assets/abc-cover.png"' in seed
    assert "<table" in (output / "chapters/0001/chapter.html").read_text()
    assert "5 &lt; 6" in (output / "chapters/0001/chapter.html").read_text()
    assert "source.pdf" not in seed
    assert "source.jpg" not in seed
    assert "render-page" not in seed

    image = pymupdf.Pixmap(str(output / "chapters/0002/pages/page-0001.jpg"))
    assert image.width > 100 and image.height > 120
    assert image.width / image.height == pytest.approx(100 / 120, rel=0.02)
    assert (
        output / "chapters/0002/assets/abc-cover.png"
    ).read_bytes() == b"synthetic image"


def test_fresh_skip_and_force_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _write_inputs(tmp_path)
    output = build_chapter_workspace(work)
    before = (output / "manifest.json").read_bytes()

    def fail_render(*args: object, **kwargs: object) -> object:
        raise AssertionError("fresh output rendered again")

    monkeypatch.setattr(workspace, "_render_base_page", fail_render)
    assert build_chapter_workspace(work) == output
    assert (output / "manifest.json").read_bytes() == before

    monkeypatch.undo()
    build_chapter_workspace(work, force=True)
    assert (output / "manifest.json").read_bytes() == before


def test_rejects_unsafe_table_and_bad_bbox(tmp_path: Path) -> None:
    work = _write_inputs(tmp_path)
    content_path = work / "02_content/content.json"
    content = json.loads(content_path.read_text())
    content["items"][2]["html"] = "<table><script>alert(1)</script></table>"
    content["items_sha256"] = _sha256_json(content["items"])
    content_path.write_text(json.dumps(content), encoding="utf-8")
    chapters_path = work / "03_chapters/chapters.json"
    chapter_plan = json.loads(chapters_path.read_text())
    chapter_plan["source_content_sha256"] = _content_source_sha256(
        content["items"], content["page_geometry"]
    )
    chapters_path.write_text(json.dumps(chapter_plan), encoding="utf-8")
    with pytest.raises(ChapterWorkspaceError, match="unsafe table tag"):
        build_chapter_workspace(work)

    content["items"][2]["html"] = "<table><tbody><tr><td>safe</td></tr></tbody></table>"
    content["items"][1]["bbox"] = [5, 25, 120, 35]
    content["items_sha256"] = _sha256_json(content["items"])
    content_path.write_text(json.dumps(content), encoding="utf-8")
    chapter_plan["source_content_sha256"] = _content_source_sha256(
        content["items"], content["page_geometry"]
    )
    chapters_path.write_text(json.dumps(chapter_plan), encoding="utf-8")
    with pytest.raises(ChapterWorkspaceError, match="outside layout page"):
        build_chapter_workspace(work)


def test_atomic_publish_failure_preserves_old_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _write_inputs(tmp_path)
    output = build_chapter_workspace(work)
    before = (output / "manifest.json").read_bytes()
    original_replace = workspace.os.replace
    failed = False

    def fail_publish(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination) == output and not failed:
            failed = True
            raise OSError("publish failed")
        original_replace(source, destination)

    monkeypatch.setattr(workspace.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        build_chapter_workspace(work, force=True)
    assert (output / "manifest.json").read_bytes() == before


def test_rejects_symlink_asset(tmp_path: Path) -> None:
    work = _write_inputs(tmp_path)
    asset = work / "02_content/assets/abc-cover.png"
    real = tmp_path / "real.png"
    real.write_bytes(asset.read_bytes())
    asset.unlink()
    asset.symlink_to(real)
    with pytest.raises(ChapterWorkspaceError, match="regular file"):
        build_chapter_workspace(work)


def test_leading_non_text_items_join_first_boundary_workspace(tmp_path: Path) -> None:
    work = _write_inputs(tmp_path)
    content_path = work / "02_content/content.json"
    content = json.loads(content_path.read_text())
    content["items"][0].update({"type": "image", "text": ""})
    content["items"][1].update({"type": "image", "text": ""})
    content["items_sha256"] = _sha256_json(content["items"])
    content_path.write_text(json.dumps(content), encoding="utf-8")

    chapters_path = work / "03_chapters/chapters.json"
    plan = json.loads(chapters_path.read_text())
    plan["boundaries"] = plan["boundaries"][1:]
    plan["source_content_sha256"] = _content_source_sha256(
        content["items"], content["page_geometry"]
    )
    chapters_path.write_text(json.dumps(plan), encoding="utf-8")

    output = build_chapter_workspace(work)
    first = json.loads((output / "chapters/0001/chapter.json").read_text())
    chapter_html = (output / "chapters/0001/chapter.html").read_text(encoding="utf-8")
    assert first["start_content_idx"] == 3
    assert first["title"] == "Chapter <One>"
    assert 'id="content-00000000"' in chapter_html
    assert 'id="content-00000003"' in chapter_html


def test_multi_resource_items_render_sorted_all_references(tmp_path: Path) -> None:
    work = _write_inputs(tmp_path)
    content_path = work / "02_content/content.json"
    content = json.loads(content_path.read_text())
    content_dir = content_path.parent
    for name, data in (("assets/z.png", b"z"), ("assets/a.png", b"a")):
        (content_dir / name).write_bytes(data)
        content["assets"][name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    content["items"][5]["img_path"] = ["assets/z.png", "assets/a.png"]
    content["items_sha256"] = _sha256_json(content["items"])
    content_path.write_text(json.dumps(content), encoding="utf-8")
    chapters_path = work / "03_chapters/chapters.json"
    plan = json.loads(chapters_path.read_text())
    plan["source_content_sha256"] = _content_source_sha256(
        content["items"], content["page_geometry"]
    )
    chapters_path.write_text(json.dumps(plan), encoding="utf-8")

    output = build_chapter_workspace(work)
    chapter_html = (output / "chapters/0002/chapter.html").read_text(encoding="utf-8")
    assert chapter_html.index('src="assets/a.png"') < chapter_html.index(
        'src="assets/z.png"'
    )
    assert chapter_html.count("<img ") == 2


@pytest.mark.parametrize(
    "table_html",
    [
        "<table><td>orphan cell</td></table>",
        "<table><tbody><tr><div>wrong parent</div></tr></tbody></table>",
        "<table><tbody><tr><td onclick='alert(1)'>active</td></tr></tbody></table>",
        "<table><tbody><tr><td><a href='https://example.test'>url</a></td></tr></tbody></table>",
    ],
)
def test_table_sanitizer_rejects_invalid_content_models(
    tmp_path: Path, table_html: str
) -> None:
    work = _write_inputs(tmp_path)
    content_path = work / "02_content/content.json"
    content = json.loads(content_path.read_text())
    content["items"][2]["html"] = table_html
    content["items_sha256"] = _sha256_json(content["items"])
    content_path.write_text(json.dumps(content), encoding="utf-8")
    chapters_path = work / "03_chapters/chapters.json"
    plan = json.loads(chapters_path.read_text())
    plan["source_content_sha256"] = _content_source_sha256(
        content["items"], content["page_geometry"]
    )
    chapters_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ChapterWorkspaceError):
        build_chapter_workspace(work)


def test_damaged_manifest_and_unsafe_hash_paths_force_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _write_inputs(tmp_path)
    output = build_chapter_workspace(work)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files_sha256"].pop(next(iter(manifest["files_sha256"])))
    manifest["files_sha256"]["../outside"] = "a" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert workspace._safe_tree_hash(output, "../outside") is None

    monkeypatch.setattr(
        workspace,
        "_render_base_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("damaged manifest must rebuild")
        ),
    )
    with pytest.raises(AssertionError, match="must rebuild"):
        build_chapter_workspace(work)


def test_workspace_limits_dpi_pixels_json_and_output(
    tmp_path: Path, monkeypatch
) -> None:
    work = _write_inputs(tmp_path)
    with pytest.raises(ChapterWorkspaceError, match="dpi"):
        build_chapter_workspace(work, dpi=1)
    with pytest.raises(ChapterWorkspaceError, match="dpi"):
        build_chapter_workspace(work, dpi=301)
    with pytest.raises(ChapterWorkspaceError, match="pixel limit"):
        build_chapter_workspace(work, max_render_pixels_per_page=1)
    with pytest.raises(ChapterWorkspaceError, match="total output limit"):
        build_chapter_workspace(work, max_total_output_bytes=1)

    content_path = work / "02_content/content.json"
    content_path.write_text(content_path.read_text() + " " * 32, encoding="utf-8")
    monkeypatch.setattr(workspace, "MAX_INPUT_JSON_BYTES", 1)
    with pytest.raises(ChapterWorkspaceError, match="size limit"):
        build_chapter_workspace(work)


def test_workspace_requires_non_empty_source_pdf_sha256(tmp_path: Path) -> None:
    work = _write_inputs(tmp_path)
    content_path = work / "02_content/content.json"
    content = json.loads(content_path.read_text())
    content["source_pdf_sha256"] = ""
    content_path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(ChapterWorkspaceError, match="source_pdf_sha256"):
        build_chapter_workspace(work)


def test_pixel_limit_is_checked_before_pixmap_allocation() -> None:
    class FakePage:
        rect = pymupdf.Rect(0, 0, 100, 120)

        def get_pixmap(self, **kwargs: object) -> object:
            raise AssertionError("get_pixmap must not be called")

    class FakeDocument:
        def __getitem__(self, index: int) -> FakePage:
            assert index == 0
            return FakePage()

    with pytest.raises(ChapterWorkspaceError, match="pixel limit"):
        workspace._render_base_page(
            FakeDocument(),  # type: ignore[arg-type]
            0,
            dpi=150,
            max_render_pixels=1,
        )


def test_declared_asset_total_is_checked_before_asset_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _write_inputs(tmp_path)
    real_read = workspace._read_regular_bytes
    asset_reads = 0

    def track_asset_reads(path: Path, label: str, *, max_bytes=None) -> bytes:
        nonlocal asset_reads
        if label.startswith("content asset"):
            asset_reads += 1
        return real_read(path, label, max_bytes=max_bytes)

    monkeypatch.setattr(workspace, "_read_regular_bytes", track_asset_reads)
    with pytest.raises(ChapterWorkspaceError, match="declared assets"):
        build_chapter_workspace(work, max_total_asset_bytes=1)
    assert asset_reads == 0


def test_asset_copy_uses_the_validated_asset_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _write_inputs(tmp_path)
    real_read = workspace._read_regular_bytes
    mutated = False

    def mutate_after_read(path: Path, label: str, *, max_bytes=None) -> bytes:
        nonlocal mutated
        data = real_read(path, label, max_bytes=max_bytes)
        if label == "content asset assets/abc-cover.png" and not mutated:
            mutated = True
            path.write_bytes(b"changed after validation")
        return data

    monkeypatch.setattr(workspace, "_read_regular_bytes", mutate_after_read)
    output = build_chapter_workspace(work)

    assert mutated
    assert (output / "chapters/0002/assets/abc-cover.png").read_bytes() == (
        b"synthetic image"
    )


def test_pdf_render_uses_the_same_snapshot_as_hash_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _write_inputs(tmp_path)
    source_pdf = work / "source/source.pdf"
    original = source_pdf.read_bytes()
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(original + b"changed path contents")
    real_read = workspace._read_regular_bytes
    mutated = False

    def mutate_after_pdf_read(path: Path, label: str, *, max_bytes=None) -> bytes:
        nonlocal mutated
        data = real_read(path, label, max_bytes=max_bytes)
        if label == "source PDF" and not mutated:
            mutated = True
            source_pdf.write_bytes(replacement.read_bytes())
        return data

    monkeypatch.setattr(workspace, "_read_regular_bytes", mutate_after_pdf_read)
    output = build_chapter_workspace(work)

    assert mutated
    assert output.is_dir()
    assert (
        hashlib.sha256(source_pdf.read_bytes()).hexdigest()
        != (
            json.loads((output / "manifest.json").read_text())["source_fingerprints"][
                "source_pdf_sha256"
            ]
        )
    )


def test_shared_page_boundary_renders_one_full_page_for_both_chapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _write_inputs(tmp_path)
    chapters_path = work / "03_chapters/chapters.json"
    plan = json.loads(chapters_path.read_text())
    plan["boundaries"][1].update(
        {
            "title": "Body & <text>",
            "start_content_idx": 4,
            "start_page_idx": 1,
        }
    )
    chapters_path.write_text(json.dumps(plan), encoding="utf-8")
    real_render = workspace._render_base_page
    rendered_pages: list[int] = []

    def count_render(document, page_idx, **kwargs):
        rendered_pages.append(page_idx)
        return real_render(document, page_idx, **kwargs)

    monkeypatch.setattr(workspace, "_render_base_page", count_render)
    output = build_chapter_workspace(work)

    assert rendered_pages == [0, 1, 2]
    first = json.loads((output / "chapters/0001/chapter.json").read_text())
    second = json.loads((output / "chapters/0002/chapter.json").read_text())
    assert first["pages"] == ["pages/page-0000.jpg", "pages/page-0001.jpg"]
    assert second["pages"] == ["pages/page-0001.jpg"]


def test_successful_workspace_publish_survives_backup_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    work = _write_inputs(tmp_path)
    output = build_chapter_workspace(work)
    real_remove = workspace._remove_path

    def fail_backup_cleanup(path: Path) -> None:
        if ".backup-" in path.name:
            raise OSError("backup cleanup failed")
        real_remove(path)

    monkeypatch.setattr(workspace, "_remove_path", fail_backup_cleanup)
    build_chapter_workspace(work, force=True)

    assert output.is_dir()
    assert any("backup" in record.message for record in caplog.records)


def test_rotated_cropbox_page_uses_displayed_pixmap_and_layout_geometry(
    tmp_path: Path,
) -> None:
    work = _write_inputs(tmp_path)
    source_pdf = work / "source/source.pdf"
    replacement = tmp_path / "rotated.pdf"
    with pymupdf.open(str(source_pdf)) as document:
        document[0].set_cropbox(pymupdf.Rect(10, 20, 90, 110))
        document[0].set_rotation(90)
        document.save(str(replacement))
    source_pdf.write_bytes(replacement.read_bytes())

    content_path = work / "02_content/content.json"
    content = json.loads(content_path.read_text())
    content["source_pdf_sha256"] = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    content["page_geometry"][0] = {
        "page_idx": 0,
        "width": 90.0,
        "height": 80.0,
    }
    content["items"][0]["bbox"] = [10, 20, 30, 40]
    content["items_sha256"] = _sha256_json(content["items"])
    content_path.write_text(json.dumps(content), encoding="utf-8")
    chapters_path = work / "03_chapters/chapters.json"
    plan = json.loads(chapters_path.read_text())
    plan["source_content_sha256"] = _content_source_sha256(
        content["items"], content["page_geometry"]
    )
    chapters_path.write_text(json.dumps(plan), encoding="utf-8")

    output = build_chapter_workspace(work)
    pixmap = pymupdf.Pixmap(str(output / "chapters/0001/pages/page-0000.jpg"))
    assert pixmap.width / pixmap.height == pytest.approx(90 / 80, rel=0.03)
    scale = 150 / 72
    expected = {
        "left": round(10 * scale),
        "top": round(20 * scale),
        "right": round(30 * scale),
        "bottom": round(40 * scale),
    }

    def is_body_pixel(x: int, y: int) -> bool:
        red, green, blue = pixmap.pixel(x, y)[:3]
        return green > red + 20 and green > blue + 10

    assert any(
        is_body_pixel(x, expected["top"])
        for x in range(expected["left"] - 2, expected["right"] + 3)
    )
    assert any(
        is_body_pixel(expected["left"], y)
        for y in range(expected["top"] - 2, expected["bottom"] + 3)
    )
    assert all(
        not is_body_pixel(x, y)
        for x, y in ((5, 5), (pixmap.width - 6, pixmap.height - 6))
    )


def test_workspace_bytes_are_stable_across_python_hash_seeds(tmp_path: Path) -> None:
    work = _write_inputs(tmp_path)
    script = (
        "import sys; "
        "from epubforge.chapter_workspace import build_chapter_workspace; "
        "build_chapter_workspace(sys.argv[1], output_dir=sys.argv[2])"
    )
    outputs: list[Path] = []
    for seed in ("1", "987"):
        output = tmp_path / f"output-{seed}"
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        subprocess.run(
            [sys.executable, "-c", script, str(work), str(output)],
            check=True,
            cwd=str(Path(__file__).parents[1]),
            env=environment,
        )
        outputs.append(output)
    first_files = {
        path.relative_to(outputs[0]): path.read_bytes()
        for path in outputs[0].rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(outputs[1]): path.read_bytes()
        for path in outputs[1].rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
