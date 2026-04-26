"""Unit tests for the Granite-Docling-258M VLM parser.

All tests in this file are fully offline — every external interaction
(httpx GET against llama-server, DocumentConverter.convert) is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from epubforge.config import GraniteSettings
from epubforge.parser import GraniteParseResult, parse_pdf_granite
from epubforge.parser.granite_parser import (
    _derive_models_url,
    _detect_repeated_lines,
    _merge_pages_with_breaks,
    _strip_doctag_wrapper,
    _text_signature,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> GraniteSettings:
    return GraniteSettings(
        enabled=True,
        api_url="http://localhost:18080/v1/chat/completions",
        api_model="granite-docling",
        prompt="Convert this page to docling.",
        scale=2.0,
        timeout_seconds=60,
        max_tokens=4096,
        health_check=True,
        concurrency=1,
    )


def _fake_page_doctags(page_idx: int) -> str:
    """Build a minimal valid per-page doctags string."""
    return (
        f"<doctag><page_header><loc_88><loc_41><loc_286><loc_49>"
        f"page {page_idx} header</page_header>\n"
        f"<text><loc_88><loc_67><loc_426><loc_139>page {page_idx} body</text>\n"
        f"</doctag>"
    )


def _make_fake_convert_result(doctags_str: str, original_page_no: int) -> Any:
    """Construct a mock object that mimics docling's ConversionResult enough
    for GraniteParser to extract per-page doctags from it."""
    fake_doc = MagicMock()
    fake_doc.export_to_doctags.return_value = doctags_str
    fake_result = MagicMock()
    fake_result.document = fake_doc

    fake_page = MagicMock()
    fake_page.page_no = original_page_no
    fake_result.pages = [fake_page]
    return fake_result


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestDeriveModelsUrl:
    def test_chat_completions_path(self) -> None:
        assert (
            _derive_models_url("http://localhost:8080/v1/chat/completions")
            == "http://localhost:8080/v1/models"
        )

    def test_no_chat_completions_suffix(self) -> None:
        assert _derive_models_url("http://localhost:8080/v1") == (
            "http://localhost:8080/v1/models"
        )

    def test_trailing_slash(self) -> None:
        assert _derive_models_url(
            "http://localhost:8080/v1/chat/completions/"
        ) == "http://localhost:8080/v1/models"


class TestStripDoctagWrapper:
    def test_well_formed(self) -> None:
        assert _strip_doctag_wrapper("<doctag>foo bar</doctag>") == "foo bar"

    def test_multiline(self) -> None:
        wrapped = "<doctag>\n<text>a</text>\n<text>b</text>\n</doctag>"
        assert _strip_doctag_wrapper(wrapped) == "<text>a</text>\n<text>b</text>"

    def test_missing_close(self) -> None:
        assert _strip_doctag_wrapper("<doctag>foo") == "foo"

    def test_empty_outer_only(self) -> None:
        assert _strip_doctag_wrapper("<doctag></doctag>") == ""


class TestMergePagesWithBreaks:
    def test_two_pages(self) -> None:
        merged = _merge_pages_with_breaks(
            [
                "<doctag>\n<text>page A</text>\n</doctag>",
                "<doctag>\n<text>page B</text>\n</doctag>",
            ]
        )
        # exactly one <page_break> between the two pages
        assert merged.count("<page_break>") == 1
        assert "<text>page A</text>" in merged
        assert "<text>page B</text>" in merged
        # outer wrapper preserved
        assert merged.startswith("<doctag>")
        assert merged.endswith("</doctag>")

    def test_three_pages_two_breaks(self) -> None:
        merged = _merge_pages_with_breaks(
            [_fake_page_doctags(1), _fake_page_doctags(2), _fake_page_doctags(3)]
        )
        assert merged.count("<page_break>") == 2

    def test_zero_breaks_for_single_page(self) -> None:
        merged = _merge_pages_with_breaks([_fake_page_doctags(1)])
        assert merged.count("<page_break>") == 0

    def test_round_trip_into_doctags_document(self) -> None:
        """Merged doctags must round-trip through docling's parser to verify
        the page_break injection is structurally valid (not just regex-correct)."""
        from docling_core.types.doc import DocTagsDocument, DoclingDocument

        merged = _merge_pages_with_breaks(
            [_fake_page_doctags(1), _fake_page_doctags(2), _fake_page_doctags(3)]
        )
        doc_tags = DocTagsDocument.from_multipage_doctags_and_images(
            doctags=merged, images=None
        )
        assert len(doc_tags.pages) == 3
        doc = DoclingDocument.load_from_doctags(doc_tags, document_name="test")
        assert len(doc.pages) == 3


class TestTextSignature:
    def test_text_with_loc(self) -> None:
        sig = _text_signature(
            "<text><loc_10><loc_20><loc_30><loc_40>hello</text>"
        )
        assert sig == "hello"

    def test_text_without_loc(self) -> None:
        assert _text_signature("<text>hello world</text>") == "hello world"

    def test_non_text_line(self) -> None:
        assert _text_signature("<page_header>foo</page_header>") is None

    def test_blank_line(self) -> None:
        assert _text_signature("") is None


class TestDetectRepeatedLines:
    def test_no_repetition(self) -> None:
        doctags = (
            "<text><loc_10><loc_20><loc_30><loc_40>one</text>\n"
            "<text><loc_10><loc_20><loc_30><loc_40>two</text>\n"
            "<text><loc_10><loc_20><loc_30><loc_40>three</text>"
        )
        cleaned, dropped = _detect_repeated_lines(doctags)
        assert dropped == 0
        assert cleaned == doctags

    def test_two_repeats_under_threshold(self) -> None:
        doctags = (
            "<text><loc_1><loc_2><loc_3><loc_4>same</text>\n"
            "<text><loc_5><loc_6><loc_7><loc_8>same</text>"
        )
        cleaned, dropped = _detect_repeated_lines(doctags, threshold=3)
        assert dropped == 0  # 2 < threshold
        assert cleaned == doctags

    def test_three_repeats_drops_one(self) -> None:
        """At threshold=3, the first 2 lines are kept (the run becomes
        "repetition" only when the 3rd identical line arrives, which is
        the first surplus copy)."""
        doctags = (
            "<text><loc_1><loc_2><loc_3><loc_4>same</text>\n"
            "<text><loc_5><loc_6><loc_7><loc_8>same</text>\n"
            "<text><loc_9><loc_10><loc_11><loc_12>same</text>"
        )
        cleaned, dropped = _detect_repeated_lines(doctags, threshold=3)
        assert dropped == 1
        # 2 lines should remain
        assert cleaned.count("<text>") == 2

    def test_page_004_hallucination_case(self) -> None:
        """Reproduce the spike-report page_004 case: 14 identical
        ``吴心越著`` lines with shifted bbox locs. After dedup we expect
        2 lines preserved (= threshold - 1) and 12 surplus dropped."""
        repeated = "\n".join(
            f"<text><loc_{i}><loc_250><loc_{i + 13}><loc_377>吴 心 越 著</text>"
            for i in range(251, 265)  # 14 copies
        )
        doctags = (
            "<text><loc_235><loc_250><loc_251><loc_377>养 老 院</text>\n"
            + repeated
            + "\n<page_footer><loc_212><loc_434><loc_273><loc_441>fff</page_footer>"
        )
        cleaned, dropped = _detect_repeated_lines(doctags, threshold=3)
        assert dropped == 12  # 14 copies → 2 kept
        # the unique lead-in & footer survive
        assert "养 老 院" in cleaned
        assert "<page_footer>" in cleaned

    def test_repetition_followed_by_unique_resets_run(self) -> None:
        doctags = (
            "<text>a</text>\n"
            "<text>a</text>\n"
            "<text>a</text>\n"  # would be dropped (3rd same)
            "<text>b</text>\n"
            "<text>b</text>"  # only 2 of "b" in a row, both kept
        )
        cleaned, dropped = _detect_repeated_lines(doctags, threshold=3)
        assert dropped == 1
        assert cleaned.count("<text>b</text>") == 2

    def test_loc_strip_so_different_bboxes_count_as_same_text(self) -> None:
        """Spike's page_004 had varying bbox locs but identical inner text.
        The dedup must treat these as the same content."""
        sig_a = _text_signature(
            "<text><loc_1><loc_2><loc_3><loc_4>repeat</text>"
        )
        sig_b = _text_signature(
            "<text><loc_99><loc_98><loc_97><loc_96>repeat</text>"
        )
        assert sig_a == sig_b == "repeat"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheckIntegration:
    def test_unreachable_server_raises_runtime_error(
        self, settings: GraniteSettings, tmp_path: Path
    ) -> None:
        """When httpx.get raises (server down), parse_pdf_granite re-raises
        as RuntimeError with a helpful message."""
        with patch(
            "epubforge.parser.granite_parser.httpx.get",
            side_effect=ConnectionError("[Errno 111] Connection refused"),
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                parse_pdf_granite(
                    pdf_path=tmp_path / "fake.pdf",
                    out_path=tmp_path / "out.json",
                    settings=settings,
                    page_count=1,
                )

    def test_health_check_skipped_when_disabled(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """settings.health_check=False bypasses the GET call."""
        settings.health_check = False
        get_mock = MagicMock(side_effect=AssertionError("must not be called"))
        monkeypatch.setattr(
            "epubforge.parser.granite_parser.httpx.get", get_mock
        )

        # Need to stub the converter so the rest of the function still completes.
        fake_converter = MagicMock()
        fake_converter.convert.return_value = _make_fake_convert_result(
            _fake_page_doctags(1), original_page_no=1
        )
        with patch(
            "epubforge.parser.granite_parser._build_converter",
            return_value=fake_converter,
        ):
            (tmp_path / "fake.pdf").write_bytes(b"")
            result = parse_pdf_granite(
                pdf_path=tmp_path / "fake.pdf",
                out_path=tmp_path / "01_raw_granite.json",
                settings=settings,
                page_count=1,
            )

        assert get_mock.call_count == 0
        assert result.successful_pages == [1]


# ---------------------------------------------------------------------------
# End-to-end (with mocked converter)
# ---------------------------------------------------------------------------


class TestParsePdfGraniteMocked:
    """Drive parse_pdf_granite end-to-end with everything mocked."""

    def _patch_external(
        self, monkeypatch: pytest.MonkeyPatch, fake_converter: MagicMock
    ) -> MagicMock:
        # Mock the health check
        httpx_resp = MagicMock()
        httpx_resp.status_code = 200
        httpx_resp.raise_for_status = MagicMock()
        httpx_get = MagicMock(return_value=httpx_resp)
        monkeypatch.setattr(
            "epubforge.parser.granite_parser.httpx.get", httpx_get
        )

        # Mock the converter factory
        monkeypatch.setattr(
            "epubforge.parser.granite_parser._build_converter",
            lambda settings: fake_converter,
        )
        return httpx_get

    def test_three_pages_all_succeed(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_converter = MagicMock()
        fake_converter.convert.side_effect = [
            _make_fake_convert_result(_fake_page_doctags(1), 1),
            _make_fake_convert_result(_fake_page_doctags(2), 2),
            _make_fake_convert_result(_fake_page_doctags(3), 3),
        ]
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")
        out_path = tmp_path / "01_raw_granite.json"

        result = parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=out_path,
            settings=settings,
            page_count=3,
        )

        assert result.successful_pages == [1, 2, 3]
        assert result.failed_pages == []
        assert out_path.exists()
        # Manifest sidecar
        manifest_path = out_path.with_name("01_raw_granite.manifest.json")
        assert result.manifest_path == manifest_path
        assert manifest_path.exists()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 1
        assert manifest["mode"] == "chunked-per-page"
        assert manifest["model"] == "ibm-granite/granite-docling-258M"
        assert manifest["backend"] == "llama-server-gguf"
        assert manifest["api_url"] == settings.api_url
        assert manifest["api_model"] == settings.api_model
        assert manifest["scale"] == settings.scale
        assert manifest["concurrency"] == settings.concurrency
        assert manifest["page_count"] == 3
        assert manifest["successful_pages"] == 3
        assert manifest["failed_pages"] == []
        assert manifest["prompt"] == settings.prompt
        # ISO timestamps should parse back
        from datetime import datetime
        datetime.fromisoformat(manifest["started_at"])
        datetime.fromisoformat(manifest["completed_at"])

    def test_per_page_failure_is_isolated(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single page that throws during convert() is recorded in
        failed_pages and the loop continues."""
        fake_converter = MagicMock()
        fake_converter.convert.side_effect = [
            _make_fake_convert_result(_fake_page_doctags(1), 1),
            RuntimeError("simulated VLM hang"),
            _make_fake_convert_result(_fake_page_doctags(3), 3),
        ]
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")
        out_path = tmp_path / "01_raw_granite.json"

        result = parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=out_path,
            settings=settings,
            page_count=3,
        )

        assert result.successful_pages == [1, 3]
        assert result.failed_pages == [2]
        assert out_path.exists()

        manifest = json.loads(
            result.manifest_path.read_text(encoding="utf-8")
            if result.manifest_path
            else "{}"
        )
        assert manifest["successful_pages"] == 2
        assert manifest["failed_pages"] == [2]

    def test_all_pages_fail_raises(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_converter = MagicMock()
        fake_converter.convert.side_effect = RuntimeError("everything broken")
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")

        with pytest.raises(RuntimeError, match="0 successful pages"):
            parse_pdf_granite(
                pdf_path=pdf_path,
                out_path=tmp_path / "out.json",
                settings=settings,
                page_count=2,
            )

    def test_page_break_injection_count(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N successful pages → N-1 page_breaks in the merged doctags."""
        fake_converter = MagicMock()
        fake_converter.convert.side_effect = [
            _make_fake_convert_result(_fake_page_doctags(i), i)
            for i in range(1, 6)
        ]
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")
        out_path = tmp_path / "01_raw_granite.json"

        # Spy on _merge_pages_with_breaks to capture its result
        captured: dict[str, Any] = {}
        original_merge = _merge_pages_with_breaks

        def spy(parts: list[str]) -> str:
            merged = original_merge(parts)
            captured["merged"] = merged
            return merged

        monkeypatch.setattr(
            "epubforge.parser.granite_parser._merge_pages_with_breaks", spy
        )

        result = parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=out_path,
            settings=settings,
            page_count=5,
        )
        assert len(result.successful_pages) == 5
        assert captured["merged"].count("<page_break>") == 4

    def test_repeated_lines_are_deduped_during_parse(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A page that comes back with hallucinated repetition is
        sanitised and reported via repeated_line_warnings."""
        hallucinated = (
            "<doctag>\n"
            + "<text><loc_1><loc_2><loc_3><loc_4>spam</text>\n"
            + "<text><loc_5><loc_6><loc_7><loc_8>spam</text>\n"
            + "<text><loc_9><loc_10><loc_11><loc_12>spam</text>\n"
            + "<text><loc_13><loc_14><loc_15><loc_16>spam</text>\n"
            + "<text><loc_17><loc_18><loc_19><loc_20>spam</text>\n"
            + "</doctag>"
        )
        clean_page = _fake_page_doctags(2)

        fake_converter = MagicMock()
        fake_converter.convert.side_effect = [
            _make_fake_convert_result(hallucinated, 1),
            _make_fake_convert_result(clean_page, 2),
        ]
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")
        out_path = tmp_path / "01_raw_granite.json"

        result = parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=out_path,
            settings=settings,
            page_count=2,
        )
        assert 1 in result.repeated_line_warnings
        assert 2 not in result.repeated_line_warnings
        assert result.successful_pages == [1, 2]

    def test_on_progress_callback_invoked_per_page(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_converter = MagicMock()
        fake_converter.convert.side_effect = [
            _make_fake_convert_result(_fake_page_doctags(i), i)
            for i in range(1, 4)
        ]
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")

        calls: list[tuple[int, int, float]] = []

        def progress(page_no: int, total: int, page_elapsed: float) -> None:
            calls.append((page_no, total, page_elapsed))

        parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=tmp_path / "out.json",
            settings=settings,
            page_count=3,
            on_progress=progress,
        )
        assert [c[0] for c in calls] == [1, 2, 3]
        assert all(c[1] == 3 for c in calls)
        assert all(c[2] >= 0 for c in calls)

    def test_progress_callback_exception_does_not_break_parse(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_converter = MagicMock()
        fake_converter.convert.side_effect = [
            _make_fake_convert_result(_fake_page_doctags(1), 1),
        ]
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")

        def angry_progress(*_args: Any) -> None:
            raise ValueError("ignore me")

        result = parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=tmp_path / "out.json",
            settings=settings,
            page_count=1,
            on_progress=angry_progress,
        )
        assert result.successful_pages == [1]

    def test_returns_dataclass_instance(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_converter = MagicMock()
        fake_converter.convert.return_value = _make_fake_convert_result(
            _fake_page_doctags(1), 1
        )
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")
        out_path = tmp_path / "out.json"

        result = parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=out_path,
            settings=settings,
            page_count=1,
        )
        assert isinstance(result, GraniteParseResult)
        assert result.out_path == out_path
        assert result.page_count == 1
        assert result.elapsed_seconds >= 0

    def test_manifest_not_inside_doclingdocument_json(
        self,
        settings: GraniteSettings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The DoclingDocument JSON must be pure docling content; manifest
        belongs in the sibling .manifest.json file only."""
        fake_converter = MagicMock()
        fake_converter.convert.return_value = _make_fake_convert_result(
            _fake_page_doctags(1), 1
        )
        self._patch_external(monkeypatch, fake_converter)

        pdf_path = tmp_path / "fake.pdf"
        pdf_path.write_bytes(b"")
        out_path = tmp_path / "01_raw_granite.json"

        parse_pdf_granite(
            pdf_path=pdf_path,
            out_path=out_path,
            settings=settings,
            page_count=1,
        )
        doc_payload = json.loads(out_path.read_text(encoding="utf-8"))
        # No manifest fields leaked into the DoclingDocument JSON
        assert "schema_version" not in doc_payload  # would clash with manifest
        assert "backend" not in doc_payload
        assert "api_url" not in doc_payload
