"""Tests for the segmented dispatch logic in ``parse_pdf``.

We exercise three things without touching docling/onnxruntime:

1. ``_segment_ranges`` produces the expected 1-based inclusive ``(start, end)``
   pairs for representative inputs (full coverage, partial last segment,
   exact divisor, segment_size >= total_pages, etc.).
2. ``parse_pdf(segment_size=None)`` and ``parse_pdf(segment_size>=total)``
   both go through the single-process path (no subprocess invocation).
3. ``parse_pdf(segment_size<total)`` issues one subprocess per segment with
   the expected CLI arguments and merges the resulting JSON files.

The subprocess and the per-batch docling loop are mocked — we never touch
the real converter.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from epubforge.parser import docling_parser
from epubforge.parser.docling_parser import _segment_ranges


# ---------------------------------------------------------------------------
# _segment_ranges
# ---------------------------------------------------------------------------


class TestSegmentRanges:
    def test_partial_last_segment(self) -> None:
        assert _segment_ranges(95, 40) == [(1, 40), (41, 80), (81, 95)]

    def test_exact_divisor(self) -> None:
        assert _segment_ranges(80, 40) == [(1, 40), (41, 80)]

    def test_single_segment_covers_all(self) -> None:
        # When segment_size > total_pages we still produce one segment.
        assert _segment_ranges(30, 40) == [(1, 30)]

    def test_segment_size_one(self) -> None:
        assert _segment_ranges(3, 1) == [(1, 1), (2, 2), (3, 3)]

    def test_50_pages_size_20(self) -> None:
        assert _segment_ranges(50, 20) == [(1, 20), (21, 40), (41, 50)]

    @pytest.mark.parametrize("bad_total", [0, -1])
    def test_bad_total_raises(self, bad_total: int) -> None:
        with pytest.raises(ValueError):
            _segment_ranges(bad_total, 10)

    @pytest.mark.parametrize("bad_size", [0, -5])
    def test_bad_segment_size_raises(self, bad_size: int) -> None:
        with pytest.raises(ValueError):
            _segment_ranges(10, bad_size)


# ---------------------------------------------------------------------------
# parse_pdf dispatch
# ---------------------------------------------------------------------------


def _write_fake_segment_json(seg_out: Path, *, pages: list[int]) -> None:
    """Write a minimal docling-shaped JSON for a single segment."""
    payload = {
        "pages": {str(p): {} for p in pages},
        "texts": [],
        "groups": [],
        "pictures": [],
        "tables": [],
        "body": {"self_ref": "#/body", "name": "_root_", "children": []},
        "furniture": {"self_ref": "#/furniture", "name": "_root_", "children": []},
    }
    seg_out.parent.mkdir(parents=True, exist_ok=True)
    seg_out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestParsePdfSegmentedDispatch:
    """``parse_pdf(segment_size=N)`` paths through the subprocess."""

    def test_segment_size_none_skips_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When segment_size is None, the single-process path is taken."""
        captured: dict[str, object] = {}

        def fake_count(_: Path) -> int:
            return 100

        def fake_range(*args, **kwargs):
            captured["called"] = True
            return ({"texts": [], "pages": {}}, 0)

        def fake_save(merged_data, out_path, *, n_pictures_total):
            out_path.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(docling_parser, "_count_pdf_pages", fake_count)
        monkeypatch.setattr(docling_parser, "_parse_pdf_range", fake_range)
        monkeypatch.setattr(docling_parser, "_save_merged_doc", fake_save)
        # If subprocess.run is invoked the test must fail — we should not
        # spawn workers in single-process mode.
        with patch("subprocess.run", side_effect=AssertionError("subprocess.run should not be called")):
            docling_parser.parse_pdf(
                tmp_path / "fake.pdf",
                tmp_path / "out.json",
                images_dir=tmp_path / "imgs",
                ocr_settings=None,
                page_batch_size=20,
                segment_size=None,
            )
        assert captured.get("called") is True

    def test_segment_size_ge_total_skips_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """segment_size >= total_pages must take the single-process path."""

        def fake_count(_: Path) -> int:
            return 30

        def fake_range(*args, **kwargs):
            return ({"texts": [], "pages": {}}, 0)

        def fake_save(merged_data, out_path, *, n_pictures_total):
            out_path.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(docling_parser, "_count_pdf_pages", fake_count)
        monkeypatch.setattr(docling_parser, "_parse_pdf_range", fake_range)
        monkeypatch.setattr(docling_parser, "_save_merged_doc", fake_save)
        with patch("subprocess.run", side_effect=AssertionError("no subprocess expected")):
            docling_parser.parse_pdf(
                tmp_path / "fake.pdf",
                tmp_path / "out.json",
                images_dir=tmp_path / "imgs",
                ocr_settings=None,
                page_batch_size=20,
                segment_size=30,
            )

    def test_segment_size_lt_total_invokes_workers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """50p / segment_size=20 → 3 subprocess invocations with correct ranges."""
        total_pages = 50
        segment_size = 20
        out_path = tmp_path / "01_raw.json"
        images_dir = tmp_path / "images"
        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")

        monkeypatch.setattr(
            docling_parser, "_count_pdf_pages", lambda _: total_pages
        )

        # Each subprocess.run call writes the segment JSON we'll merge.
        calls: list[list[str]] = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(list(cmd))
            # Find --out and --start/--end in the cmd to know what to write.
            out_idx = cmd.index("--out") + 1
            start_idx = cmd.index("--start") + 1
            end_idx = cmd.index("--end") + 1
            seg_out = Path(cmd[out_idx])
            seg_start = int(cmd[start_idx])
            seg_end = int(cmd[end_idx])
            _write_fake_segment_json(
                seg_out, pages=list(range(seg_start, seg_end + 1))
            )

            class _R:
                returncode = 0

            return _R()

        # We don't want the real save_as_json round-trip in this test.
        def fake_save(merged_data, out_path, *, n_pictures_total):
            out_path.write_text(
                json.dumps(merged_data, ensure_ascii=False), encoding="utf-8"
            )

        monkeypatch.setattr(docling_parser, "_save_merged_doc", fake_save)

        with patch("subprocess.run", side_effect=fake_run):
            docling_parser.parse_pdf(
                pdf_path,
                out_path,
                images_dir=images_dir,
                ocr_settings=None,
                page_batch_size=10,
                segment_size=segment_size,
            )

        # 3 segments expected: (1,20) (21,40) (41,50)
        assert len(calls) == 3
        ranges = []
        for cmd in calls:
            assert "epubforge.parser._segment_worker" in cmd
            assert "docling" in cmd
            s = int(cmd[cmd.index("--start") + 1])
            e = int(cmd[cmd.index("--end") + 1])
            ranges.append((s, e))
            # page-batch-size from caller is forwarded
            assert int(cmd[cmd.index("--page-batch-size") + 1]) == 10
        assert ranges == [(1, 20), (21, 40), (41, 50)]

        # Output JSON exists and includes all 50 pages from the merged segments.
        merged = json.loads(out_path.read_text(encoding="utf-8"))
        page_keys = sorted(int(k) for k in merged.get("pages", {}).keys())
        assert page_keys == list(range(1, 51))

    def test_segment_failure_preserves_files_and_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero exit from a worker raises and keeps segment files."""
        import subprocess

        monkeypatch.setattr(docling_parser, "_count_pdf_pages", lambda _: 50)

        first_seg_out: dict[str, Path] = {}

        def fake_run(cmd, check=True, **kwargs):
            out_idx = cmd.index("--out") + 1
            seg_out = Path(cmd[out_idx])
            start_idx = cmd.index("--start") + 1
            seg_start = int(cmd[start_idx])
            if seg_start == 1:
                # First segment writes successfully.
                first_seg_out["path"] = seg_out
                _write_fake_segment_json(seg_out, pages=list(range(1, 21)))

                class _R:
                    returncode = 0

                return _R()
            # Second segment fails.
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="segment 2/3"):
                docling_parser.parse_pdf(
                    tmp_path / "fake.pdf",
                    tmp_path / "01_raw.json",
                    images_dir=tmp_path / "images",
                    ocr_settings=None,
                    page_batch_size=10,
                    segment_size=20,
                )

        # The successful first segment file must still be present so an
        # operator can inspect it after a partial failure.
        assert first_seg_out["path"].exists()

    def test_invalid_segment_size_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            docling_parser.parse_pdf(
                tmp_path / "fake.pdf",
                tmp_path / "out.json",
                images_dir=tmp_path / "imgs",
                ocr_settings=None,
                page_batch_size=20,
                segment_size=0,
            )
