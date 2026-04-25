"""Git workspace operations for the agentic editing workflow.

This module provides a subprocess-based wrapper around Git CLI commands.
All Git operations use ``git`` from the system PATH; no external Python
Git libraries are used (PD1).

Public API (Sub-phase 7A):
    - GitError          — exception raised on Git command failures
    - find_repo_root    — locate the Git repository root
    - WorktreeInfo      — parsed worktree descriptor (used by 7B onwards)

Public API (Sub-phase 7B):
    - WorktreeCreateResult — result of create_worktree
    - create_worktree      — create a new Git worktree with a new branch
    - list_worktrees       — list all worktrees, optionally filtering to agent/*

Internal helpers:
    - _run_git              — subprocess wrapper with timeout / check
    - _validate_branch_name — branch naming safety guard
    - _default_worktree_path — conventional worktree path derivation
    - _ensure_path_safe      — path-escape guard
    - _get_head_sha          — abbreviated HEAD SHA helper
    - _parse_worktree_porcelain — parser for ``git worktree list --porcelain``
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_GIT_TIMEOUT = 30  # seconds

# Branch name must be agent/<segment>[/<segment>...] where each segment
# consists solely of [A-Za-z0-9._-] characters.  Segments that start with
# a dot or hyphen are rejected by the extra checks in _validate_branch_name.
_BRANCH_PATTERN = re.compile(r"^agent/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    """Raised when a Git subprocess command fails or times out."""

    def __init__(self, message: str, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeInfo:
    """Describes a single Git worktree entry."""

    path: Path       # absolute filesystem path to the worktree
    branch: str      # branch name (e.g. "agent/scanner-1"), "" for detached HEAD
    commit: str      # HEAD commit SHA (abbreviated or full depending on source)
    is_bare: bool    # True when the worktree is bare
    is_main: bool    # True for the first (primary) worktree
    prunable: bool   # True when git considers this entry prunable


@dataclass(frozen=True)
class WorktreeCreateResult:
    """Result of creating a new Git worktree.

    Does not include ``work_dir``: the CLI layer computes it as
    ``worktree_path / work_dir_rel`` since it already knows the
    relative path from the positional ``work`` argument.
    """

    worktree_path: Path  # absolute path to the newly created worktree
    branch: str          # name of the branch created in this worktree
    commit: str          # HEAD commit SHA of the new worktree


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = _DEFAULT_GIT_TIMEOUT,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the CompletedProcess.

    Args:
        args: Git command arguments (without the leading ``git`` token).
        cwd: Working directory for the command.
        timeout: Maximum seconds before ``subprocess.TimeoutExpired`` is
            wrapped and re-raised as ``GitError``.
        check: If ``True`` and the command exits with a non-zero return code,
            raise ``GitError``.
        input_text: Optional text to pass on stdin.

    Returns:
        The ``subprocess.CompletedProcess[str]`` result.

    Raises:
        GitError: When the command times out or (if check=True) exits non-zero.
    """
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {' '.join(args[:3])} timed out after {timeout}s. "
            "If a merge was in progress, the working tree may be in a "
            "dirty state. Check for stale .git/index.lock.",
            returncode=-1,
            stderr=str(exc),
        ) from exc

    if check and result.returncode != 0:
        stderr_preview = result.stderr.strip()[:500]
        raise GitError(
            f"git {' '.join(args[:3])} failed (rc={result.returncode}): {stderr_preview}",
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return result


def _validate_branch_name(branch: str) -> None:
    """Validate *branch* against the project naming convention.

    Only ``agent/<kind>-<id>[/<sub>]`` patterns are accepted (PD3).
    Each path segment must consist solely of ``[A-Za-z0-9._-]`` characters
    and must not start with a dot or hyphen.

    Raises:
        ValueError: When the branch name does not conform.
    """
    if not branch:
        raise ValueError("branch name must not be empty")

    if not _BRANCH_PATTERN.fullmatch(branch):
        raise ValueError(
            f"branch name {branch!r} does not match required pattern "
            "'agent/<kind>-<id>[/<sub>]'; only alphanumeric, dot, dash, "
            "underscore allowed in each segment"
        )

    # Reject ".." anywhere in the name (path-traversal guard).
    if ".." in branch:
        raise ValueError(
            f"branch name {branch!r} contains '..'; path traversal is not allowed"
        )

    # Each segment (after stripping the "agent/" prefix) must not start with
    # a dot or a hyphen.
    segments = branch[len("agent/"):].split("/")
    for seg in segments:
        if seg.startswith("."):
            raise ValueError(
                f"branch segment {seg!r} starts with '.'; hidden segments are not allowed"
            )
        if seg.startswith("-"):
            raise ValueError(
                f"branch segment {seg!r} starts with '-'; this is not allowed"
            )


def _default_worktree_path(repo_root: Path, branch: str) -> Path:
    """Compute the default worktree directory path from *repo_root* and *branch*.

    Convention::

        <repo_root>/../<repo_name>-<branch_slug>/

    where ``branch_slug`` replaces ``/`` with ``-``.

    Note:
        Slug collision is theoretically possible (e.g. ``agent/a-b`` and
        ``agent/a/b`` both produce slug ``agent-a-b``).  In practice this is
        unlikely given the ``agent/<kind>-<id>`` naming convention.  If it
        does occur, ``git worktree add`` will fail because the target directory
        already exists, producing a clear error for the caller.
    """
    repo_name = repo_root.name
    branch_slug = branch.replace("/", "-")
    return repo_root.parent / f"{repo_name}-{branch_slug}"


def _ensure_path_safe(path: Path, repo_root: Path) -> None:
    """Verify that *path* does not escape the sibling directory space of *repo_root*.

    Allowed zone: ``repo_root.parent`` and its descendants (the same directory
    level as the repository root).  Path traversal into arbitrary filesystem
    locations is rejected.

    Args:
        path: The candidate path to validate.
        repo_root: The repository root used as the reference anchor.

    Raises:
        ValueError: When *path* escapes the allowed zone.
    """
    resolved = path.resolve()
    allowed_root = repo_root.parent.resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError:
        raise ValueError(
            f"path {path} escapes the expected parent directory {allowed_root}"
        )


def _get_head_sha(repo_root: Path) -> str:
    """Return the abbreviated HEAD SHA for the repository at *repo_root*.

    Uses ``git rev-parse --short HEAD`` for a compact identifier.
    """
    result = _run_git(
        ["rev-parse", "--short", "HEAD"],
        cwd=repo_root,
        timeout=5,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Porcelain parser
# ---------------------------------------------------------------------------


def _parse_worktree_porcelain(output: str) -> list[WorktreeInfo]:
    """Parse ``git worktree list --porcelain`` output into a list of WorktreeInfo.

    Each worktree block is separated by a blank line.  The expected block format::

        worktree /path/to/worktree
        HEAD <sha>
        branch refs/heads/<name>
        [bare]
        [prunable gitdir file points to non-existent location ...]

    Detached-HEAD worktrees have ``detached`` instead of a ``branch`` line.

    The first block (index 0) is always the main worktree (``is_main=True``).
    """
    worktrees: list[WorktreeInfo] = []
    # Split on blank lines; strip trailing whitespace from each line.
    blocks = re.split(r"\n\n+", output.strip())
    for block_index, block in enumerate(blocks):
        if not block.strip():
            continue

        path: Path | None = None
        commit: str = ""
        branch: str = ""
        is_bare: bool = False
        prunable: bool = False

        for line in block.splitlines():
            line = line.strip()
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):].strip())
            elif line.startswith("HEAD "):
                commit = line[len("HEAD "):].strip()
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/"):].strip()
            elif line == "bare":
                is_bare = True
            elif line.startswith("prunable"):
                prunable = True
            # "detached" line: branch stays "" (already initialized)

        if path is None:
            # Malformed block; skip gracefully (forward-compat with new Git versions).
            continue

        worktrees.append(
            WorktreeInfo(
                path=path,
                branch=branch,
                commit=commit,
                is_bare=is_bare,
                is_main=(block_index == 0),
                prunable=prunable,
            )
        )

    return worktrees


# ---------------------------------------------------------------------------
# Public API — Sub-phase 7A
# ---------------------------------------------------------------------------


def find_repo_root(path: Path) -> Path:
    """Find the Git repository root that contains *path*.

    Uses ``git rev-parse --show-toplevel``.

    Args:
        path: Any path inside (or equal to) a Git repository.

    Returns:
        The absolute Path to the repository root.

    Raises:
        GitError: When *path* is not inside a Git repository or the git
            command fails for any other reason.
    """
    cwd = path if path.is_dir() else path.parent
    result = _run_git(
        ["rev-parse", "--show-toplevel"],
        cwd=cwd,
        timeout=5,
    )
    return Path(result.stdout.strip())


# ---------------------------------------------------------------------------
# Public API — Sub-phase 7B
# ---------------------------------------------------------------------------


def create_worktree(
    repo_root: Path,
    branch: str,
    *,
    worktree_path: Path | None = None,
    base_ref: str = "HEAD",
    timeout: int = 30,
) -> WorktreeCreateResult:
    """Create a new Git worktree with a new branch.

    Flow:
        1. Validate the branch name (must match ``agent/<kind>-<id>`` pattern).
        2. Compute ``worktree_path`` if not provided (default naming convention).
        3. Verify the computed/given path does not escape the sibling space.
        4. Reject if ``worktree_path`` already exists on the filesystem.
        5. Run ``git worktree add <worktree_path> -b <branch> <base_ref>``.
        6. Verify that the worktree directory was created.
        7. Retrieve the HEAD commit SHA from the new worktree.
        8. Return ``WorktreeCreateResult``.

    Args:
        repo_root: Absolute path to the repository root.
        branch: New branch name; must match ``agent/<kind>-<id>[/<sub>]``.
        worktree_path: Explicit path for the new worktree directory.  When
            ``None`` the default convention is used:
            ``<repo_root>/../<repo_name>-<branch_slug>/``.
        base_ref: Git ref (branch name, tag, or commit SHA) to base the new
            branch on.  Defaults to ``HEAD``.
        timeout: Maximum seconds for the ``git worktree add`` command.

    Returns:
        A ``WorktreeCreateResult`` describing the newly created worktree.

    Raises:
        ValueError: When the branch name is invalid or the computed path
            escapes the allowed zone.
        GitError: When the Git command fails (e.g. branch already exists,
            invalid base_ref, filesystem error).
    """
    # Step 1: validate branch name
    _validate_branch_name(branch)

    # Step 2: resolve worktree path
    if worktree_path is None:
        worktree_path = _default_worktree_path(repo_root, branch)

    # Step 3: path safety check
    _ensure_path_safe(worktree_path, repo_root)

    # Step 4: reject if path already exists
    if worktree_path.exists():
        raise GitError(
            f"worktree path already exists: {worktree_path}",
            returncode=1,
            stderr="",
        )

    # Step 5: create the worktree
    _run_git(
        ["worktree", "add", str(worktree_path), "-b", branch, base_ref],
        cwd=repo_root,
        timeout=timeout,
    )

    # Step 6: verify the directory was created (defensive check)
    if not worktree_path.exists():
        raise GitError(
            f"git worktree add reported success but path does not exist: {worktree_path}",
            returncode=0,
            stderr="",
        )

    # Step 7: retrieve HEAD commit SHA from the new worktree
    commit = _get_head_sha(worktree_path)

    return WorktreeCreateResult(
        worktree_path=worktree_path,
        branch=branch,
        commit=commit,
    )


def list_worktrees(
    repo_root: Path,
    *,
    agent_only: bool = False,
    timeout: int = 10,
) -> list[WorktreeInfo]:
    """List all Git worktrees, optionally filtering to ``agent/*`` branches.

    Uses ``git worktree list --porcelain`` for stable, machine-readable output.

    Args:
        repo_root: Absolute path to the repository root.
        agent_only: When ``True``, return only worktrees whose branch name
            starts with ``agent/``.
        timeout: Maximum seconds for the ``git worktree list`` command.

    Returns:
        A list of ``WorktreeInfo`` entries.  The main worktree is always first
        (``is_main=True``) when ``agent_only=False``.

    Raises:
        GitError: When the Git command fails.
    """
    result = _run_git(
        ["worktree", "list", "--porcelain"],
        cwd=repo_root,
        timeout=timeout,
    )

    worktrees = _parse_worktree_porcelain(result.stdout)

    if agent_only:
        worktrees = [wt for wt in worktrees if wt.branch.startswith("agent/")]

    return worktrees


__all__ = [
    "GitError",
    "WorktreeInfo",
    "WorktreeCreateResult",
    "find_repo_root",
    "create_worktree",
    "list_worktrees",
    # Internal helpers exposed for testing and downstream sub-phases:
    "_DEFAULT_GIT_TIMEOUT",
    "_run_git",
    "_validate_branch_name",
    "_default_worktree_path",
    "_ensure_path_safe",
    "_get_head_sha",
    "_parse_worktree_porcelain",
]
