"""End-to-end Granite parser test against a real llama-server.

Skipped by default. To run, start llama-server (see
``docs/explorations/granite-llama-server-spike.md`` for the required
``--special`` / ``--jinja`` flags) and invoke pytest with the marker::

    uv run pytest tests/parser/test_granite_parser_e2e.py -v -m granite_server

Requires:
- llama-server running with Granite-Docling-258M on the configured api_url
- A small fixture PDF on disk (set EPUBFORGE_TEST_PDF or rely on default).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from epubforge.config import GraniteSettings
from epubforge.parser import parse_pdf_granite


pytestmark = pytest.mark.granite_server


@pytest.fixture
def fixture_pdf() -> Path:
    candidate = os.environ.get("EPUBFORGE_TEST_PDF")
    if candidate:
        path = Path(candidate)
    else:
        path = Path("work/bmsf/source/source.pdf")
    if not path.is_file():
        pytest.skip(f"Fixture PDF not found: {path}")
    return path


def test_three_page_smoke(fixture_pdf: Path, tmp_path: Path) -> None:
    """Convert the first 3 pages of a real PDF via a live llama-server."""
    settings = GraniteSettings(
        enabled=True,
        api_url=os.environ.get(
            "EPUBFORGE_TEST_GRANITE_URL",
            "http://localhost:8080/v1/chat/completions",
        ),
    )
    out_path = tmp_path / "01_raw_granite.json"
    result = parse_pdf_granite(
        pdf_path=fixture_pdf,
        out_path=out_path,
        settings=settings,
        page_count=3,
    )

    assert out_path.exists()
    assert len(result.successful_pages) >= 1
    assert result.manifest_path is not None
    assert result.manifest_path.exists()
