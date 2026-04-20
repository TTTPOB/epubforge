"""End-to-end smoke test. Implement in epubforge-7tl.

Requires fixtures/*.pdf to exist and API env vars to be set.
Skips automatically when fixtures are absent so CI passes without credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = sorted(Path("fixtures").glob("*.pdf"))


@pytest.mark.skipif(not FIXTURES, reason="No PDF fixtures found in fixtures/")
@pytest.mark.skipif(
    not os.environ.get("EPUBFORGE_LLM_API_KEY"),
    reason="EPUBFORGE_LLM_API_KEY not set",
)
def test_full_pipeline_smoke() -> None:
    """Run the full pipeline on the first fixture PDF."""
    raise NotImplementedError("TODO: implement in epubforge-7tl")
