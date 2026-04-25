"""Tests for Phase 7F: workspace CLI commands (workspace_cli.py + tool_surface run_workspace_* functions).

Tests exercise the run_workspace_* business functions directly with real git repos,
following the same fixture pattern as test_workspace.py.

Test cases:
1. test_cli_workspace_create — basic create, verify JSON output
2. test_cli_workspace_create_bad_branch — invalid branch name, raises CommandError exit_code=2
3. test_cli_workspace_list — list worktrees, verify JSON
4. test_cli_workspace_list_agent_only — --agent-only filter
5. test_cli_workspace_merge_accepted — accepted merge, exit 0
6. test_cli_workspace_merge_conflict — git conflict branch not found, exit 1
7. test_cli_workspace_merge_semantic — semantic conflict (invalid uid), exit 2
8. test_cli_workspace_remove — remove worktree, verify JSON
9. test_cli_workspace_remove_force — remove with force=True
10. test_cli_workspace_gc — gc on fresh repo (nothing to remove)
11. test_cli_workspace_gc_dry_run — dry_run=True flag
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from epubforge.editor.cli_support import CommandError
from epubforge.editor.tool_surface import (
    run_workspace_create,
    run_workspace_gc,
    run_workspace_list,
    run_workspace_merge,
    run_workspace_remove,
)
from epubforge.editor.workspace import (
    _validate_branch_name,
    create_worktree,
    find_repo_root,
)
from epubforge.io import save_book
from epubforge.ir.semantic import (
    Book,
    Chapter,
    Paragraph,
    Provenance,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    """Create a minimal Provenance."""
    return Provenance(page=page, bbox=None, source="passthrough")


def _minimal_book(
    *,
    title: str = "Test Book",
    block_text: str = "Hello world.",
    block_uid: str = "blk-1",
    chapter_uid: str = "ch-1",
) -> Book:
    """Create a minimal Book suitable for edit_state/book.json."""
    return Book(
        initialized_at="2024-01-01T00:00:00",
        uid_seed="test-seed",
        title=title,
        chapters=[
            Chapter(
                uid=chapter_uid,
                title="Chapter 1",
                level=1,
                blocks=[
                    Paragraph(
                        uid=block_uid,
                        text=block_text,
                        role="body",
                        provenance=_prov(),
                    ),
                ],
            ),
        ],
    )


def _capture_stdout(fn, *args, **kwargs) -> tuple[int, dict]:
    """Call fn and capture its emit_json output; return (exit_code, parsed_json)."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        code = fn(*args, **kwargs)
    text = buf.getvalue().strip()
    return code, json.loads(text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal Git repo with an initial commit."""
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
    (repo / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


@pytest.fixture
def book_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a Git repo with a committed edit_state/book.json.

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
    book = _minimal_book()
    save_book(book, work_dir)

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial book"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo, work_dir_rel


# ---------------------------------------------------------------------------
# 1. workspace create — basic success
# ---------------------------------------------------------------------------


def test_cli_workspace_create(book_repo: tuple[Path, str]) -> None:
    """run_workspace_create should create worktree and emit JSON with expected fields."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    code, data = _capture_stdout(
        run_workspace_create,
        work=work,
        branch="agent/scanner-1",
        base_ref="HEAD",
    )

    assert code == 0
    assert data["created"] is True
    assert data["branch"] == "agent/scanner-1"
    assert "worktree_path" in data
    assert "work_dir" in data
    assert "commit" in data
    assert data["base_ref"] == "HEAD"
    # The worktree directory should actually exist on disk
    assert Path(data["worktree_path"]).exists()


# ---------------------------------------------------------------------------
# 2. workspace create — invalid branch name
# ---------------------------------------------------------------------------


def test_cli_workspace_create_bad_branch(book_repo: tuple[Path, str]) -> None:
    """run_workspace_create should raise CommandError(exit_code=2) for bad branch name."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    with pytest.raises(CommandError) as exc_info:
        run_workspace_create(work=work, branch="main", base_ref="HEAD")

    assert exc_info.value.exit_code == 2
    assert "invalid_branch" in exc_info.value.payload.get("kind", "")


def test_cli_workspace_create_bad_branch_no_prefix(book_repo: tuple[Path, str]) -> None:
    """Branch without agent/ prefix should raise CommandError(exit_code=2)."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    with pytest.raises(CommandError) as exc_info:
        run_workspace_create(work=work, branch="feature/foo", base_ref="HEAD")

    assert exc_info.value.exit_code == 2


# ---------------------------------------------------------------------------
# 3. workspace list — all worktrees
# ---------------------------------------------------------------------------


def test_cli_workspace_list(book_repo: tuple[Path, str]) -> None:
    """run_workspace_list should return JSON with worktrees list and count."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    code, data = _capture_stdout(run_workspace_list, work=work, agent_only=False)

    assert code == 0
    assert "worktrees" in data
    assert "count" in data
    assert data["count"] >= 1
    # Verify each item has expected fields
    for item in data["worktrees"]:
        assert "path" in item
        assert "branch" in item
        assert "commit" in item
        assert "is_main" in item
        assert "is_bare" in item
        assert "prunable" in item


# ---------------------------------------------------------------------------
# 4. workspace list — agent_only filter
# ---------------------------------------------------------------------------


def test_cli_workspace_list_agent_only(book_repo: tuple[Path, str]) -> None:
    """run_workspace_list with agent_only=True should only return agent/* worktrees."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    # First create an agent worktree so there's something to filter
    wt = create_worktree(repo, "agent/scanner-2")

    try:
        code, data = _capture_stdout(run_workspace_list, work=work, agent_only=True)

        assert code == 0
        assert "worktrees" in data
        # All returned worktrees must have branch starting with "agent/"
        for item in data["worktrees"]:
            assert item["branch"].startswith("agent/")
        # The main worktree should not appear
        branches = [item["branch"] for item in data["worktrees"]]
        assert "agent/scanner-2" in branches
    finally:
        # Clean up worktree
        subprocess.run(
            ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
            cwd=repo, check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", "agent/scanner-2"],
            cwd=repo, check=False, capture_output=True,
        )


def test_cli_workspace_list_agent_only_empty(book_repo: tuple[Path, str]) -> None:
    """run_workspace_list agent_only=True on repo with no agent branches returns empty list."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    code, data = _capture_stdout(run_workspace_list, work=work, agent_only=True)

    assert code == 0
    assert data["worktrees"] == []
    assert data["count"] == 0


# ---------------------------------------------------------------------------
# 5. workspace merge — accepted
# ---------------------------------------------------------------------------


def test_cli_workspace_merge_accepted(book_repo: tuple[Path, str]) -> None:
    """run_workspace_merge should return status=accepted and exit code 0."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    # Create agent worktree and add a commit that modifies book.json
    wt = create_worktree(repo, "agent/fixer-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"

    book_data = json.loads(wt_book_path.read_text(encoding="utf-8"))
    # Modify text in a way that produces a valid patch
    book_data["chapters"][0]["blocks"][0]["text"] = "Updated text from agent."
    wt_book_path.write_text(json.dumps(book_data, indent=2), encoding="utf-8")

    subprocess.run(
        ["git", "add", "."], cwd=wt.worktree_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "agent edit"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    code, data = _capture_stdout(
        run_workspace_merge, work=work, branch="agent/fixer-1", timeout=30
    )

    assert code == 0
    assert data["outcome"] == "accepted"
    assert data["branch"] == "agent/fixer-1"
    assert data["merge_commit"] is not None


# ---------------------------------------------------------------------------
# 6. workspace merge — git conflict (branch not found)
# ---------------------------------------------------------------------------


def test_cli_workspace_merge_conflict(book_repo: tuple[Path, str]) -> None:
    """run_workspace_merge should return status=git_conflict and exit code 1 for missing branch."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    code, data = _capture_stdout(
        run_workspace_merge, work=work, branch="agent/nonexistent-99", timeout=10
    )

    assert code == 1
    assert data["outcome"] == "git_conflict"


# ---------------------------------------------------------------------------
# 7. workspace merge — semantic conflict
# ---------------------------------------------------------------------------


def test_cli_workspace_merge_semantic(book_repo: tuple[Path, str]) -> None:
    """run_workspace_merge should return outcome=semantic_conflict and exit code 2.

    Triggered by changing uid_seed (a Book-level immutable field) in the agent branch,
    which causes diff_books to raise DiffError → semantic_conflict outcome.
    """
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    # Create agent worktree
    wt = create_worktree(repo, "agent/semantic-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"

    # Corrupt book.json by changing uid_seed — a Book-level immutable field.
    # diff_books raises DiffError("unsupported Book-level delta(s): uid_seed; ...")
    # which merge_and_validate classifies as semantic_conflict.
    book_data = json.loads(wt_book_path.read_text(encoding="utf-8"))
    book_data["uid_seed"] = "different-seed-intentionally-bad"
    wt_book_path.write_text(json.dumps(book_data, indent=2), encoding="utf-8")

    subprocess.run(
        ["git", "add", "."], cwd=wt.worktree_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "semantic break"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    code, data = _capture_stdout(
        run_workspace_merge, work=work, branch="agent/semantic-1", timeout=30
    )

    assert code == 2
    assert data["outcome"] == "semantic_conflict"


# ---------------------------------------------------------------------------
# 8. workspace remove — basic
# ---------------------------------------------------------------------------


def test_cli_workspace_remove(book_repo: tuple[Path, str]) -> None:
    """run_workspace_remove should remove worktree and emit JSON with removed=True."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    # Create agent worktree first
    wt = create_worktree(repo, "agent/remover-1")
    assert wt.worktree_path.exists()

    code, data = _capture_stdout(
        run_workspace_remove, work=work, branch="agent/remover-1", force=False
    )

    assert code == 0
    assert data["removed"] is True
    assert data["branch"] == "agent/remover-1"
    assert "worktree_path" in data
    assert "branch_deleted" in data
    assert "force_used" in data
    # The worktree directory should be gone
    assert not Path(data["worktree_path"]).exists()


# ---------------------------------------------------------------------------
# 9. workspace remove — force
# ---------------------------------------------------------------------------


def test_cli_workspace_remove_force(book_repo: tuple[Path, str]) -> None:
    """run_workspace_remove with force=True should succeed and set force_used=True."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    # Create agent worktree and add an unmerged commit so safe delete fails
    wt = create_worktree(repo, "agent/remover-force-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"

    book_data = json.loads(wt_book_path.read_text(encoding="utf-8"))
    book_data["chapters"][0]["blocks"][0]["text"] = "Unmerged change."
    wt_book_path.write_text(json.dumps(book_data, indent=2), encoding="utf-8")

    subprocess.run(
        ["git", "add", "."], cwd=wt.worktree_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "unmerged"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    code, data = _capture_stdout(
        run_workspace_remove, work=work, branch="agent/remover-force-1", force=True
    )

    assert code == 0
    assert data["removed"] is True
    assert data["force_used"] is True
    assert data["branch_deleted"] is True


# ---------------------------------------------------------------------------
# 10. workspace gc — fresh repo (nothing to GC)
# ---------------------------------------------------------------------------


def test_cli_workspace_gc(book_repo: tuple[Path, str]) -> None:
    """run_workspace_gc should succeed and return JSON with removed/skipped/pruned/dry_run."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    code, data = _capture_stdout(
        run_workspace_gc, work=work, max_age_days=7, dry_run=False
    )

    assert code == 0
    assert "removed" in data
    assert "skipped" in data
    assert "pruned" in data
    assert "dry_run" in data
    assert data["dry_run"] is False
    assert isinstance(data["removed"], list)
    assert isinstance(data["skipped"], list)
    assert isinstance(data["pruned"], int)


# ---------------------------------------------------------------------------
# 11. workspace gc — dry_run=True
# ---------------------------------------------------------------------------


def test_cli_workspace_gc_dry_run(book_repo: tuple[Path, str]) -> None:
    """run_workspace_gc with dry_run=True should report dry_run=True in output."""
    repo, work_dir_rel = book_repo
    work = repo / work_dir_rel

    code, data = _capture_stdout(
        run_workspace_gc, work=work, max_age_days=7, dry_run=True
    )

    assert code == 0
    assert data["dry_run"] is True
    # In dry-run mode nothing is actually removed
    assert data["removed"] == []


# ---------------------------------------------------------------------------
# Additional: workspace list not-a-repo raises CommandError
# ---------------------------------------------------------------------------


def test_cli_workspace_list_not_a_repo(tmp_path: Path) -> None:
    """run_workspace_list should raise CommandError when work dir is not in a git repo."""
    work = tmp_path / "not_a_repo" / "work"
    work.mkdir(parents=True)
    # Create edit_state/book.json so ensure_work_dir passes
    edit_state = work / "edit_state"
    edit_state.mkdir()
    (edit_state / "book.json").write_text("{}", encoding="utf-8")

    with pytest.raises(CommandError) as exc_info:
        run_workspace_list(work=work, agent_only=False)

    assert exc_info.value.exit_code == 1
    assert "not_a_repo" in exc_info.value.payload.get("kind", "")
