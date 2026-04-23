"""End-to-end smoke test for the ingestion pipeline.

Requires fixtures/*.pdf to exist and API env vars to be set.
Skips automatically when fixtures are absent so CI passes without credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

FIXTURES = sorted(Path("fixtures").glob("*.pdf"))
FIXTURE = FIXTURES[0] if FIXTURES else None


@pytest.mark.skipif(not FIXTURES, reason="No PDF fixtures found in fixtures/")
@pytest.mark.skipif(
    not os.environ.get("EPUBFORGE_LLM_API_KEY"),
    reason="EPUBFORGE_LLM_API_KEY not set",
)
def test_full_pipeline_smoke() -> None:
    """Run stage 4 on top of existing stage 1-3 outputs and verify the raw semantic artifact."""
    assert FIXTURE is not None
    work_dir = Path("work") / FIXTURE.stem

    # Require stages 1-3 to already exist (expensive; skip test if not)
    if not (work_dir / "02_pages.json").exists():
        pytest.skip("Stage 2 output missing — run parse+classify first")
    if not list((work_dir / "03_extract").glob("unit_*.json")):
        pytest.skip("Stage 3 output missing — run extract first")

    result = subprocess.run(
        ["uv", "run", "epubforge", "run", str(FIXTURE), "--from", "4", "--force-rerun"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Pipeline failed:\n{result.stdout}\n{result.stderr}"

    raw_path = work_dir / "05_semantic_raw.json"
    assert raw_path.exists(), "05_semantic_raw.json not created"

    raw = json.loads(raw_path.read_text())
    assert raw["chapters"], "05_semantic_raw.json has no chapters"
