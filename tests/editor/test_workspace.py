"""Tests for Sub-phase 7A, 7B, 7C, and 7D: Git workspace foundation layer.

Covers (7A):
- find_repo_root / GitError for non-repo paths
- _validate_branch_name accept/reject cases
- _run_git basic success, check-failure, and timeout
- _default_worktree_path naming convention
- _ensure_path_safe accept/reject cases
- _parse_worktree_porcelain standard and bare blocks
- _get_head_sha returns a valid SHA string

Covers (7B):
- create_worktree basic, custom path, base_ref, error cases, default path naming
- list_worktrees initial state, with agent worktrees, agent_only filter

Covers (7C):
- remove_worktree merged, unmerged, force, main rejection
- gc_worktrees stale removal, dry run, skip merged

Covers (7D):
- merge_and_validate no-conflict, git-conflict, semantic-conflict, parse-error
- merge rollback verification
- abort_merge
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from epubforge.editor.workspace import (
    GCResult,
    GitError,
    IntegrationResult,
    MergeOutcome,
    WorktreeCreateResult,
    WorktreeInfo,
    WorktreeRemoveResult,
    _DEFAULT_GIT_TIMEOUT,
    _default_worktree_path,
    _ensure_path_safe,
    _get_head_sha,
    _parse_conflict_files,
    _parse_worktree_porcelain,
    _run_git,
    _validate_branch_name,
    abort_merge,
    create_worktree,
    find_repo_root,
    gc_worktrees,
    list_worktrees,
    merge_and_validate,
    remove_worktree,
)
from epubforge.ir.semantic import (
    Book,
    Chapter,
    Heading,
    Paragraph,
    Provenance,
)
from epubforge.io import save_book


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
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


# ---------------------------------------------------------------------------
# find_repo_root
# ---------------------------------------------------------------------------


def test_find_repo_root(git_repo: Path) -> None:
    """find_repo_root should return the repo root from a sub-directory."""
    subdir = git_repo / "a" / "b"
    subdir.mkdir(parents=True)
    root = find_repo_root(subdir)
    assert root == git_repo.resolve()


def test_find_repo_root_from_root(git_repo: Path) -> None:
    """find_repo_root should work when given the repo root itself."""
    root = find_repo_root(git_repo)
    assert root == git_repo.resolve()


def test_find_repo_root_not_git(tmp_path: Path) -> None:
    """find_repo_root should raise GitError for a non-Git directory."""
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()
    with pytest.raises(GitError):
        find_repo_root(non_git)


# ---------------------------------------------------------------------------
# _validate_branch_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "branch",
    [
        "agent/scanner-1",
        "agent/fixer-ch1-a3x",
        "agent/s.1",
        "agent/scanner-1/ch-1",
        "agent/a",
        "agent/foo_bar",
    ],
)
def test_validate_branch_name_valid(branch: str) -> None:
    """Valid branch names should not raise."""
    _validate_branch_name(branch)  # must not raise


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "master",
        "../escape",
        "agent/../escape",
        "agent/",
        "agent/.hidden",
        "agent/-start",
        "",
        "refs/heads/agent/foo",
        "agent/foo bar",
        "feature/foo",
    ],
)
def test_validate_branch_name_invalid(branch: str) -> None:
    """Invalid branch names should raise ValueError."""
    with pytest.raises(ValueError):
        _validate_branch_name(branch)


# ---------------------------------------------------------------------------
# _run_git
# ---------------------------------------------------------------------------


def test_run_git_basic(git_repo: Path) -> None:
    """_run_git should successfully run a simple git command."""
    result = _run_git(["rev-parse", "HEAD"], cwd=git_repo)
    assert result.returncode == 0
    assert len(result.stdout.strip()) == 40  # full SHA


def test_run_git_check_failure(git_repo: Path) -> None:
    """_run_git with check=True should raise GitError on non-zero exit."""
    with pytest.raises(GitError) as exc_info:
        _run_git(["no-such-command"], cwd=git_repo)
    err = exc_info.value
    assert err.returncode != 0
    assert "no-such-command" in str(err) or err.returncode != 0


def test_run_git_check_false_no_raise(git_repo: Path) -> None:
    """_run_git with check=False should not raise even on non-zero exit."""
    result = _run_git(["no-such-command"], cwd=git_repo, check=False)
    assert result.returncode != 0


def test_run_git_timeout(git_repo: Path) -> None:
    """_run_git should wrap TimeoutExpired as GitError with returncode -1."""
    from unittest.mock import patch

    # Simulate subprocess.run raising TimeoutExpired so the test is deterministic
    # and environment-independent (no need to spin up a real slow process).
    with patch(
        "epubforge.editor.workspace.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git", "log"], timeout=1),
    ):
        with pytest.raises(GitError) as exc_info:
            _run_git(["log"], cwd=git_repo, timeout=1)

    assert exc_info.value.returncode == -1


# ---------------------------------------------------------------------------
# _default_worktree_path
# ---------------------------------------------------------------------------


def test_default_worktree_path(tmp_path: Path) -> None:
    """_default_worktree_path should produce the conventional sibling path."""
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    wt = _default_worktree_path(repo_root, "agent/scanner-1")
    # Expected: tmp_path / "myrepo-agent-scanner-1"
    assert wt == tmp_path / "myrepo-agent-scanner-1"


def test_default_worktree_path_nested_branch(tmp_path: Path) -> None:
    """Slashes in branch names should be replaced with dashes."""
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    wt = _default_worktree_path(repo_root, "agent/scanner-1/ch-1")
    assert wt == tmp_path / "myrepo-agent-scanner-1-ch-1"


# ---------------------------------------------------------------------------
# _ensure_path_safe
# ---------------------------------------------------------------------------


def test_ensure_path_safe_sibling(tmp_path: Path) -> None:
    """A sibling directory of repo_root should be accepted."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sibling = tmp_path / "repo-agent-scanner-1"
    sibling.mkdir()
    # Should not raise — sibling is under tmp_path (repo_root.parent)
    _ensure_path_safe(sibling, repo_root)


def test_ensure_path_safe_nested_inside_parent(tmp_path: Path) -> None:
    """A path deeply nested under repo_root.parent should be accepted."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    nested = tmp_path / "worktrees" / "deep"
    nested.mkdir(parents=True)
    _ensure_path_safe(nested, repo_root)


def test_ensure_path_safe_escape(tmp_path: Path) -> None:
    """A path outside repo_root.parent should be rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # Go two levels above tmp_path — guaranteed to escape.
    escape = tmp_path.parent.parent / "etc"
    with pytest.raises(ValueError, match="escapes"):
        _ensure_path_safe(escape, repo_root)


# ---------------------------------------------------------------------------
# _parse_worktree_porcelain
# ---------------------------------------------------------------------------

_PORCELAIN_STANDARD = """\
worktree /path/to/repo
HEAD abc123def456abc123def456abc123def456abc1
branch refs/heads/main

worktree /path/to/worktree-1
HEAD def456abc123def456abc123def456abc123def4
branch refs/heads/agent/scanner-1
"""

_PORCELAIN_BARE = """\
worktree /path/to/bare.git
HEAD 0000000000000000000000000000000000000000
bare

worktree /path/to/linked
HEAD 1111111111111111111111111111111111111111
branch refs/heads/feature/x
prunable gitdir file points to non-existent location
"""

_PORCELAIN_DETACHED = """\
worktree /path/to/repo
HEAD abc123def456abc123def456abc123def456abc1
branch refs/heads/main

worktree /path/to/detached
HEAD deadbeefdeadbeefdeadbeefdeadbeefdeadbeef
detached
"""


def test_parse_worktree_porcelain() -> None:
    """Standard porcelain output should parse into correct WorktreeInfo entries."""
    entries = _parse_worktree_porcelain(_PORCELAIN_STANDARD)
    assert len(entries) == 2

    main = entries[0]
    assert main.path == Path("/path/to/repo")
    assert main.branch == "main"
    assert main.commit == "abc123def456abc123def456abc123def456abc1"
    assert main.is_main is True
    assert main.is_bare is False
    assert main.prunable is False

    agent = entries[1]
    assert agent.path == Path("/path/to/worktree-1")
    assert agent.branch == "agent/scanner-1"
    assert agent.is_main is False
    assert agent.is_bare is False
    assert agent.prunable is False


def test_parse_worktree_porcelain_bare() -> None:
    """Bare worktree blocks and prunable lines should be parsed correctly."""
    entries = _parse_worktree_porcelain(_PORCELAIN_BARE)
    assert len(entries) == 2

    bare = entries[0]
    assert bare.is_bare is True
    assert bare.is_main is True
    assert bare.prunable is False

    linked = entries[1]
    assert linked.branch == "feature/x"
    assert linked.is_bare is False
    assert linked.prunable is True


def test_parse_worktree_porcelain_detached() -> None:
    """Detached HEAD worktrees should have an empty branch string."""
    entries = _parse_worktree_porcelain(_PORCELAIN_DETACHED)
    assert len(entries) == 2
    detached = entries[1]
    assert detached.branch == ""
    assert detached.is_bare is False


def test_parse_worktree_porcelain_empty() -> None:
    """Empty input should return an empty list without raising."""
    assert _parse_worktree_porcelain("") == []


# ---------------------------------------------------------------------------
# _get_head_sha
# ---------------------------------------------------------------------------


def test_get_head_sha(git_repo: Path) -> None:
    """_get_head_sha should return a non-empty abbreviated SHA string."""
    sha = _get_head_sha(git_repo)
    # Abbreviated SHA is typically 7+ characters, all hex.
    assert len(sha) >= 4
    assert all(c in "0123456789abcdef" for c in sha)


# ---------------------------------------------------------------------------
# Sub-phase 7B: create_worktree
# ---------------------------------------------------------------------------


def test_create_worktree_basic(git_repo: Path) -> None:
    """create_worktree should create a worktree directory and a new branch."""
    result = create_worktree(git_repo, "agent/scanner-1")

    assert isinstance(result, WorktreeCreateResult)
    # The default path should be a sibling of the repo root.
    assert result.worktree_path.exists()
    assert result.branch == "agent/scanner-1"
    # commit SHA should be non-empty and hex characters only.
    assert len(result.commit) >= 4
    assert all(c in "0123456789abcdef" for c in result.commit)

    # The branch must appear in `git branch` output.
    git_branch_out = subprocess.run(
        ["git", "branch"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "agent/scanner-1" in git_branch_out

    # Clean up: remove the worktree so tmp_path teardown works smoothly.
    subprocess.run(
        ["git", "worktree", "remove", str(result.worktree_path), "--force"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


def test_create_worktree_custom_path(git_repo: Path, tmp_path: Path) -> None:
    """create_worktree should use the provided worktree_path instead of default."""
    custom = tmp_path / "custom-worktree"
    result = create_worktree(git_repo, "agent/fixer-1", worktree_path=custom)

    assert result.worktree_path == custom
    assert custom.exists()

    subprocess.run(
        ["git", "worktree", "remove", str(custom), "--force"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


def test_create_worktree_base_ref(git_repo: Path) -> None:
    """create_worktree should accept a non-HEAD base_ref (tagged commit)."""
    # Create a tag pointing to the current HEAD so we have a non-HEAD ref.
    subprocess.run(
        ["git", "tag", "v1.0"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = create_worktree(git_repo, "agent/tagger-1", base_ref="v1.0")
    assert result.worktree_path.exists()
    assert result.commit  # non-empty SHA

    subprocess.run(
        ["git", "worktree", "remove", str(result.worktree_path), "--force"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


def test_create_worktree_branch_exists(git_repo: Path) -> None:
    """create_worktree should raise GitError when the branch already exists."""
    # Create the branch first.
    subprocess.run(
        ["git", "branch", "agent/dup-1"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(GitError):
        create_worktree(git_repo, "agent/dup-1")


def test_create_worktree_path_exists(git_repo: Path, tmp_path: Path) -> None:
    """create_worktree should raise GitError when the target path already exists."""
    existing = tmp_path / "already-there"
    existing.mkdir()

    with pytest.raises(GitError, match="already exists"):
        create_worktree(git_repo, "agent/clash-1", worktree_path=existing)


def test_create_worktree_default_path(git_repo: Path) -> None:
    """create_worktree default path should follow <repo_name>-<branch_slug> convention."""
    result = create_worktree(git_repo, "agent/scanner-99")

    repo_name = git_repo.name  # "repo"
    expected_name = f"{repo_name}-agent-scanner-99"
    assert result.worktree_path.name == expected_name
    assert result.worktree_path.parent == git_repo.parent

    subprocess.run(
        ["git", "worktree", "remove", str(result.worktree_path), "--force"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Sub-phase 7B: list_worktrees
# ---------------------------------------------------------------------------


def test_list_worktrees_initial(git_repo: Path) -> None:
    """A fresh repo should have exactly one worktree that is the main one."""
    worktrees = list_worktrees(git_repo)
    assert len(worktrees) == 1
    main = worktrees[0]
    assert main.is_main is True
    assert main.path == git_repo.resolve()


def test_list_worktrees_with_agents(git_repo: Path) -> None:
    """After creating two agent worktrees, list_worktrees should return 3 entries."""
    wt1 = create_worktree(git_repo, "agent/alpha-1")
    wt2 = create_worktree(git_repo, "agent/beta-2")

    worktrees = list_worktrees(git_repo)
    assert len(worktrees) == 3

    paths = {wt.path for wt in worktrees}
    assert wt1.worktree_path in paths
    assert wt2.worktree_path in paths

    # Clean up
    for wt in [wt1, wt2]:
        subprocess.run(
            ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )


def test_list_worktrees_agent_only(git_repo: Path) -> None:
    """agent_only=True should exclude the main worktree and any non-agent branches."""
    wt1 = create_worktree(git_repo, "agent/gamma-3")

    # agent_only should exclude main worktree
    agent_worktrees = list_worktrees(git_repo, agent_only=True)
    assert len(agent_worktrees) == 1
    assert agent_worktrees[0].branch == "agent/gamma-3"
    # Main worktree must not appear
    assert all(not wt.is_main for wt in agent_worktrees)

    subprocess.run(
        ["git", "worktree", "remove", str(wt1.worktree_path), "--force"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Sub-phase 7C: remove_worktree
# ---------------------------------------------------------------------------


def _make_commit_in_worktree(worktree_path: Path, filename: str = "change.txt") -> None:
    """Helper: add a file and commit in the given worktree directory."""
    (worktree_path / filename).write_text("content", encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"add {filename}"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
    )


def test_remove_worktree_merged(git_repo: Path) -> None:
    """After merging a branch, remove_worktree should delete the worktree path and branch."""
    # Create worktree and make a commit in it
    wt = create_worktree(git_repo, "agent/rm-merged-1")
    _make_commit_in_worktree(wt.worktree_path)

    # Merge the branch into main repo (fast-forward or no-ff both fine)
    subprocess.run(
        ["git", "merge", "--no-ff", "agent/rm-merged-1", "-m", "merge agent"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    # Now remove the worktree
    result = remove_worktree(git_repo, "agent/rm-merged-1")

    assert isinstance(result, WorktreeRemoveResult)
    assert result.branch == "agent/rm-merged-1"
    assert result.branch_deleted is True
    assert result.force_used is False
    # Worktree path should no longer exist
    assert not wt.worktree_path.exists()
    # Branch should no longer exist in git
    branch_check = subprocess.run(
        ["git", "branch", "--list", "agent/rm-merged-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "agent/rm-merged-1" not in branch_check.stdout


def test_remove_worktree_unmerged_no_force(git_repo: Path) -> None:
    """With force=False, an unmerged branch's worktree should be removed but branch kept."""
    wt = create_worktree(git_repo, "agent/rm-unmerged-1")
    # Make an unmerged commit in the worktree
    _make_commit_in_worktree(wt.worktree_path)

    # Remove without force — worktree should be removed, but branch should be kept
    result = remove_worktree(git_repo, "agent/rm-unmerged-1", force=False)

    assert isinstance(result, WorktreeRemoveResult)
    assert result.branch == "agent/rm-unmerged-1"
    assert result.force_used is False
    # Worktree directory must be gone
    assert not wt.worktree_path.exists()
    # Branch_deleted should be False because branch was not merged
    assert result.branch_deleted is False
    # Branch must still exist (unmerged, not force deleted)
    branch_check = subprocess.run(
        ["git", "branch", "--list", "agent/rm-unmerged-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "agent/rm-unmerged-1" in branch_check.stdout

    # Cleanup remaining branch
    subprocess.run(
        ["git", "branch", "-D", "agent/rm-unmerged-1"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


def test_remove_worktree_unmerged_force(git_repo: Path) -> None:
    """With force=True, an unmerged worktree and its branch should both be deleted."""
    wt = create_worktree(git_repo, "agent/rm-force-1")
    # Make an unmerged commit in the worktree
    _make_commit_in_worktree(wt.worktree_path)

    result = remove_worktree(git_repo, "agent/rm-force-1", force=True)

    assert isinstance(result, WorktreeRemoveResult)
    assert result.branch == "agent/rm-force-1"
    assert result.force_used is True
    assert result.branch_deleted is True
    # Both worktree path and branch should be gone
    assert not wt.worktree_path.exists()
    branch_check = subprocess.run(
        ["git", "branch", "--list", "agent/rm-force-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "agent/rm-force-1" not in branch_check.stdout


def test_remove_worktree_main_rejected(git_repo: Path) -> None:
    """Attempting to remove the main worktree should raise GitError."""
    # Find the branch name of the main worktree
    worktrees = list_worktrees(git_repo)
    main_wt = next(wt for wt in worktrees if wt.is_main)
    main_branch = main_wt.branch  # e.g. "master" or "main"

    with pytest.raises(GitError, match="cannot remove main worktree"):
        remove_worktree(git_repo, main_branch)


# ---------------------------------------------------------------------------
# Sub-phase 7C: gc_worktrees
# ---------------------------------------------------------------------------


def test_gc_no_stale(git_repo: Path) -> None:
    """With no agent worktrees at all, gc should return empty removed list."""
    result = gc_worktrees(git_repo, max_age_days=0)

    assert isinstance(result, GCResult)
    assert result.removed == []
    # pruned may be 0 or higher (no stale entries expected in fresh repo)
    assert result.pruned >= 0


def test_gc_removes_stale(git_repo: Path) -> None:
    """A worktree with a commit older than max_age_days=0 should be removed."""
    wt = create_worktree(git_repo, "agent/gc-stale-1")

    # Make a commit in the worktree using a very old date to simulate staleness.
    old_date = "2000-01-01T00:00:00"
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": old_date,
        "GIT_COMMITTER_DATE": old_date,
    }
    (wt.worktree_path / "stale.txt").write_text("old", encoding="utf-8")
    subprocess.run(
        ["git", "add", "stale.txt"],
        cwd=wt.worktree_path,
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "-m", "old commit"],
        cwd=wt.worktree_path,
        check=True,
        capture_output=True,
        env=env,
    )

    # With max_age_days=0 any commit older than now should qualify
    result = gc_worktrees(git_repo, max_age_days=0)

    assert isinstance(result, GCResult)
    removed_branches = [r.branch for r in result.removed]
    assert "agent/gc-stale-1" in removed_branches
    # Worktree directory should be gone
    assert not wt.worktree_path.exists()


def test_gc_dry_run(git_repo: Path) -> None:
    """With dry_run=True, gc should report candidates without actually deleting."""
    wt = create_worktree(git_repo, "agent/gc-dry-1")

    # Make an old commit
    old_date = "2000-01-01T00:00:00"
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": old_date,
        "GIT_COMMITTER_DATE": old_date,
    }
    (wt.worktree_path / "dry.txt").write_text("dry", encoding="utf-8")
    subprocess.run(
        ["git", "add", "dry.txt"],
        cwd=wt.worktree_path,
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "-m", "dry run commit"],
        cwd=wt.worktree_path,
        check=True,
        capture_output=True,
        env=env,
    )

    result = gc_worktrees(git_repo, max_age_days=0, dry_run=True)

    assert isinstance(result, GCResult)
    # Nothing should have been removed
    assert result.removed == []
    # The dry_run skip reason should appear for this branch
    dry_run_skips = [s for s in result.skipped if "agent/gc-dry-1" in s and "dry_run" in s]
    assert len(dry_run_skips) >= 1
    # Worktree directory must still exist
    assert wt.worktree_path.exists()

    # Cleanup
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", "agent/gc-dry-1"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


def test_gc_skips_merged(git_repo: Path) -> None:
    """A worktree whose branch has been merged should be skipped by gc."""
    wt = create_worktree(git_repo, "agent/gc-merged-1")

    # Make an old commit in the worktree
    old_date = "2000-01-01T00:00:00"
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": old_date,
        "GIT_COMMITTER_DATE": old_date,
    }
    (wt.worktree_path / "merged.txt").write_text("merged", encoding="utf-8")
    subprocess.run(
        ["git", "add", "merged.txt"],
        cwd=wt.worktree_path,
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "-m", "merged commit"],
        cwd=wt.worktree_path,
        check=True,
        capture_output=True,
        env=env,
    )

    # Merge the branch into main repo so it appears in --merged
    subprocess.run(
        ["git", "merge", "--no-ff", "agent/gc-merged-1", "-m", "merge for gc test"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    # Run GC — the merged worktree should be skipped, not removed
    result = gc_worktrees(git_repo, max_age_days=0)

    assert isinstance(result, GCResult)
    removed_branches = [r.branch for r in result.removed]
    assert "agent/gc-merged-1" not in removed_branches
    # Check it appears in the skipped list with the "merged" reason
    merged_skips = [
        s for s in result.skipped
        if "agent/gc-merged-1" in s and "merged" in s
    ]
    assert len(merged_skips) >= 1
    # Worktree should still be present (not removed by gc)
    assert wt.worktree_path.exists()

    # Cleanup
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", "agent/gc-merged-1"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Sub-phase 7D helpers
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


def _init_book_in_repo(repo: Path, book: Book, work_dir_rel: str = "work/book") -> Path:
    """Write book.json into repo/work_dir_rel/edit_state/ and return work_dir."""
    work_dir = repo / work_dir_rel
    # save_book uses resolve_book_path which checks if work_dir is a directory.
    # Create it first so that resolve_book_path correctly appends edit_state/book.json.
    work_dir.mkdir(parents=True, exist_ok=True)
    save_book(book, work_dir)
    return work_dir


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
    book = _minimal_book()
    _init_book_in_repo(repo, book, work_dir_rel)

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial book"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo, work_dir_rel


# ---------------------------------------------------------------------------
# Sub-phase 7D: _parse_conflict_files
# ---------------------------------------------------------------------------


def test_parse_conflict_files_basic() -> None:
    """_parse_conflict_files should extract file paths from CONFLICT lines."""
    stdout = (
        "Auto-merging edit_state/book.json\n"
        "CONFLICT (content): Merge conflict in edit_state/book.json\n"
        "Automatic merge failed; fix conflicts and then commit the result.\n"
    )
    files = _parse_conflict_files(stdout, "")
    assert files == ["edit_state/book.json"]


def test_parse_conflict_files_multiple() -> None:
    """_parse_conflict_files should handle multiple conflict files."""
    stdout = (
        "CONFLICT (content): Merge conflict in a.txt\n"
        "CONFLICT (content): Merge conflict in b/c.txt\n"
    )
    files = _parse_conflict_files(stdout, "")
    assert files == ["a.txt", "b/c.txt"]


def test_parse_conflict_files_empty() -> None:
    """_parse_conflict_files should return empty list for no conflicts."""
    files = _parse_conflict_files("Everything up to date", "")
    assert files == []


# ---------------------------------------------------------------------------
# Sub-phase 7D: merge_and_validate
# ---------------------------------------------------------------------------


def test_merge_no_conflict(book_repo: tuple[Path, str]) -> None:
    """Merge with non-conflicting block text change should succeed."""
    repo, work_dir_rel = book_repo

    # Create worktree and modify book.json
    wt = create_worktree(repo, "agent/scanner-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"

    # Load, modify, and save the book in the worktree
    import json
    book_data = json.loads(wt_book_path.read_text(encoding="utf-8"))
    book_data["chapters"][0]["blocks"][0]["text"] = "Modified text."
    wt_book_path.write_text(json.dumps(book_data, indent=2), encoding="utf-8")

    # Commit the change in the worktree
    subprocess.run(
        ["git", "add", "."],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "modify block text"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    # Merge back
    result = merge_and_validate(repo, work_dir_rel, "agent/scanner-1")

    assert isinstance(result, IntegrationResult)
    assert result.outcome.status == "accepted"
    assert result.outcome.message == "merge validated"
    assert result.change_count > 0
    assert result.merge_commit is not None
    assert result.pre_merge_sha is not None
    assert result.base_sha256 is not None
    assert result.merged_sha256 is not None
    assert result.patch_json is not None
    assert result.conflict_files == []

    # Cleanup
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=repo, check=True, capture_output=True,
    )


def test_merge_git_conflict(book_repo: tuple[Path, str]) -> None:
    """Merge with conflicting changes on the same line should report git_conflict."""
    repo, work_dir_rel = book_repo

    # Create worktree
    wt = create_worktree(repo, "agent/conflict-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"
    main_book_path = repo / work_dir_rel / "edit_state" / "book.json"

    import json

    # Modify the same block text in the worktree
    book_data = json.loads(wt_book_path.read_text(encoding="utf-8"))
    book_data["chapters"][0]["blocks"][0]["text"] = "Worktree version."
    wt_book_path.write_text(json.dumps(book_data, indent=2), encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "worktree change"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    # Modify the same block text in the main repo
    book_data2 = json.loads(main_book_path.read_text(encoding="utf-8"))
    book_data2["chapters"][0]["blocks"][0]["text"] = "Main repo version."
    main_book_path.write_text(json.dumps(book_data2, indent=2), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main change"],
        cwd=repo, check=True, capture_output=True,
    )

    # Record pre-merge SHA
    pre_sha = _get_head_sha(repo)

    # Merge should fail with git_conflict
    result = merge_and_validate(repo, work_dir_rel, "agent/conflict-1")

    assert result.outcome.status == "git_conflict"
    assert result.conflict_files  # should have at least one conflict file
    # HEAD should be back to pre-merge state
    assert _get_head_sha(repo) == pre_sha

    # Cleanup
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=repo, check=True, capture_output=True,
    )


def test_merge_semantic_conflict(book_repo: tuple[Path, str]) -> None:
    """Git merge succeeds but book-level field change causes DiffError (semantic_conflict)."""
    repo, work_dir_rel = book_repo

    # Create worktree
    wt = create_worktree(repo, "agent/semantic-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"

    import json

    # Change the book title in the worktree — diff_books rejects book-level field changes
    book_data = json.loads(wt_book_path.read_text(encoding="utf-8"))
    book_data["title"] = "A Different Title"
    wt_book_path.write_text(json.dumps(book_data, indent=2), encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "change title"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    # Record pre-merge SHA
    pre_sha = _get_head_sha(repo)

    result = merge_and_validate(repo, work_dir_rel, "agent/semantic-1")

    assert result.outcome.status == "semantic_conflict"
    assert "title" in result.outcome.message.lower() or "unsupported" in result.outcome.message.lower()
    # HEAD should be rolled back
    assert _get_head_sha(repo) == pre_sha

    # Cleanup
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=repo, check=True, capture_output=True,
    )


def test_merge_parse_error(book_repo: tuple[Path, str]) -> None:
    """Git merge succeeds but merged book.json is invalid JSON -> parse_error."""
    repo, work_dir_rel = book_repo

    # Create worktree
    wt = create_worktree(repo, "agent/parse-err-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"

    # Write invalid JSON
    wt_book_path.write_text("{invalid json!!!", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "break json"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    pre_sha = _get_head_sha(repo)

    result = merge_and_validate(repo, work_dir_rel, "agent/parse-err-1")

    assert result.outcome.status == "parse_error"
    assert _get_head_sha(repo) == pre_sha

    # Cleanup
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=repo, check=True, capture_output=True,
    )


def test_merge_rollback_on_semantic_failure(book_repo: tuple[Path, str]) -> None:
    """After a semantic failure the HEAD SHA should match pre_merge_sha."""
    repo, work_dir_rel = book_repo

    wt = create_worktree(repo, "agent/rollback-1")
    wt_book_path = wt.worktree_path / work_dir_rel / "edit_state" / "book.json"

    import json
    # Change book title to trigger DiffError
    book_data = json.loads(wt_book_path.read_text(encoding="utf-8"))
    book_data["title"] = "Rollback Test Title"
    wt_book_path.write_text(json.dumps(book_data, indent=2), encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "trigger rollback"],
        cwd=wt.worktree_path, check=True, capture_output=True,
    )

    pre_sha = _get_head_sha(repo)
    result = merge_and_validate(repo, work_dir_rel, "agent/rollback-1")

    assert result.outcome.status == "semantic_conflict"
    assert result.pre_merge_sha == pre_sha
    # Verify HEAD matches pre_merge_sha
    assert _get_head_sha(repo) == pre_sha

    # Cleanup
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=repo, check=True, capture_output=True,
    )


def test_merge_branch_not_found(book_repo: tuple[Path, str]) -> None:
    """Merging a non-existent branch should return git_conflict with descriptive message."""
    repo, work_dir_rel = book_repo

    result = merge_and_validate(repo, work_dir_rel, "agent/nonexistent-999")

    assert result.outcome.status == "git_conflict"
    assert "does not exist" in result.outcome.message


def test_merge_dirty_working_tree(book_repo: tuple[Path, str]) -> None:
    """Merge should be rejected when the working tree has uncommitted changes."""
    repo, work_dir_rel = book_repo

    # Create an uncommitted file
    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")

    # Create a branch that exists (we need it to pass the branch-exists check)
    wt = create_worktree(repo, "agent/dirty-1")

    result = merge_and_validate(repo, work_dir_rel, "agent/dirty-1")

    assert result.outcome.status == "git_conflict"
    assert "uncommitted" in result.outcome.message

    # Cleanup
    (repo / "dirty.txt").unlink()
    subprocess.run(
        ["git", "worktree", "remove", str(wt.worktree_path), "--force"],
        cwd=repo, check=True, capture_output=True,
    )
