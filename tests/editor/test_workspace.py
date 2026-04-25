"""Tests for Sub-phase 7A and 7B: Git workspace foundation layer.

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
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from epubforge.editor.workspace import (
    GitError,
    WorktreeCreateResult,
    WorktreeInfo,
    _DEFAULT_GIT_TIMEOUT,
    _default_worktree_path,
    _ensure_path_safe,
    _get_head_sha,
    _parse_worktree_porcelain,
    _run_git,
    _validate_branch_name,
    create_worktree,
    find_repo_root,
    list_worktrees,
)


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
