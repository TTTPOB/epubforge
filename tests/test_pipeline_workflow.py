"""Tests for the five-stage default chapter workflow."""

from __future__ import annotations

import logging
import io
import json
from pathlib import Path
from typing import Any
import zipfile

import pytest
import pymupdf
from typer.testing import CliRunner

from epubforge import pipeline
from epubforge.agent_runner import (
    AgentRunRequest,
    AgentRunResult,
    book_editor_identity,
)
from epubforge.chapter_revision import ChapterRevisionReport
from epubforge.cli import app
from epubforge.config import Config, RuntimeSettings


def _config(tmp_path: Path) -> Config:
    return Config(
        runtime=RuntimeSettings(work_dir=tmp_path / "work"),
    )


def _pdf_path(tmp_path: Path) -> Path:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"synthetic pdf")
    return pdf


def test_run_normalize_checks_both_stage_one_prerequisites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    called = False

    def fake_normalize(*args: Any, **kwargs: Any) -> Path:
        nonlocal called
        called = True
        return tmp_path / "content"

    monkeypatch.setattr(
        "epubforge.mineru_content.normalize_mineru_content", fake_normalize
    )

    with pytest.raises(RuntimeError, match="01_raw.zip"):
        pipeline.run_normalize(pdf, cfg)
    assert called is False


def test_run_normalize_emits_stage_timer_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    work = cfg.book_work_dir(pdf)
    source = work / "source/source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(pdf.read_bytes())
    (work / "01_raw.zip").write_bytes(b"archive")

    monkeypatch.setattr(
        "epubforge.mineru_content.normalize_mineru_content",
        lambda *args, **kwargs: work / "02_content",
    )
    caplog.set_level(logging.INFO, logger="epubforge.pipeline")

    pipeline.run_normalize(pdf, cfg)

    assert "Stage 2: normalizing MinerU content" in caplog.text
    assert "Stage 2 normalize started" in caplog.text
    assert "Stage 2 normalize done" in caplog.text


def test_run_segment_constructs_runner_after_freshness_and_passes_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    work = cfg.book_work_dir(pdf)
    content = work / "02_content" / "content.json"
    content.parent.mkdir(parents=True)
    content.write_text("{}", encoding="utf-8")
    calls: list[tuple[Path, Path, bool]] = []
    order: list[str] = []

    class FakeRunner:
        identity = book_editor_identity()

    runner = FakeRunner()

    def fake_fresh(*args: Any, **kwargs: Any) -> bool:
        assert kwargs["agent_identity"] == runner.identity
        order.append("freshness")
        return False

    def make_runner() -> FakeRunner:
        order.append("construct")
        return runner

    def fake_segment(
        content_path: Path,
        output_dir: Path,
        agent_runner: FakeRunner,
        *,
        force: bool,
    ) -> Path:
        assert agent_runner is runner
        order.append("segment")
        calls.append((content_path, output_dir, force))
        return output_dir / "chapters.json"

    monkeypatch.setattr(pipeline, "book_editor_identity", lambda: runner.identity)
    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", make_runner)
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.is_chapter_segmentation_fresh",
        fake_fresh,
    )
    monkeypatch.setattr("epubforge.chapter_segmentation.segment_chapters", fake_segment)

    pipeline.run_segment(pdf, cfg)

    assert order == ["freshness", "construct", "segment"]
    assert calls == [(content, work / "03_chapters", False)]


def test_run_segment_fresh_output_reuses_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    work = cfg.book_work_dir(pdf)
    content = work / "02_content/content.json"
    content.parent.mkdir(parents=True)
    content.write_text("{}", encoding="utf-8")
    runner_created = False
    segment_called = False

    def unexpected_runner() -> None:
        nonlocal runner_created
        runner_created = True
        raise AssertionError("fresh Stage 3 must not construct an agent runner")

    def unexpected_segment(*args: Any, **kwargs: Any) -> Path:
        nonlocal segment_called
        segment_called = True
        raise AssertionError("fresh Stage 3 must not call the agent module")

    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", unexpected_runner)
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.is_chapter_segmentation_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.segment_chapters", unexpected_segment
    )
    caplog.set_level(logging.INFO, logger="epubforge.pipeline")

    pipeline.run_segment(pdf, cfg)

    assert runner_created is False
    assert segment_called is False
    assert "Stage 3 segment started" in caplog.text
    assert "Stage 3 segment done" in caplog.text


def test_run_segment_force_skips_freshness_but_constructs_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    content = cfg.book_work_dir(pdf) / "02_content/content.json"
    content.parent.mkdir(parents=True)
    content.write_text("{}", encoding="utf-8")
    calls: list[bool] = []

    class FakeRunner:
        identity = book_editor_identity()

    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", FakeRunner)
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.is_chapter_segmentation_fresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("forced Stage 3 must skip freshness")
        ),
    )
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.segment_chapters",
        lambda *args, **kwargs: calls.append(kwargs["force"]),
    )

    pipeline.run_segment(pdf, cfg, force=True)

    assert calls == [True]


def test_run_revise_raises_with_report_failures_and_keeps_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    work = cfg.book_work_dir(pdf)
    edit_dir = work / "04_edit"
    edit_dir.mkdir(parents=True)
    (edit_dir / "manifest.json").write_text("{}", encoding="utf-8")
    failed_chapter = edit_dir / "chapters/0002"
    report = ChapterRevisionReport(
        completed=[edit_dir / "chapters/0001"],
        failed=[failed_chapter],
        errors={failed_chapter: "provider unavailable"},
    )
    calls: list[dict[str, Any]] = []

    class FakeRunner:
        identity = book_editor_identity()

    runner = FakeRunner()

    def fake_revise(
        edit_path: Path, agent_runner: FakeRunner, **kwargs: Any
    ) -> ChapterRevisionReport:
        assert agent_runner is runner
        calls.append({"edit_path": edit_path, **kwargs})
        return report

    monkeypatch.setattr(pipeline, "book_editor_identity", lambda: runner.identity)
    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", lambda: runner)
    monkeypatch.setattr(
        "epubforge.chapter_revision.is_chapter_revision_fresh",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr("epubforge.chapter_revision.revise_all_chapters", fake_revise)

    with pytest.raises(
        RuntimeError, match="successful chapters were preserved"
    ) as exc_info:
        pipeline.run_revise(pdf, cfg)

    assert str(failed_chapter) in str(exc_info.value)
    assert calls == [
        {
            "edit_path": edit_dir,
            "force": False,
            "continue_on_error": False,
        }
    ]


def test_run_revise_fresh_output_reuses_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    edit_dir = cfg.book_work_dir(pdf) / "04_edit"
    edit_dir.mkdir(parents=True)
    (edit_dir / "manifest.json").write_text("{}", encoding="utf-8")
    runner_created = False
    revise_called = False

    def unexpected_runner() -> None:
        nonlocal runner_created
        runner_created = True
        raise AssertionError("fresh Stage 5 must not construct an agent runner")

    def unexpected_revise(*args: Any, **kwargs: Any) -> ChapterRevisionReport:
        nonlocal revise_called
        revise_called = True
        raise AssertionError("fresh Stage 5 must not call the agent module")

    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", unexpected_runner)
    monkeypatch.setattr(
        "epubforge.chapter_revision.is_chapter_revision_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "epubforge.chapter_revision.revise_all_chapters", unexpected_revise
    )
    caplog.set_level(logging.INFO, logger="epubforge.pipeline")

    pipeline.run_revise(pdf, cfg)

    assert runner_created is False
    assert revise_called is False
    assert "Stage 5 revise started" in caplog.text
    assert "Stage 5 revise done" in caplog.text


def test_run_revise_force_skips_freshness_and_needs_no_epubforge_llm_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    edit_dir = cfg.book_work_dir(pdf) / "04_edit"
    edit_dir.mkdir(parents=True)
    (edit_dir / "manifest.json").write_text("{}", encoding="utf-8")
    calls: list[bool] = []

    class FakeRunner:
        identity = book_editor_identity()

    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", FakeRunner)
    monkeypatch.setattr(
        "epubforge.chapter_revision.is_chapter_revision_fresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("forced Stage 5 must skip freshness")
        ),
    )
    monkeypatch.setattr(
        "epubforge.chapter_revision.revise_all_chapters",
        lambda *args, **kwargs: calls.append(kwargs["force"])
        or ChapterRevisionReport(),
    )

    pipeline.run_revise(pdf, cfg, force=True)

    assert calls == [True]


def test_new_cli_commands_and_run_help() -> None:
    runner = CliRunner()

    for command in ("normalize", "segment", "prepare", "revise"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--force-rerun" in result.output

    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "1–5" in result.output
    assert "--pages" not in result.output


def test_cli_run_vertical_workflow_writes_and_reuses_real_chapter_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "book.pdf"
    document = pymupdf.open()
    try:
        page = document.new_page(width=100, height=120)
        page.insert_text((10, 20), "Chapter One")
        page.insert_text((10, 40), "Body text")
        document.save(str(pdf))
    finally:
        document.close()

    work_root = tmp_path / "work"
    work = work_root / "book"
    source_pdf = work / "source/source.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(pdf.read_bytes())
    raw_members = {
        "book_content_list.json": json.dumps(
            [
                {
                    "type": "text",
                    "text_level": 1,
                    "text": "Chapter One",
                    "page_idx": 0,
                    "bbox": [10, 10, 90, 25],
                },
                {
                    "type": "text",
                    "text_level": 0,
                    "text": "Body text",
                    "page_idx": 0,
                    "bbox": [10, 30, 90, 45],
                },
            ]
        ).encode("utf-8"),
        "layout.json": json.dumps(
            {"pdf_info": [{"page_idx": 0, "page_size": [100, 120]}]}
        ).encode("utf-8"),
    }
    raw_buffer = io.BytesIO()
    with zipfile.ZipFile(raw_buffer, mode="w") as archive:
        for name, content in raw_members.items():
            archive.writestr(name, content)
    (work / "01_raw.zip").write_bytes(raw_buffer.getvalue())

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[runtime]\nwork_dir = {str(work_root)!r}\n",
        encoding="utf-8",
    )
    parse_calls = 0

    def fake_parse(pdf_path: Path, cfg: Config, *, force: bool) -> None:
        del pdf_path, cfg, force
        nonlocal parse_calls
        parse_calls += 1

    class FakeAgentRunner:
        identity = book_editor_identity()
        total_calls = 0

        def __call__(self, request: AgentRunRequest) -> AgentRunResult:
            self.__class__.total_calls += 1
            if "boundaries.json" in request.output_limits:
                output = json.dumps(
                    {
                        "boundaries": [
                            {
                                "title": "Chapter One",
                                "kind": "chapter",
                                "start_content_idx": 0,
                                "start_page_idx": 0,
                                "confidence": 1.0,
                                "evidence": "The first heading starts the chapter.",
                            }
                        ]
                    }
                ).encode("utf-8")
                outputs = {"boundaries.json": output}
            else:
                outputs = {"corrected.html": request.files["corrected.html"]}
            return AgentRunResult(outputs=outputs, session_id="ses_vertical_test")

    monkeypatch.setattr(pipeline, "run_parse", fake_parse)
    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", FakeAgentRunner)

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "run", str(pdf)],
    )

    assert result.exit_code == 0, result.output
    normalized = work / "02_content/content.json"
    chapter_plan = work / "03_chapters/chapters.json"
    workspace_manifest = work / "04_edit/manifest.json"
    chapter_dir = work / "04_edit/chapters/0001"
    assert normalized.is_file()
    assert chapter_plan.is_file()
    assert workspace_manifest.is_file()
    assert (chapter_dir / "chapter.html").is_file()
    assert (chapter_dir / "pages/page-0000.jpg").is_file()
    assert (chapter_dir / "corrected.html").is_file()
    assert (chapter_dir / "revision.json").is_file()
    assert parse_calls == 1
    assert FakeAgentRunner.total_calls == 2

    corrected_before = (chapter_dir / "corrected.html").read_bytes()
    revision_before = (chapter_dir / "revision.json").read_bytes()

    class UnexpectedRunner:
        def __init__(self) -> None:
            raise AssertionError("fresh rerun must not construct an agent runner")

    monkeypatch.setattr(pipeline, "OpenCodeAgentRunner", UnexpectedRunner)
    offline_config = tmp_path / "offline-config.toml"
    offline_config.write_text(
        f"[runtime]\nwork_dir = {str(work_root)!r}\n",
        encoding="utf-8",
    )
    rerun = CliRunner().invoke(
        app,
        ["--config", str(offline_config), "run", str(pdf)],
    )

    assert rerun.exit_code == 0, rerun.output
    assert parse_calls == 2
    assert FakeAgentRunner.total_calls == 2
    assert (chapter_dir / "corrected.html").read_bytes() == corrected_before
    assert (chapter_dir / "revision.json").read_bytes() == revision_before
