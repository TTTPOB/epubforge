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

Public API (Sub-phase 7C):
    - WorktreeRemoveResult — result of remove_worktree
    - GCResult             — result of gc_worktrees
    - remove_worktree      — remove a worktree and optionally its branch
    - gc_worktrees         — garbage-collect stale agent worktrees

Public API (Sub-phase 7D):
    - MergeOutcome         — outcome classification for a merge attempt
    - IntegrationResult    — full result of an integration merge attempt
    - merge_and_validate   — merge an agent branch with semantic validation
    - abort_merge          — abort an in-progress merge

Internal helpers:
    - _run_git              — subprocess wrapper with timeout / check
    - _validate_branch_name — branch naming safety guard
    - _default_worktree_path — conventional worktree path derivation
    - _ensure_path_safe      — path-escape guard
    - _get_head_sha          — abbreviated HEAD SHA helper
    - _parse_worktree_porcelain — parser for ``git worktree list --porcelain``
    - _parse_conflict_files — extract conflicting file paths from merge output
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from epubforge.ir.semantic import Book


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


@dataclass(frozen=True)
class WorktreeRemoveResult:
    """Result of removing a Git worktree."""

    worktree_path: Path   # absolute path of the removed worktree
    branch: str           # branch that was associated with the worktree
    branch_deleted: bool  # True when the branch was also deleted
    force_used: bool      # True when --force was passed to worktree remove


@dataclass(frozen=True)
class GCResult:
    """Result of garbage-collecting stale agent worktrees."""

    removed: list[WorktreeRemoveResult]  # worktrees that were removed
    skipped: list[str]                   # "path: reason" entries for skipped worktrees
    pruned: int                          # number of entries cleaned by git worktree prune


@dataclass(frozen=True)
class MergeOutcome:
    """Outcome classification for a merge attempt."""

    status: Literal["accepted", "git_conflict", "semantic_conflict", "parse_error"]
    message: str


@dataclass(frozen=True)
class IntegrationResult:
    """Full result of an integration merge attempt."""

    outcome: MergeOutcome
    branch: str                         # branch that was merged
    merge_commit: str | None            # merge commit SHA (if accepted)
    pre_merge_sha: str | None           # integration branch HEAD before merge
    base_sha256: str | None             # base book.json SHA256
    merged_sha256: str | None           # merged book.json SHA256
    change_count: int                   # number of changes in BookPatch
    patch_json: dict | None             # BookPatch JSON (if accepted)
    conflict_files: list[str]           # Git conflict file list


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


# ---------------------------------------------------------------------------
# Public API — Sub-phase 7C
# ---------------------------------------------------------------------------


def remove_worktree(
    repo_root: Path,
    branch: str,
    *,
    force: bool = False,
    delete_branch: bool = True,
    timeout: int = 30,
) -> WorktreeRemoveResult:
    """Remove a worktree and optionally its branch.

    Flow:
        1. Call ``list_worktrees`` to find the worktree path for *branch*.
        2. If not found, ``git worktree prune`` first, then proceed to
           branch deletion only (worktree path is already gone).
        3. Reject if the found worktree is the main worktree.
        4. ``git worktree remove <path> [--force]``
        5. If *delete_branch*:
           - Try ``git branch -d <branch>`` (safe delete, merged only).
           - If that fails and *force* is True, retry with ``-D``.
           - If it fails and *force* is False, skip branch deletion
             (``branch_deleted=False``).
        6. Return ``WorktreeRemoveResult``.

    Edge cases:
        - Worktree path does not exist but Git still has a record →
          run ``git worktree prune`` first, then delete branch.
        - Branch does not exist but worktree path does →
          remove worktree, skip branch deletion.

    Args:
        repo_root: Absolute path to the repository root.
        branch: Branch name to remove the worktree for.
        force: When ``True``, pass ``--force`` to ``git worktree remove``
            and use ``-D`` for the branch delete.
        delete_branch: When ``True`` (default), also delete the branch after
            removing the worktree.
        timeout: Maximum seconds for each git sub-command.

    Returns:
        A ``WorktreeRemoveResult`` describing what was done.

    Raises:
        GitError: When the worktree is the main worktree, or when a git
            command fails unexpectedly.
    """
    # Step 1: find the worktree for this branch
    worktrees = list_worktrees(repo_root, timeout=timeout)
    matched = [wt for wt in worktrees if wt.branch == branch]

    worktree_path: Path | None
    if matched:
        wt_info = matched[0]
        # Step 3: reject main worktree
        if wt_info.is_main:
            raise GitError(
                f"cannot remove main worktree (branch={branch!r})",
                returncode=1,
                stderr="",
            )
        worktree_path = wt_info.path
    else:
        # Step 2: branch not found in list — prune stale entries first
        _run_git(["worktree", "prune"], cwd=repo_root, timeout=timeout)
        # Re-scan after prune; if still absent, worktree was already gone
        worktrees_after = list_worktrees(repo_root, timeout=timeout)
        rematched = [wt for wt in worktrees_after if wt.branch == branch]
        if rematched:
            if rematched[0].is_main:
                raise GitError(
                    f"cannot remove main worktree (branch={branch!r})",
                    returncode=1,
                    stderr="",
                )
            worktree_path = rematched[0].path
        else:
            # Worktree is completely gone; fall through to branch deletion only
            worktree_path = None

    branch_deleted = False
    force_used = force

    # Step 4: remove the worktree directory if it still exists
    if worktree_path is not None:
        remove_args = ["worktree", "remove", str(worktree_path)]
        if force:
            remove_args.append("--force")
        _run_git(remove_args, cwd=repo_root, timeout=timeout)

    # Step 5: optionally delete the branch
    if delete_branch:
        # First check whether the branch actually exists
        branch_check = _run_git(
            ["rev-parse", "--verify", branch],
            cwd=repo_root,
            timeout=5,
            check=False,
        )
        if branch_check.returncode != 0:
            # Branch does not exist — nothing to delete
            branch_deleted = False
        else:
            # Try safe delete first (-d only succeeds for merged branches)
            safe_del = _run_git(
                ["branch", "-d", branch],
                cwd=repo_root,
                timeout=timeout,
                check=False,
            )
            if safe_del.returncode == 0:
                branch_deleted = True
            elif force:
                # Force delete for unmerged branch
                _run_git(["branch", "-D", branch], cwd=repo_root, timeout=timeout)
                branch_deleted = True
            else:
                # Branch not merged and force=False — leave the branch intact
                branch_deleted = False

    if worktree_path is None:
        # Construct a synthetic path for the result (already removed from fs)
        worktree_path = _default_worktree_path(repo_root, branch)

    return WorktreeRemoveResult(
        worktree_path=worktree_path,
        branch=branch,
        branch_deleted=branch_deleted,
        force_used=force_used,
    )


def gc_worktrees(
    repo_root: Path,
    *,
    max_age_days: int = 7,
    dry_run: bool = False,
    timeout: int = 60,
) -> GCResult:
    """Garbage-collect orphaned agent worktrees older than *max_age_days*.

    An agent worktree is considered a GC candidate when ALL of the
    following are true:

    1. Its branch matches the ``agent/*`` pattern.
    2. The branch has at least one commit beyond the fork point
       (``git log HEAD..<branch> --oneline`` has output).  Branches with no
       new commits are skipped to avoid false positives on freshly created
       worktrees whose ``git log -1`` would return the (potentially old)
       fork-point timestamp.
    3. Its last commit timestamp is older than ``max_age_days * 86400`` seconds.
    4. The branch has **not** been merged into the current HEAD.

    Flow:
        1. ``git worktree prune`` — removes stale Git records for deleted dirs.
        2. ``list_worktrees(agent_only=True)`` — get remaining agent worktrees.
        3. For each agent worktree:
           a. Check merged: ``git branch --merged HEAD`` includes branch?
              If yes → skip (reason: "branch merged, should be cleaned by remove").
           b. Check new commits: ``git log HEAD..<branch> --oneline``
              If empty → skip (reason: "no new commits (recently created)").
           c. Get last commit timestamp: ``git log -1 --format=%ct <branch>``
           d. Compute age; if age <= max_age_days * 86400 → skip (too young).
           e. Otherwise → add to candidates.
        4. For each candidate:
           - *dry_run* → add to skipped with reason "dry_run".
           - Otherwise → ``remove_worktree(force=True)`` → add to removed.
        5. Return ``GCResult``.

    Args:
        repo_root: Absolute path to the repository root.
        max_age_days: Worktrees whose last commit is older than this many days
            are eligible for removal.
        dry_run: When ``True``, report candidates but do not remove anything.
        timeout: Maximum seconds for the overall gc operation.  Individual
            sub-commands use a proportional share of this budget.

    Returns:
        A ``GCResult`` summarising what was done.

    Raises:
        GitError: When an unexpected git command failure occurs.
    """
    # Step 1: prune stale worktree records and count entries cleaned
    prune_result = _run_git(
        ["worktree", "prune", "--verbose"],
        cwd=repo_root,
        timeout=min(timeout, 10),
    )
    # Count pruned entries by counting non-empty lines in prune output
    pruned_lines = [
        line for line in prune_result.stdout.splitlines() if line.strip()
    ]
    pruned = len(pruned_lines)

    # Step 2: list remaining agent worktrees
    agent_worktrees = list_worktrees(repo_root, agent_only=True, timeout=10)

    removed: list[WorktreeRemoveResult] = []
    skipped: list[str] = []
    now = int(time.time())
    max_age_seconds = max_age_days * 86400

    for wt in agent_worktrees:
        branch = wt.branch
        wt_label = f"{wt.path}: {branch}"

        # Step 3a: check if already merged into HEAD
        merged_result = _run_git(
            ["branch", "--merged", "HEAD"],
            cwd=repo_root,
            timeout=10,
            check=False,
        )
        merged_branches = {
            line.strip().lstrip("* ") for line in merged_result.stdout.splitlines()
        }
        if branch in merged_branches:
            skipped.append(f"{wt_label}: branch merged, should be cleaned by remove")
            continue

        # Step 3b: check whether the branch has any new commits beyond HEAD
        new_commits_result = _run_git(
            ["log", f"HEAD..{branch}", "--oneline"],
            cwd=repo_root,
            timeout=10,
            check=False,
        )
        new_commits_output = new_commits_result.stdout.strip()
        if not new_commits_output:
            skipped.append(f"{wt_label}: no new commits (recently created)")
            continue

        # Step 3c: get last commit timestamp for the branch
        ts_result = _run_git(
            ["log", "-1", "--format=%ct", branch],
            cwd=repo_root,
            timeout=5,
            check=False,
        )
        ts_str = ts_result.stdout.strip()
        if not ts_str:
            # Cannot determine age; skip conservatively
            skipped.append(f"{wt_label}: cannot determine last commit time")
            continue

        try:
            last_commit_ts = int(ts_str)
        except ValueError:
            skipped.append(f"{wt_label}: cannot parse commit timestamp: {ts_str!r}")
            continue

        # Step 3d: check age
        age = now - last_commit_ts
        if age <= max_age_seconds:
            skipped.append(
                f"{wt_label}: last commit {age // 86400}d old (threshold {max_age_days}d)"
            )
            continue

        # Step 4: GC candidate — dry run or remove
        if dry_run:
            skipped.append(f"{wt_label}: dry_run")
        else:
            result = remove_worktree(
                repo_root,
                branch,
                force=True,
                delete_branch=True,
                timeout=timeout,
            )
            removed.append(result)

    return GCResult(removed=removed, skipped=skipped, pruned=pruned)


# ---------------------------------------------------------------------------
# Internal helpers — Sub-phase 7D
# ---------------------------------------------------------------------------


def _parse_conflict_files(stdout: str, stderr: str) -> list[str]:
    """Extract conflicting file paths from Git merge output.

    Parses ``CONFLICT (content): Merge conflict in <path>`` lines
    from both stdout and stderr.
    """
    pattern = re.compile(r"CONFLICT \([^)]+\):.*?(?:Merge conflict in |merge conflict in )(.+)")
    files: list[str] = []
    for line in (stdout + "\n" + stderr).splitlines():
        m = pattern.search(line)
        if m:
            files.append(m.group(1).strip())
    return files


# ---------------------------------------------------------------------------
# Public API — Sub-phase 7D
# ---------------------------------------------------------------------------


def merge_and_validate(
    repo_root: Path,
    work_dir_rel: str,
    branch: str,
    *,
    timeout: int = 60,
) -> IntegrationResult:
    """Merge an agent branch and validate the result semantically.

    v1 always auto-aborts/rollbacks on any conflict (PD5 reject-and-report).

    Flow (per plan section 8.1):
        1.  Snapshot base Book from ``work_dir_rel/edit_state/book.json``.
        1b. Record ``pre_merge_sha`` for rollback.
        2.  Verify branch exists via ``git rev-parse --verify``.
        2b. Check clean working tree via ``git status --porcelain``.
        3.  ``git merge --no-ff <branch> -m "Merge <branch>"``
        4.  If Git conflict: abort merge, return ``git_conflict``.
        5.  Parse merged ``book.json``.
        6.  ``diff_books(base, merged)`` -> BookPatch.
        7.  ``validate_book_patch`` + ``apply_book_patch`` round-trip.
        8.  Compare ``applied.model_dump(mode="json")`` vs ``merged.model_dump(mode="json")``.
        9.  Accept — return merge details.

    All rollback paths use ``git reset --hard <pre_merge_sha>`` (recorded
    before the merge) instead of ``HEAD~1``.

    On timeout during any git subprocess, attempts ``git merge --abort``
    as best-effort cleanup before raising GitError.

    Args:
        repo_root: Absolute path to the repository root.
        work_dir_rel: Relative path from repo_root to the work directory
            (e.g. ``work/book``).
        branch: Branch name to merge (must match ``agent/<kind>-<id>``).
        timeout: Maximum seconds for the ``git merge`` command.

    Returns:
        An ``IntegrationResult`` describing the merge outcome.

    Raises:
        GitError: For unexpected Git failures or timeout.
    """
    # Lazy imports to avoid circular dependencies and keep the module
    # importable without the full editor stack when only Git helpers are needed.
    from epubforge.editor.diff import DiffError, diff_books
    from epubforge.editor.patches import PatchError, apply_book_patch, validate_book_patch
    from epubforge.io import load_book

    # -- Step 1: Snapshot base Book -------------------------------------------
    base_book_path = repo_root / work_dir_rel / "edit_state" / "book.json"
    try:
        base = load_book(base_book_path)
    except Exception as exc:
        return IntegrationResult(
            outcome=MergeOutcome(
                status="parse_error",
                message=f"failed to load base book.json: {exc}",
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=None,
            base_sha256=None,
            merged_sha256=None,
            change_count=0,
            patch_json=None,
            conflict_files=[],
        )

    base_raw = base_book_path.read_bytes()
    base_sha256 = hashlib.sha256(base_raw).hexdigest()

    # -- Step 1b: Record pre-merge SHA ----------------------------------------
    pre_merge_sha = _get_head_sha(repo_root)

    # -- Step 2: Verify branch exists -----------------------------------------
    branch_check = _run_git(
        ["rev-parse", "--verify", branch],
        cwd=repo_root,
        timeout=5,
        check=False,
    )
    if branch_check.returncode != 0:
        return IntegrationResult(
            outcome=MergeOutcome(
                status="git_conflict",
                message=f"branch '{branch}' does not exist",
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=pre_merge_sha,
            base_sha256=base_sha256,
            merged_sha256=None,
            change_count=0,
            patch_json=None,
            conflict_files=[],
        )

    # -- Step 2b: Check clean working tree ------------------------------------
    status_result = _run_git(
        ["status", "--porcelain"],
        cwd=repo_root,
        timeout=10,
        check=False,
    )
    if status_result.stdout.strip():
        return IntegrationResult(
            outcome=MergeOutcome(
                status="git_conflict",
                message="working tree has uncommitted changes",
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=pre_merge_sha,
            base_sha256=base_sha256,
            merged_sha256=None,
            change_count=0,
            patch_json=None,
            conflict_files=[],
        )

    # -- Step 3: git merge --no-ff --------------------------------------------
    try:
        merge_result = _run_git(
            ["merge", "--no-ff", branch, "-m", f"Merge {branch}"],
            cwd=repo_root,
            timeout=timeout,
            check=False,
        )
    except GitError:
        # Timeout — attempt cleanup
        try:
            _run_git(["merge", "--abort"], cwd=repo_root, timeout=10, check=False)
        except Exception:
            pass  # best effort
        raise

    # -- Step 4: Check Git result ---------------------------------------------
    if merge_result.returncode != 0:
        conflict_files = _parse_conflict_files(merge_result.stdout, merge_result.stderr)
        # Abort the failed merge to restore clean state
        _run_git(["merge", "--abort"], cwd=repo_root, timeout=10, check=False)
        return IntegrationResult(
            outcome=MergeOutcome(
                status="git_conflict",
                message=(
                    f"merge conflict in {', '.join(conflict_files)}"
                    if conflict_files
                    else f"git merge failed (rc={merge_result.returncode})"
                ),
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=pre_merge_sha,
            base_sha256=base_sha256,
            merged_sha256=None,
            change_count=0,
            patch_json=None,
            conflict_files=conflict_files,
        )

    # -- Step 5: Parse merged Book --------------------------------------------
    merged_book_path = repo_root / work_dir_rel / "edit_state" / "book.json"
    try:
        merged = load_book(merged_book_path)
        merged_raw = merged_book_path.read_bytes()
        merged_sha256 = hashlib.sha256(merged_raw).hexdigest()
    except Exception as exc:
        _run_git(
            ["reset", "--hard", pre_merge_sha],
            cwd=repo_root,
            timeout=10,
        )
        return IntegrationResult(
            outcome=MergeOutcome(
                status="parse_error",
                message=f"merged book.json parse failed: {exc}",
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=pre_merge_sha,
            base_sha256=base_sha256,
            merged_sha256=None,
            change_count=0,
            patch_json=None,
            conflict_files=[],
        )

    # -- Step 6: diff_books(base, merged) -> BookPatch ------------------------
    try:
        patch = diff_books(base, merged)
    except DiffError as exc:
        _run_git(
            ["reset", "--hard", pre_merge_sha],
            cwd=repo_root,
            timeout=10,
        )
        return IntegrationResult(
            outcome=MergeOutcome(
                status="semantic_conflict",
                message=str(exc),
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=pre_merge_sha,
            base_sha256=base_sha256,
            merged_sha256=merged_sha256,
            change_count=0,
            patch_json=None,
            conflict_files=[],
        )

    # -- Step 7: validate + apply round-trip ----------------------------------
    try:
        validate_book_patch(base, patch)
        applied = apply_book_patch(base, patch)
    except PatchError as exc:
        _run_git(
            ["reset", "--hard", pre_merge_sha],
            cwd=repo_root,
            timeout=10,
        )
        return IntegrationResult(
            outcome=MergeOutcome(
                status="semantic_conflict",
                message=exc.reason,
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=pre_merge_sha,
            base_sha256=base_sha256,
            merged_sha256=merged_sha256,
            change_count=0,
            patch_json=None,
            conflict_files=[],
        )

    # -- Step 8: Round-trip assertion (defensive) -----------------------------
    applied_dump = applied.model_dump(mode="json")
    merged_dump = merged.model_dump(mode="json")
    if applied_dump != merged_dump:
        diff_keys = [
            k for k in set(applied_dump) | set(merged_dump)
            if applied_dump.get(k) != merged_dump.get(k)
        ]
        _run_git(
            ["reset", "--hard", pre_merge_sha],
            cwd=repo_root,
            timeout=10,
        )
        return IntegrationResult(
            outcome=MergeOutcome(
                status="semantic_conflict",
                message=(
                    "round-trip mismatch: applied patch does not reproduce "
                    f"merged Book (divergent keys: {diff_keys}). "
                    "This indicates a Phase 6 diff/apply regression."
                ),
            ),
            branch=branch,
            merge_commit=None,
            pre_merge_sha=pre_merge_sha,
            base_sha256=base_sha256,
            merged_sha256=merged_sha256,
            change_count=0,
            patch_json=None,
            conflict_files=[],
        )

    # -- Step 9: Accept -------------------------------------------------------
    merge_commit_sha = _get_head_sha(repo_root)

    return IntegrationResult(
        outcome=MergeOutcome(status="accepted", message="merge validated"),
        branch=branch,
        merge_commit=merge_commit_sha,
        pre_merge_sha=pre_merge_sha,
        base_sha256=base_sha256,
        merged_sha256=merged_sha256,
        change_count=len(patch.changes),
        patch_json=patch.model_dump(mode="json"),
        conflict_files=[],
    )


def abort_merge(repo_root: Path, *, timeout: int = 10) -> None:
    """Abort an in-progress merge.

    Raises GitError if no merge is in progress or if the abort fails.

    Args:
        repo_root: Absolute path to the repository root.
        timeout: Maximum seconds for the ``git merge --abort`` command.
    """
    _run_git(["merge", "--abort"], cwd=repo_root, timeout=timeout)


def resolve_book_at_ref(
    repo_root: Path,
    ref: str,
    work_dir_rel: str,
    *,
    timeout: int = 10,
) -> str:
    """Return the raw JSON string of book.json at the given Git ref.

    Uses ``git show <ref>:<work_dir_rel>/edit_state/book.json``.
    Path separators are normalized to forward slashes for Git compatibility.

    Raises GitError if the ref or path does not exist.
    """
    # Normalize path separators (Windows backslashes -> forward slashes)
    normalized_rel = work_dir_rel.replace("\\", "/")
    git_path = f"{normalized_rel}/edit_state/book.json"
    result = _run_git(
        ["show", f"{ref}:{git_path}"],
        cwd=repo_root,
        timeout=timeout,
    )
    return result.stdout


def resolve_book_path_at_ref(
    repo_root: Path,
    ref: str,
    work_dir_rel: str,
    *,
    timeout: int = 10,
) -> tuple[Book, bytes]:
    """Resolve the Book at a Git ref, returning (Book, raw_bytes).

    Convenience wrapper around resolve_book_at_ref that parses + validates.
    Raises GitError if ref/path missing, or pydantic ValidationError if invalid.
    """
    json_text = resolve_book_at_ref(repo_root, ref, work_dir_rel, timeout=timeout)
    raw = json_text.encode("utf-8")
    book = Book.model_validate_json(json_text)
    return book, raw


__all__ = [
    # Sub-phase 7A
    "GitError",
    "WorktreeInfo",
    "find_repo_root",
    # Sub-phase 7B
    "WorktreeCreateResult",
    "create_worktree",
    "list_worktrees",
    # Sub-phase 7C
    "WorktreeRemoveResult",
    "GCResult",
    "remove_worktree",
    "gc_worktrees",
    # Sub-phase 7D
    "MergeOutcome",
    "IntegrationResult",
    "merge_and_validate",
    "abort_merge",
    # Sub-phase 7E
    "resolve_book_at_ref",
    "resolve_book_path_at_ref",
    # Internal helpers exposed for testing and downstream sub-phases:
    "_DEFAULT_GIT_TIMEOUT",
    "_run_git",
    "_validate_branch_name",
    "_default_worktree_path",
    "_ensure_path_safe",
    "_get_head_sha",
    "_parse_worktree_porcelain",
    "_parse_conflict_files",
]
