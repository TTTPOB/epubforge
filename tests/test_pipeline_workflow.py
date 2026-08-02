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
from epubforge.chapter_revision import ChapterRevisionReport
from epubforge.cli import app
from epubforge.config import Config, ProviderSettings, RuntimeSettings


def _config(tmp_path: Path, *, api_key: str | None = "test-key") -> Config:
    return Config(
        llm=ProviderSettings(api_key=api_key, model="configured-model"),
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


def test_run_segment_requires_llm_credentials_before_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf_path(tmp_path)
    work = _config(tmp_path, api_key=None).book_work_dir(pdf)
    content = work / "02_content" / "content.json"
    content.parent.mkdir(parents=True)
    content.write_text("{}", encoding="utf-8")
    client_created = False

    class UnexpectedClient:
        def __init__(self, _cfg: Config) -> None:
            nonlocal client_created
            client_created = True

    monkeypatch.setattr(pipeline, "LLMClient", UnexpectedClient)
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.is_chapter_segmentation_fresh",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(SystemExit, match="LLM API key"):
        pipeline.run_segment(pdf, _config(tmp_path, api_key=None))
    assert client_created is False


def test_run_segment_passes_configured_model_and_force_to_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path)
    work = cfg.book_work_dir(pdf)
    content = work / "02_content" / "content.json"
    content.parent.mkdir(parents=True)
    content.write_text("{}", encoding="utf-8")
    calls: list[tuple[Path, Path, str, bool]] = []

    class FakeClient:
        model = "client-model"

        def __init__(self, _cfg: Config) -> None:
            pass

    def fake_segment(
        content_path: Path,
        output_dir: Path,
        client: FakeClient,
        *,
        force: bool,
    ) -> Path:
        calls.append((content_path, output_dir, client.model, force))
        return output_dir / "chapters.json"

    monkeypatch.setattr(pipeline, "LLMClient", FakeClient)
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.is_chapter_segmentation_fresh",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr("epubforge.chapter_segmentation.segment_chapters", fake_segment)

    pipeline.run_segment(pdf, cfg, force=True)

    assert calls == [(content, work / "03_chapters", "client-model", True)]


def test_run_segment_fresh_output_reuses_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path, api_key=None)
    work = cfg.book_work_dir(pdf)
    content = work / "02_content/content.json"
    content.parent.mkdir(parents=True)
    content.write_text("{}", encoding="utf-8")
    client_created = False
    segment_called = False

    class UnexpectedClient:
        def __init__(self, _cfg: Config) -> None:
            nonlocal client_created
            client_created = True
            raise AssertionError("fresh Stage 3 must not construct an LLM client")

    def unexpected_segment(*args: Any, **kwargs: Any) -> Path:
        nonlocal segment_called
        segment_called = True
        raise AssertionError("fresh Stage 3 must not call the provider module")

    monkeypatch.setattr(pipeline, "LLMClient", UnexpectedClient)
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.is_chapter_segmentation_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "epubforge.chapter_segmentation.segment_chapters", unexpected_segment
    )
    caplog.set_level(logging.INFO, logger="epubforge.pipeline")

    pipeline.run_segment(pdf, cfg)

    assert client_created is False
    assert segment_called is False
    assert "Stage 3 segment started" in caplog.text
    assert "Stage 3 segment done" in caplog.text


@pytest.mark.parametrize("force", [False, True])
def test_run_segment_stale_or_forced_output_requires_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path, api_key=None)
    content = cfg.book_work_dir(pdf) / "02_content/content.json"
    content.parent.mkdir(parents=True)
    content.write_text("{}", encoding="utf-8")
    if not force:
        monkeypatch.setattr(
            "epubforge.chapter_segmentation.is_chapter_segmentation_fresh",
            lambda *args, **kwargs: False,
        )

    with pytest.raises(SystemExit, match="LLM API key"):
        pipeline.run_segment(pdf, cfg, force=force)


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

    class FakeClient:
        model = "client-model"

        def __init__(self, _cfg: Config) -> None:
            pass

    def fake_revise(
        edit_path: Path, client: FakeClient, **kwargs: Any
    ) -> ChapterRevisionReport:
        calls.append({"edit_path": edit_path, "model": client.model, **kwargs})
        return report

    monkeypatch.setattr(pipeline, "LLMClient", FakeClient)
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
            "model": "client-model",
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
    cfg = _config(tmp_path, api_key=None)
    edit_dir = cfg.book_work_dir(pdf) / "04_edit"
    edit_dir.mkdir(parents=True)
    (edit_dir / "manifest.json").write_text("{}", encoding="utf-8")
    client_created = False
    revise_called = False

    class UnexpectedClient:
        def __init__(self, _cfg: Config) -> None:
            nonlocal client_created
            client_created = True
            raise AssertionError("fresh Stage 5 must not construct an LLM client")

    def unexpected_revise(*args: Any, **kwargs: Any) -> ChapterRevisionReport:
        nonlocal revise_called
        revise_called = True
        raise AssertionError("fresh Stage 5 must not call the provider module")

    monkeypatch.setattr(pipeline, "LLMClient", UnexpectedClient)
    monkeypatch.setattr(
        "epubforge.chapter_revision.is_chapter_revision_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "epubforge.chapter_revision.revise_all_chapters", unexpected_revise
    )
    caplog.set_level(logging.INFO, logger="epubforge.pipeline")

    pipeline.run_revise(pdf, cfg)

    assert client_created is False
    assert revise_called is False
    assert "Stage 5 revise started" in caplog.text
    assert "Stage 5 revise done" in caplog.text


@pytest.mark.parametrize("force", [False, True])
def test_run_revise_stale_or_forced_output_requires_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    pdf = _pdf_path(tmp_path)
    cfg = _config(tmp_path, api_key=None)
    edit_dir = cfg.book_work_dir(pdf) / "04_edit"
    edit_dir.mkdir(parents=True)
    (edit_dir / "manifest.json").write_text("{}", encoding="utf-8")
    if not force:
        monkeypatch.setattr(
            "epubforge.chapter_revision.is_chapter_revision_fresh",
            lambda *args, **kwargs: False,
        )

    with pytest.raises(SystemExit, match="LLM API key"):
        pipeline.run_revise(pdf, cfg, force=force)


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
        "[llm]\n"
        'api_key = "fake-key"\n'
        'model = "openai/gpt-5.6-luna"\n'
        f"\n[runtime]\nwork_dir = {str(work_root)!r}\n",
        encoding="utf-8",
    )
    parse_calls = 0

    def fake_parse(pdf_path: Path, cfg: Config, *, force: bool) -> None:
        del pdf_path, cfg, force
        nonlocal parse_calls
        parse_calls += 1

    class FakeLLM:
        model = "openai/gpt-5.6-luna"
        total_calls = 0

        def __init__(self, _cfg: Config) -> None:
            pass

        def chat_parsed(
            self,
            messages: list[dict[str, Any]],
            *,
            response_format: type[Any],
            validator=None,
            bypass_cache: bool = False,
        ) -> Any:
            del bypass_cache
            self.__class__.total_calls += 1
            if response_format.__name__ == "ChapterSegmentationResponse":
                response = response_format(
                    boundaries=[
                        {
                            "title": "Chapter One",
                            "kind": "chapter",
                            "start_content_idx": 0,
                            "start_page_idx": 0,
                            "confidence": 1.0,
                            "evidence": "The first heading starts the chapter.",
                        }
                    ]
                )
            else:
                html = messages[1]["content"][0]["text"].split("COMPLETE HTML:\n", 1)[1]
                response = response_format(corrected_html=html)
            if validator is not None:
                validator(response)
            return response

    monkeypatch.setattr(pipeline, "run_parse", fake_parse)
    monkeypatch.setattr(pipeline, "LLMClient", FakeLLM)

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
    assert FakeLLM.total_calls == 2

    corrected_before = (chapter_dir / "corrected.html").read_bytes()
    revision_before = (chapter_dir / "revision.json").read_bytes()

    class UnexpectedClient:
        def __init__(self, _cfg: Config) -> None:
            raise AssertionError("fresh rerun must remain offline")

    monkeypatch.setattr(pipeline, "LLMClient", UnexpectedClient)
    offline_config = tmp_path / "offline-config.toml"
    offline_config.write_text(
        "[llm]\n"
        'model = "openai/gpt-5.6-luna"\n'
        f"\n[runtime]\nwork_dir = {str(work_root)!r}\n",
        encoding="utf-8",
    )
    rerun = CliRunner().invoke(
        app,
        ["--config", str(offline_config), "run", str(pdf)],
    )

    assert rerun.exit_code == 0, rerun.output
    assert parse_calls == 2
    assert FakeLLM.total_calls == 2
    assert (chapter_dir / "corrected.html").read_bytes() == corrected_before
    assert (chapter_dir / "revision.json").read_bytes() == revision_before
