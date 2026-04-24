from __future__ import annotations

from typing import Literal

import pytest

from epubforge.ir.semantic import Provenance


@pytest.fixture
def prov():
    def _make(page: int = 1, source: Literal["llm", "vlm", "docling", "passthrough"] = "passthrough") -> Provenance:
        return Provenance(page=page, bbox=None, source=source)

    return _make
