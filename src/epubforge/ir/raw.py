"""Typed helpers for reading Docling DoclingDocument JSON (Raw IR)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_raw(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# Docling element roles that indicate a complex page.
COMPLEX_ROLES: frozenset[str] = frozenset(
    {"Table", "Figure", "Footnote", "Formula", "Code"}
)
