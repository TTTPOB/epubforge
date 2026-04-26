"""Tests for Stage 1 dual-pipeline orchestration (Phase 1 / I3).

Verifies ``pipeline.run_parse`` behaviour when ``cfg.extract.granite.enabled``
toggles, including success/partial-failure/exception/skip/force scenarios.
The Granite VLM call itself is mocked — we never spin up llama-server here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

import pytest

from epubforge.config import (
    Config,
    ExtractSettings,
    GraniteSettings,
    RuntimeSettings,
)
from epubforge.parser.granite_parser import GraniteParseResult
from epubforge.pipeline import run_parse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path: Path, *, granite_enabled: bool) -> Config:
    """Build a Config rooted at ``tmp_path/work`` with an optional Granite enable."""
    return Config(
        runtime=RuntimeSettings(work_dir=tmp_path / "work"),
        extract=ExtractSettings(
            granite=GraniteSettings(enabled=granite_enabled, health_check=False),
        ),
    )


def _install_fake_standard(
    monkeypatch: pytest.MonkeyPatch, *, page_count: int = 3
) -> list[Path]:
    """Patch ``parse_pdf`` to write a minimal docling JSON with N synthetic pages.

    Returns a list that records every PDF path the fake was called with.
    """
    calls: list[Path] = []

    def fake_parse_pdf(
        pdf_path: Path, out_path: Path, *, images_dir: Path, **kwargs: Any
    ) -> None:
        calls.append(pdf_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Match the real DoclingDocument JSON shape: pages keyed by stringified
        # 1-based page numbers. Empty inner dicts are fine for _peek_page_count.
        payload = {"pages": {str(i): {} for i in range(1, page_count + 1)}}
        out_path.write_text(json.dumps(payload), encoding="utf-8")
        images_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("epubforge.parser.docling_parser.parse_pdf", fake_parse_pdf)
    return calls


def _install_fake_granite(
    monkeypatch: pytest.MonkeyPatch,
    *,
    successful_pages: list[int] | None = None,
    failed_pages: list[int] | None = None,
    elapsed_seconds: float = 1.5,
    raise_exc: BaseException | None = None,
) -> list[dict[str, Any]]:
    """Patch ``parse_pdf_granite`` (as imported by pipeline) with a stub.

    Returns a list of recorded call kwargs so tests can assert the call.
    """
    calls: list[dict[str, Any]] = []

    def fake_parse_pdf_granite(
        pdf_path: Path,
        out_path: Path,
        *,
        settings: Any,
        page_count: int,
        on_progress: Callable[[int, int, float], None] | None = None,
    ) -> GraniteParseResult:
        calls.append(
            {
                "pdf_path": pdf_path,
                "out_path": out_path,
                "settings": settings,
                "page_count": page_count,
                "on_progress": on_progress,
            }
        )
        if raise_exc is not None:
            raise raise_exc
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("{}", encoding="utf-8")
        succ = (
            successful_pages
            if successful_pages is not None
            else list(range(1, page_count + 1))
        )
        fails = failed_pages or []
        return GraniteParseResult(
            successful_pages=succ,
            failed_pages=fails,
            elapsed_seconds=elapsed_seconds,
            out_path=out_path,
            page_count=page_count,
        )

    monkeypatch.setattr("epubforge.pipeline.parse_pdf_granite", fake_parse_pdf_granite)
    return calls


def _make_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfake bytes\n")
    return pdf


# ---------------------------------------------------------------------------
# Scenarios from issue epubforge-iqpc
# ---------------------------------------------------------------------------


def test_granite_disabled_runs_only_standard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 1: granite.enabled=False — standard runs, no Granite call, no granite output."""
    pdf = _make_pdf(tmp_path)
    cfg = _make_cfg(tmp_path, granite_enabled=False)
    _install_fake_standard(monkeypatch)
    granite_calls = _install_fake_granite(monkeypatch)

    run_parse(pdf, cfg)

    work = cfg.book_work_dir(pdf)
    assert (work / "01_raw.json").is_file()
    assert not (work / "01_raw_granite.json").exists()
    assert granite_calls == []


def test_granite_enabled_success_writes_both_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario 2: granite.enabled=True + Granite success — both JSON files exist."""
    pdf = _make_pdf(tmp_path)
    cfg = _make_cfg(tmp_path, granite_enabled=True)
    _install_fake_standard(monkeypatch, page_count=3)
    granite_calls = _install_fake_granite(monkeypatch)

    caplog.set_level(logging.INFO, logger="epubforge.pipeline")
    run_parse(pdf, cfg)

    work = cfg.book_work_dir(pdf)
    assert (work / "01_raw.json").is_file()
    assert (work / "01_raw_granite.json").is_file()
    assert len(granite_calls) == 1
    assert granite_calls[0]["page_count"] == 3
    assert granite_calls[0]["out_path"] == work / "01_raw_granite.json"
    # Settings must be the GraniteSettings instance from cfg.extract.granite
    assert granite_calls[0]["settings"] is cfg.extract.granite
    # Success summary log is emitted
    assert "Granite parse complete" in caplog.text


def test_granite_enabled_exception_does_not_abort_stage1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario 3: Granite raises — Stage 1 still succeeds, only 01_raw.json exists."""
    pdf = _make_pdf(tmp_path)
    cfg = _make_cfg(tmp_path, granite_enabled=True)
    _install_fake_standard(monkeypatch)
    _install_fake_granite(
        monkeypatch, raise_exc=RuntimeError("llama-server unreachable")
    )

    caplog.set_level(logging.WARNING, logger="epubforge.pipeline")
    # MUST NOT raise — Granite is the secondary pipeline.
    run_parse(pdf, cfg)

    work = cfg.book_work_dir(pdf)
    assert (work / "01_raw.json").is_file()
    assert not (work / "01_raw_granite.json").exists()
    assert "Granite parse failed (secondary pipeline, continuing)" in caplog.text
    assert "llama-server unreachable" in caplog.text


def test_granite_partial_failure_logs_failed_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario 4: Granite returns partial failure — Stage 1 succeeds; warning lists failed pages."""
    pdf = _make_pdf(tmp_path)
    cfg = _make_cfg(tmp_path, granite_enabled=True)
    _install_fake_standard(monkeypatch, page_count=10)
    _install_fake_granite(
        monkeypatch,
        successful_pages=[1, 2, 4, 5, 6, 8, 9, 10],
        failed_pages=[3, 7],
    )

    caplog.set_level(logging.WARNING, logger="epubforge.pipeline")
    run_parse(pdf, cfg)

    work = cfg.book_work_dir(pdf)
    assert (work / "01_raw.json").is_file()
    assert (work / "01_raw_granite.json").is_file()
    # Warning text must mention the failed page numbers
    assert "Granite failed pages" in caplog.text
    assert "[3, 7]" in caplog.text


def test_granite_skip_when_output_exists_and_not_forced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario 5: 01_raw_granite.json already exists and force=False — Granite is skipped."""
    pdf = _make_pdf(tmp_path)
    cfg = _make_cfg(tmp_path, granite_enabled=True)
    _install_fake_standard(monkeypatch)
    granite_calls = _install_fake_granite(monkeypatch)

    work = cfg.book_work_dir(pdf)
    work.mkdir(parents=True, exist_ok=True)
    (work / "01_raw_granite.json").write_text(
        '{"prior": true}', encoding="utf-8"
    )

    caplog.set_level(logging.INFO, logger="epubforge.pipeline")
    run_parse(pdf, cfg, force=False)

    # Granite must NOT be invoked
    assert granite_calls == []
    assert "Granite output exists; skipping" in caplog.text
    # Pre-existing granite content must be preserved
    assert (
        json.loads((work / "01_raw_granite.json").read_text(encoding="utf-8"))
        == {"prior": True}
    )


def test_granite_force_reruns_even_when_output_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 6: force=True — Granite is invoked even though 01_raw_granite.json exists."""
    pdf = _make_pdf(tmp_path)
    cfg = _make_cfg(tmp_path, granite_enabled=True)
    _install_fake_standard(monkeypatch, page_count=4)
    granite_calls = _install_fake_granite(monkeypatch)

    work = cfg.book_work_dir(pdf)
    work.mkdir(parents=True, exist_ok=True)
    (work / "01_raw_granite.json").write_text(
        '{"prior": true}', encoding="utf-8"
    )

    run_parse(pdf, cfg, force=True)

    # Granite must be re-invoked exactly once
    assert len(granite_calls) == 1
    # The fake overwrote the file with "{}"
    assert (
        (work / "01_raw_granite.json").read_text(encoding="utf-8") == "{}"
    )
    assert granite_calls[0]["page_count"] == 4
