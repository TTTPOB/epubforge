"""Shared HTML regex patterns for audit table detectors."""

from __future__ import annotations

import re

TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
COLSPAN_RE = re.compile(r'colspan\s*=\s*["\']?(\d+)', re.IGNORECASE)

__all__ = ["TBODY_RE", "ROW_RE", "COLSPAN_RE"]
