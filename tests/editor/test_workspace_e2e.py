"""End-to-end workspace workflow tests — Phase 7G.

Covers:
- test_full_agent_workflow: full lifecycle create worktree -> modify book -> commit -> merge -> remove
- test_concurrent_agents_no_overlap: two agents modify different chapters, both accepted
- test_concurrent_agents_conflict: two agents modify same block, second merge gets git_conflict
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from epubforge.editor.workspace import (
    IntegrationResult,
    create_worktree,
    list_worktrees,
    merge_and_validate,
    remove_worktree,
)
from epubforge.io import save_book
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    """Create a minimal Provenance."""
    return Provenance(page=page, bbox=None, source="passthrough")


def _two_chapter_book() -> Book:
    """Create a minimal Book with 2 chapters, each with 1 paragraph block."""
    return Book(
        initialized_at="2024-01-01T00:00:00",
        uid_seed="e2e-test-seed",
        title="E2E Test Book",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                level=1,
                blocks=[
                    Paragraph(
                        uid="blk-1-1",
                        text="Hello from chapter 1.",
                        role="body",
                        provenance=_prov(page=1),
                    ),
                ],
            ),
            Chapter(
                uid="ch-2",
                title="Chapter 2",
                level=1,
                blocks=[
                    Paragraph(
                        uid="blk-2-1",
                        text="Hello from chapter 2.",
                        role="body",
                        provenance=_prov(page=5),
                    ),
                ],
            ),
        ],
    )


def _init_repo_with_book(tmp_path: Path) -> tuple[Path, str]:
    """Create a minimal git repo with a committed 2-chapter book.json.

    Returns (repo_root, work_dir_rel).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )

    work_dir_rel = "work/book"
    work_dir = repo / work_dir_rel
    work_dir.mkdir(parents=True, exist_ok=True)

    book = _two_chapter_book()
    save_book(book, work_dir)

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial book"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo, work_dir_rel


def _modify_block_text(book_json_path: Path, chapter_idx: int, block_idx: int, new_text: str) -> None:
    """Read book.json, change one block's text, write back."""
    data = json.loads(book_json_path.read_text(encoding="utf-8"))
    data["chapters"][chapter_idx]["blocks"][block_idx]["text"] = new_text
    book_json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _commit_in_worktree(worktree_path: Path, message: str) -> None:
    """Stage all changes and commit in a worktree."""
    subprocess.run(["git", "add", "."], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=worktree_path, check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_repo(tmp_path: Path) -> tuple[Path, str]:
    """Git repo with a committed 2-chapter book.json.

    Yields (repo_root, work_dir_rel).
    """
    return _init_repo_with_book(tmp_path)


# ---------------------------------------------------------------------------
# Test 1: Full agent lifecycle
# ---------------------------------------------------------------------------


def test_full_agent_workflow(e2e_repo: tuple[Path, str]) -> None:
    """Full lifecycle: create worktree -> modify book.json -> commit -> merge -> remove."""
    repo, work_dir_rel = e2e_repo
    branch = "agent/scanner-1"

    # Step 1: create worktree
    wt = create_worktree(repo, branch)
    assert wt.worktree_path.exists()
    assert wt.branch == branch

    # Step 2: modify book.json inside the worktree (change chapter 1, block 0 text)
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"
    _modify_block_text(wt_book_path, chapter_idx=0, block_idx=0, new_text="Agent modified chapter 1.")

    # Step 3: commit the change in the worktree
    _commit_in_worktree(wt.worktree_path, "agent work")

    # Step 4: merge_and_validate back into main repo
    result = merge_and_validate(repo, work_dir_rel, branch)
    assert isinstance(result, IntegrationResult)
    assert result.outcome.status == "accepted", (
        f"Expected 'accepted' but got '{result.outcome.status}': {result.outcome.message}"
    )
    assert result.change_count > 0
    assert result.merge_commit is not None
    assert result.conflict_files == []

    # Step 5: remove the worktree
    remove_result = remove_worktree(repo, branch)
    assert remove_result.branch == branch
    assert remove_result.branch_deleted is True

    # Verify worktree directory is gone
    assert not wt.worktree_path.exists()

    # Verify branch is deleted
    branch_check = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=repo, capture_output=True, text=True,
    )
    assert branch not in branch_check.stdout

    # Verify the main repo worktree list is back to just 1 (main)
    remaining = list_worktrees(repo, agent_only=True)
    assert len(remaining) == 0


# ---------------------------------------------------------------------------
# Test 2: Concurrent agents modifying different chapters — both accepted
# ---------------------------------------------------------------------------


def test_concurrent_agents_no_overlap(e2e_repo: tuple[Path, str]) -> None:
    """Two agents modify different chapters; both merges succeed."""
    repo, work_dir_rel = e2e_repo
    branch_1 = "agent/scanner-1"
    branch_2 = "agent/scanner-2"

    # Create both worktrees from the same base commit
    wt1 = create_worktree(repo, branch_1)
    wt2 = create_worktree(repo, branch_2)

    # Agent 1: modify chapter 1 (index 0), block 0
    wt1_book = wt1.worktree_path / work_dir_rel / "edit_state" / "book.json"
    _modify_block_text(wt1_book, chapter_idx=0, block_idx=0, new_text="Scanner-1 edited chapter 1.")
    _commit_in_worktree(wt1.worktree_path, "agent 1 work")

    # Agent 2: modify chapter 2 (index 1), block 0 — different content area
    wt2_book = wt2.worktree_path / work_dir_rel / "edit_state" / "book.json"
    _modify_block_text(wt2_book, chapter_idx=1, block_idx=0, new_text="Scanner-2 edited chapter 2.")
    _commit_in_worktree(wt2.worktree_path, "agent 2 work")

    # Merge agent 1 first
    result_1 = merge_and_validate(repo, work_dir_rel, branch_1)
    assert result_1.outcome.status == "accepted", (
        f"Agent 1 merge failed: {result_1.outcome.status}: {result_1.outcome.message}"
    )
    assert result_1.change_count > 0

    # Merge agent 2 — should also succeed since it modified a different chapter
    result_2 = merge_and_validate(repo, work_dir_rel, branch_2)
    assert result_2.outcome.status == "accepted", (
        f"Agent 2 merge failed: {result_2.outcome.status}: {result_2.outcome.message}"
    )
    assert result_2.change_count > 0

    # Clean up both worktrees
    remove_worktree(repo, branch_1, force=True)
    remove_worktree(repo, branch_2, force=True)

    # Verify no agent worktrees remain
    assert len(list_worktrees(repo, agent_only=True)) == 0


# ---------------------------------------------------------------------------
# Test 3: Concurrent agents modifying same block — second merge conflicts
# ---------------------------------------------------------------------------


def test_concurrent_agents_conflict(e2e_repo: tuple[Path, str]) -> None:
    """Two agents modify the same block; the second merge yields git_conflict."""
    repo, work_dir_rel = e2e_repo
    branch_1 = "agent/scanner-1"
    branch_2 = "agent/scanner-2"

    # Create both worktrees from the same base commit
    wt1 = create_worktree(repo, branch_1)
    wt2 = create_worktree(repo, branch_2)

    # Both agents modify the SAME block in the SAME chapter
    wt1_book = wt1.worktree_path / work_dir_rel / "edit_state" / "book.json"
    _modify_block_text(wt1_book, chapter_idx=0, block_idx=0, new_text="Scanner-1 version of block.")
    _commit_in_worktree(wt1.worktree_path, "agent 1 conflict work")

    wt2_book = wt2.worktree_path / work_dir_rel / "edit_state" / "book.json"
    _modify_block_text(wt2_book, chapter_idx=0, block_idx=0, new_text="Scanner-2 version of block.")
    _commit_in_worktree(wt2.worktree_path, "agent 2 conflict work")

    # Merge agent 1 — should succeed
    result_1 = merge_and_validate(repo, work_dir_rel, branch_1)
    assert result_1.outcome.status == "accepted", (
        f"Agent 1 merge failed unexpectedly: {result_1.outcome.status}: {result_1.outcome.message}"
    )

    # Capture the repo HEAD before trying to merge agent 2
    head_before = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Merge agent 2 — should get git_conflict since the same line was already merged
    result_2 = merge_and_validate(repo, work_dir_rel, branch_2)
    assert result_2.outcome.status == "git_conflict", (
        f"Expected 'git_conflict' but got '{result_2.outcome.status}': {result_2.outcome.message}"
    )

    # Verify rollback is clean: HEAD should still be at head_before
    head_after = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == head_before, (
        f"Rollback failed: HEAD moved from {head_before} to {head_after}"
    )

    # Verify the working tree is clean after rollback
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == "", f"Working tree is dirty after rollback: {status!r}"

    # Clean up both worktrees
    remove_worktree(repo, branch_1, force=True)
    remove_worktree(repo, branch_2, force=True)
