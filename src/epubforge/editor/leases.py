"""Lease state helpers for chapter-scoped and book-wide editor work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _require_non_empty(value: str, *, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _parse_utc_iso(value: str, *, field_name: str) -> datetime:
    value = _require_non_empty(value, field_name=field_name)
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be a UTC ISO timestamp ending with 'Z'")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UTC ISO timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be in UTC")
    return parsed.astimezone(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expires_at(now: str, ttl: int) -> str:
    if ttl <= 0:
        raise ValueError("ttl must be > 0")
    start = _parse_utc_iso(now, field_name="now")
    return (start + timedelta(seconds=ttl)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LeaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChapterLease(LeaseModel):
    chapter_uid: str
    holder: str
    task: str
    acquired_at: str
    expires_at: str

    @field_validator("chapter_uid", "holder", "task")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return _require_non_empty(value, field_name=info.field_name)

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _validate_ts(cls, value: str, info) -> str:
        _parse_utc_iso(value, field_name=info.field_name)
        return value


class BookExclusiveLease(LeaseModel):
    holder: str
    reason: Literal["topology_op", "compact", "init"]
    acquired_at: str
    expires_at: str

    @field_validator("holder")
    @classmethod
    def _validate_holder(cls, value: str) -> str:
        return _require_non_empty(value, field_name="holder")

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _validate_ts(cls, value: str, info) -> str:
        _parse_utc_iso(value, field_name=info.field_name)
        return value


class LeaseState(LeaseModel):
    chapter_leases: list[ChapterLease] = Field(default_factory=list)
    book_exclusive: BookExclusiveLease | None = None

    def expire_stale(self, *, now: str | None = None) -> None:
        current = _parse_utc_iso(now or _utc_now(), field_name="now")
        self.chapter_leases = [
            lease for lease in self.chapter_leases if _parse_utc_iso(lease.expires_at, field_name="expires_at") > current
        ]
        if self.book_exclusive is not None and _parse_utc_iso(self.book_exclusive.expires_at, field_name="expires_at") <= current:
            self.book_exclusive = None

    def chapter_lease(self, chapter_uid: str) -> ChapterLease | None:
        for lease in self.chapter_leases:
            if lease.chapter_uid == chapter_uid:
                return lease
        return None

    def acquire_chapter(
        self,
        chapter_uid: str,
        holder: str,
        task: str,
        *,
        ttl: int = 1800,
        now: str | None = None,
    ) -> ChapterLease | None:
        current = now or _utc_now()
        self.expire_stale(now=current)
        _require_non_empty(chapter_uid, field_name="chapter_uid")
        _require_non_empty(holder, field_name="holder")
        _require_non_empty(task, field_name="task")
        if self.book_exclusive is not None:
            return None
        existing = self.chapter_lease(chapter_uid)
        if existing is not None and existing.holder != holder:
            return None
        lease = ChapterLease(
            chapter_uid=chapter_uid,
            holder=holder,
            task=task,
            acquired_at=current,
            expires_at=_expires_at(current, ttl),
        )
        self.chapter_leases = [item for item in self.chapter_leases if item.chapter_uid != chapter_uid]
        self.chapter_leases.append(lease)
        self.chapter_leases.sort(key=lambda item: item.chapter_uid)
        return lease

    def release_chapter(self, chapter_uid: str, holder: str, *, now: str | None = None) -> ChapterLease | None:
        self.expire_stale(now=now)
        _require_non_empty(chapter_uid, field_name="chapter_uid")
        _require_non_empty(holder, field_name="holder")
        existing = self.chapter_lease(chapter_uid)
        if existing is None or existing.holder != holder:
            return None
        self.chapter_leases = [item for item in self.chapter_leases if item.chapter_uid != chapter_uid]
        return existing

    def acquire_book_exclusive(
        self,
        holder: str,
        reason: Literal["topology_op", "compact", "init"],
        *,
        ttl: int = 300,
        now: str | None = None,
    ) -> BookExclusiveLease | None:
        current = now or _utc_now()
        self.expire_stale(now=current)
        _require_non_empty(holder, field_name="holder")
        if self.chapter_leases:
            return None
        if self.book_exclusive is not None and self.book_exclusive.holder != holder:
            return None
        lease = BookExclusiveLease(
            holder=holder,
            reason=reason,
            acquired_at=current,
            expires_at=_expires_at(current, ttl),
        )
        self.book_exclusive = lease
        return lease

    def release_book_exclusive(self, holder: str, *, now: str | None = None) -> BookExclusiveLease | None:
        self.expire_stale(now=now)
        _require_non_empty(holder, field_name="holder")
        existing = self.book_exclusive
        if existing is None or existing.holder != holder:
            return None
        self.book_exclusive = None
        return existing


__all__ = [
    "BookExclusiveLease",
    "ChapterLease",
    "LeaseState",
]
