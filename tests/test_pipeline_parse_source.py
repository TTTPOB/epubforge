from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

import pytest

from epubforge.config import Config, RuntimeSettings
from epubforge.pipeline import run_parse


def test_run_parse_persists_source_pdf_and_uses_it_for_docling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source_pdf = tmp_path / "input.pdf"
    source_pdf.write_bytes(b"%PDF-1.7\nstable source bytes\n")
    cfg = Config(runtime=RuntimeSettings(work_dir=tmp_path / "work"))
    calls: list[Path] = []

    def fake_parse_pdf(pdf_path: Path, out_path: Path, *, images_dir: Path, **kwargs) -> None:
        calls.append(pdf_path)
        out_path.write_text('{"pages": {}}', encoding="utf-8")

    monkeypatch.setattr("epubforge.parser.docling_parser.parse_pdf", fake_parse_pdf)

    caplog.set_level(logging.INFO, logger="epubforge.pipeline")
    run_parse(source_pdf, cfg)

    work = cfg.book_work_dir(source_pdf)
    persisted_pdf = work / "source" / "source.pdf"
    meta_path = work / "source" / "source_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    assert calls == [persisted_pdf]
    assert persisted_pdf.read_bytes() == source_pdf.read_bytes()
    assert meta["source_pdf"] == "source/source.pdf"
    assert meta["original_pdf_abs"] == str(source_pdf.resolve())
    assert meta["size_bytes"] == source_pdf.stat().st_size
    assert meta["sha256"] == hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    assert meta["sha256"] == hashlib.sha256(persisted_pdf.read_bytes()).hexdigest()
    assert isinstance(meta["copied_at"], str)
    log_text = caplog.text
    assert str(source_pdf.resolve()) in log_text
    assert str(persisted_pdf) in log_text
    assert meta["sha256"] in log_text


def test_run_parse_falls_back_to_copy2_when_hardlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_pdf = tmp_path / "input.pdf"
    source_pdf.write_bytes(b"%PDF-1.7\ncopy fallback bytes\n")
    cfg = Config(runtime=RuntimeSettings(work_dir=tmp_path / "work"))
    copied: list[tuple[Path, Path]] = []

    def fake_parse_pdf(pdf_path: Path, out_path: Path, *, images_dir: Path, **kwargs) -> None:
        out_path.write_text('{"pages": {}}', encoding="utf-8")

    def fake_link(src: Path, dst: Path) -> None:
        raise OSError("cross-device link")

    def fake_copy2(src: Path, dst: Path) -> Path:
        copied.append((src, dst))
        return shutil.copyfile(src, dst)

    monkeypatch.setattr("epubforge.parser.docling_parser.parse_pdf", fake_parse_pdf)
    monkeypatch.setattr("epubforge.pipeline.os.link", fake_link)
    monkeypatch.setattr("epubforge.pipeline.shutil.copy2", fake_copy2)

    run_parse(source_pdf, cfg)

    persisted_pdf = cfg.book_work_dir(source_pdf) / "source" / "source.pdf"
    assert copied == [(source_pdf.resolve(), persisted_pdf)]
    assert persisted_pdf.read_bytes() == source_pdf.read_bytes()


def test_run_parse_existing_output_requires_stable_source_artifacts(tmp_path: Path) -> None:
    source_pdf = tmp_path / "input.pdf"
    source_pdf.write_bytes(b"%PDF-1.7\nstable source bytes\n")
    cfg = Config(runtime=RuntimeSettings(work_dir=tmp_path / "work"))
    work = cfg.book_work_dir(source_pdf)
    work.mkdir(parents=True)
    (work / "01_raw.json").write_text('{"pages": {}}', encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"source/source\.pdf.*source/source_meta\.json.*--force-rerun"):
        run_parse(source_pdf, cfg, force=False)
