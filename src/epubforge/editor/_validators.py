"""Shared validator helpers for the editor package.

These are package-internal utilities (not file-private), shared across
ops.py, memory.py, doctor.py, and leases.py to avoid duplication.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base model that forbids extra fields."""

    model_config = ConfigDict(extra="forbid")


def require_non_empty(value: str, *, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def validate_uuid4(value: str, *, field_name: str) -> str:
    value = require_non_empty(value, field_name=field_name)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise ValueError(f"{field_name} must be a canonical UUID4 string")
    return value


def validate_utc_iso_timestamp(value: str, *, field_name: str) -> str:
    value = require_non_empty(value, field_name=field_name)
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be a UTC ISO timestamp ending with 'Z'")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UTC ISO timestamp") from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be in UTC")
    return value


def parse_utc_iso(value: str, *, field_name: str) -> datetime:
    """Parse a UTC ISO timestamp string and return a timezone-aware datetime.

    Keeps leases-specific semantics: returns datetime instead of str.
    """
    value = require_non_empty(value, field_name=field_name)
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be a UTC ISO timestamp ending with 'Z'")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UTC ISO timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be in UTC")
    return parsed.astimezone(UTC)


__all__ = [
    "StrictModel",
    "parse_utc_iso",
    "require_non_empty",
    "validate_utc_iso_timestamp",
    "validate_uuid4",
]
