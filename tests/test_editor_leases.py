from __future__ import annotations

from epubforge.editor.leases import LeaseState


def test_chapter_lease_acquire_release_and_stale_cleanup() -> None:
    state = LeaseState()

    lease = state.acquire_chapter("ch-1", "agent-1", "proofread", ttl=60, now="2026-04-23T08:00:00Z")
    assert lease is not None
    assert lease.chapter_uid == "ch-1"
    assert lease.holder == "agent-1"
    assert lease.expires_at == "2026-04-23T08:01:00Z"

    assert state.acquire_chapter("ch-1", "agent-2", "other", now="2026-04-23T08:00:10Z") is None

    renewed = state.acquire_chapter("ch-1", "agent-1", "proofread-pass-2", ttl=120, now="2026-04-23T08:00:30Z")
    assert renewed is not None
    assert renewed.task == "proofread-pass-2"
    assert renewed.expires_at == "2026-04-23T08:02:30Z"

    assert state.release_chapter("ch-1", "agent-2", now="2026-04-23T08:00:31Z") is None
    released = state.release_chapter("ch-1", "agent-1", now="2026-04-23T08:00:31Z")
    assert released is not None
    assert released.task == "proofread-pass-2"
    assert state.chapter_leases == []

    stale = state.acquire_chapter("ch-2", "agent-3", "cleanup", ttl=1, now="2026-04-23T08:10:00Z")
    assert stale is not None
    state.expire_stale(now="2026-04-23T08:10:02Z")
    assert state.chapter_leases == []


def test_book_exclusive_and_chapter_leases_are_mutually_exclusive() -> None:
    state = LeaseState()

    assert state.acquire_chapter("ch-1", "agent-1", "proofread", now="2026-04-23T08:00:00Z") is not None
    assert state.acquire_book_exclusive("supervisor", "topology_op", now="2026-04-23T08:00:05Z") is None

    assert state.release_chapter("ch-1", "agent-1", now="2026-04-23T08:00:06Z") is not None
    book_lease = state.acquire_book_exclusive("supervisor", "topology_op", ttl=60, now="2026-04-23T08:01:00Z")
    assert book_lease is not None
    assert book_lease.reason == "topology_op"

    assert state.acquire_chapter("ch-2", "agent-2", "proofread", now="2026-04-23T08:01:10Z") is None
    assert state.acquire_book_exclusive("agent-2", "topology_op", now="2026-04-23T08:01:10Z") is None
    assert state.release_book_exclusive("agent-2", now="2026-04-23T08:01:11Z") is None

    released = state.release_book_exclusive("supervisor", now="2026-04-23T08:01:12Z")
    assert released is not None
    assert released.holder == "supervisor"

    stale_book = state.acquire_book_exclusive("supervisor", "compact", ttl=1, now="2026-04-23T08:02:00Z")
    assert stale_book is not None
    state.expire_stale(now="2026-04-23T08:02:02Z")
    assert state.book_exclusive is None
    assert state.acquire_chapter("ch-3", "agent-3", "resume", now="2026-04-23T08:02:03Z") is not None
