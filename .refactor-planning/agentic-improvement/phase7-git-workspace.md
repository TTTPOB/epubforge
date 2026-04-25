# Phase 7 实施计划：Git-backed Workspace Workflow

> 状态：修订后的实施计划（pro review）
> 对应主设计：`agentic-improvement.md` §7（Validate patches semantically）、§8（Integration merge validation）、§10（Use Git for projection version control）、§14（Concurrency model: Git workspaces）、D1（Git workspace mode）、D4（No migration）、D6（No op_log_version）
> 前置条件：Phase 1-5 的 BookPatch / AgentOutput / PatchCommand / Projection 已可用；Phase 6 的 `diff_books` / `apply_book_patch` 提供语义桥接
> 下游依赖：Phase 8 VLM evidence 工具、Phase 9 Stage 3 简化、Phase 10 Doctor task 生成

---

## 0. Plan-review loop policy / no human blocking

本计划必须支持无人值守的后续实现流程。实现 worker / reviewer **不得阻塞等待人类在线答疑**，也不得依赖任何 ask-human 工具或等价机制。

规则：

1. **不 ask human**：遇到设计不确定性时，不发起 ask-human；把问题写入本文件的 [Open questions register](#15-open-questions-register)。
2. **默认假设可执行**：每个开放问题必须同时记录：影响、默认假设、推荐决策、触发复核的条件。实现者按默认假设继续推进，除非代码事实或测试证明该默认不可行。
3. **多轮 plan-review 后仍未解决的问题必须保留**：如果经过多轮 plan-review loop 仍存在开放问题，不删除、不隐藏；将其标记为 `unresolved-after-review`，并保留默认实现路径与复核条件。
4. **计划文件是异步决策载体**：所有需要用户之后查看的设计点、风险、折中、默认选择和后续复核点都写在本计划中。
5. **实现期间的新发现回写计划**：如果实现时发现本计划与实际代码不一致，应先修订本计划的相应条目，再继续实现；不要在聊天中等待人类裁决。

---

## 1. 目标与非目标

### 1.1 目标

Phase 7 为 agentic editing 提供基于 Git worktree 的并发隔离 workspace workflow：

1. **Git 操作封装层**：使用 subprocess + git CLI 封装 worktree 的创建、列举、删除、GC 操作，不引入外部 Git 库（PD1）。
2. **Workspace 生命周期管理**：supervisor/orchestrator 通过 CLI 创建和管理 worktree；agent 在已创建的 worktree 中工作，不自己管理 worktree（PD6）。
3. **Integration merge 与语义验证**：Git merge → parse Book → `diff_books(base, proposed)` → semantic validation → accept/reject（PD4、PD5）。
4. **diff-books CLI 的 Git ref 扩展**：新增 `--base-ref` / `--proposed-ref` 参数，通过 Git ref 解析为 `book.json` 路径后调用 Phase 6 `diff_books`。
5. **完整的端到端 agent 工作循环支持**：从 worktree 创建到 agent 编辑到 integration merge 到 cleanup 的全流程。
6. **孤立 worktree GC**：age-based 清理未 merge 的废弃 worktree。

### 1.2 非目标

Phase 7 不做：

- **不引入外部 Git 库**：不使用 GitPython、Dulwich 等（PD1）。
- **不实现 VLM evidence 工具**：属于 Phase 8。
- **不实现 Stage 3 简化**：属于 Phase 9。
- **不实现 Doctor task 生成**：属于 Phase 10。
- **不实现 display_handle**：UID 重设计属于未来工作。
- **不实现 `--stdout` 输出模式**：与 Phase 5 一致。
- **不实现自动语义合并**：v1 冲突模型为 reject-and-report（PD5），不做 auto-semantic-merge。
- **不实现远程 Git 操作**：Phase 7 只操作本地 worktree，不涉及 push/pull/remote。
- **不修改 Book IR schema**：不新增字段，不改变 `semantic.py`。

---

## 2. 当前代码事实与约束

### 2.1 EditorPaths 与路径解析

`src/epubforge/editor/state.py` 中的 `EditorPaths` 是 frozen dataclass，所有路径从 `work_dir` 派生：

```python
@dataclass(frozen=True)
class EditorPaths:
    work_dir: Path
    edit_state_dir: Path          # work_dir / "edit_state"
    book_path: Path               # edit_state / "book.json"
    meta_path: Path               # edit_state / "meta.json"
    memory_path: Path             # edit_state / "memory.json"
    agent_outputs_dir: Path       # edit_state / "agent_outputs"
    agent_outputs_archives_dir: Path
    # ...
```

`resolve_editor_paths(path)` 检测 `path` 是否以 `edit_state` 结尾，自动推导 `work_dir`。

**关键约束**：Git worktree 创建的工作目录具有与主仓库相同的目录结构。如果主仓库在 `repo/work/book/edit_state/` 下工作，则 worktree 创建在 `../repo-agent-scanner-1/work/book/edit_state/` 下时，`EditorPaths` 的 `work_dir` 不同（worktree 路径），但 `edit_state/` 的相对结构完全一致。因此 `resolve_editor_paths(worktree_work_dir)` 可以直接工作，无需修改 `EditorPaths`。

### 2.2 subprocess 使用模式

`src/epubforge/editor/scratch.py` 已建立 subprocess 使用先例：

```python
completed = subprocess.run(
    [sys.executable, str(script_path)],
    cwd=PROJECT_ROOT,
    capture_output=True,
    text=True,
    env=env,
    check=False,
)
```

Phase 7 的 Git 操作封装将遵循相同模式：`subprocess.run` + `capture_output=True` + `text=True` + `check=False` + 显式 `timeout`。

### 2.3 app.py CLI 注册

`src/epubforge/editor/app.py` 使用 Typer，子命令组通过 `editor_app.add_typer(sub_app, name="...")` 注册。已有子命令组：`agent-output`、`projection`。Phase 7 将新增 `workspace` 子命令组。

### 2.4 Phase 6 diff_books API

`src/epubforge/editor/diff.py` 提供：

```python
class DiffError(RuntimeError): ...

def diff_books(base: Book, proposed: Book) -> BookPatch: ...
```

`src/epubforge/editor/tool_surface.py` 中 `build_diff_books_result()` 的注释（行 416-419）：

> Phase 7 integration should resolve Git refs/worktrees outside this helper,
> pass the resulting `edit_state/book.json` paths here (or parse them and call
> `diff_books` directly), and keep all Git operations out of the Phase 6
> semantic diff bridge.

### 2.5 io.py Book 加载/保存

```python
def load_book(path: str | Path) -> Book
def save_book(book: Book, path: str | Path) -> Path
```

### 2.6 Git 仓库结构假设

Phase 7 假设 `work_dir` 所在的仓库（或其祖先目录）是一个 Git 仓库。`git worktree add` 在仓库根目录操作。worktree 路径是仓库根目录的同级目录或其子目录。

### 2.7 patches.py 公共 API

```python
def validate_book_patch(book: Book, patch: BookPatch) -> None: ...
def apply_book_patch(book: Book, patch: BookPatch) -> Book: ...
```

---

## 3. 架构位置

### 3.1 Phase 7 在整体 agentic workflow 中的位置

```text
                         +-----------------------+
                         |    Supervisor /        |
                         |    Orchestrator        |
                         +-----------+-----------+
                                     |
              +----------------------+----------------------+
              |                                             |
   workspace create                              workspace merge
              |                                             |
              v                                             v
   +---------------------+                  +----------------------------+
   | Agent Worktree      |                  | Integration Branch         |
   | (branch: agent/*)   |                  | (main / master)            |
   |                     |                  |                            |
   | 1. projection export|    git commit    | 5. git merge --no-ff       |
   | 2. agent-output     | --------------> | 6. parse book.json         |
   |    begin/add/submit  |                 | 7. diff_books(base, merged)|
   | 3. git add & commit |                 | 8. validate/audit          |
   | 4. (repeat)         |                 | 9. accept or reject        |
   +---------------------+                  +----------------------------+
              |                                             |
              |           workspace remove/gc               |
              +---------------------------------------------+
```

### 3.2 数据流详细

```text
supervisor CLI
    |
    +-> epubforge editor workspace create <work> --branch agent/scanner-1
    |       -> git worktree add ../<repo>-agent-scanner-1 -b agent/scanner-1
    |       -> 输出 worktree 路径和分支名
    |
    |   [agent 在 worktree 中工作]
    |       -> epubforge editor projection export <worktree-work>
    |       -> epubforge editor agent-output begin/add/submit <worktree-work>
    |       -> git add edit_state/ && git commit
    |
    +-> epubforge editor workspace merge <work> --branch agent/scanner-1
    |       -> git merge --no-ff agent/scanner-1
    |       -> 如果 Git 冲突: reject, abort merge, 报告给 supervisor
    |       -> 如果 Git 成功: parse merged book.json
    |       -> diff_books(base, merged) -> BookPatch
    |       -> validate_book_patch + apply_book_patch (round-trip check)
    |       -> 如果语义验证失败: reject, git merge --abort, 报告
    |       -> 如果全部通过: accept, 输出 merge result
    |
    +-> epubforge editor workspace remove <work> --branch agent/scanner-1
    |       -> git worktree remove <path>
    |       -> git branch -d agent/scanner-1 (如果已 merged)
    |
    +-> epubforge editor workspace gc <work> --max-age-days 7
            -> 扫描所有 agent/* worktree
            -> 删除超龄且未 merge 的 worktree
```

---

## 4. API 设计

### 4.1 核心公共 API

以下函数位于新模块 `src/epubforge/editor/workspace.py`。

#### 4.1.1 返回类型定义

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WorktreeInfo:
    """Describes a single Git worktree."""
    path: Path                          # worktree 绝对路径
    branch: str                         # branch name (e.g. "agent/scanner-1")
    commit: str                         # HEAD commit SHA (abbreviated)
    is_bare: bool                       # 是否为 bare worktree
    is_main: bool                       # 是否为主 worktree
    prunable: bool                      # git worktree prune 是否可清理


@dataclass(frozen=True)
class WorktreeCreateResult:
    """Result of creating a new worktree.

    Does not include ``work_dir``: the CLI layer computes it as
    ``worktree_path / work_dir_rel`` since it already knows the
    relative path from the positional ``work`` argument.
    """
    worktree_path: Path                 # 新创建的 worktree 绝对路径
    branch: str                         # 创建的分支名
    commit: str                         # worktree HEAD commit SHA


@dataclass(frozen=True)
class MergeOutcome:
    """Outcome classification for a merge attempt."""
    status: Literal["accepted", "git_conflict", "semantic_conflict", "parse_error"]
    message: str


@dataclass(frozen=True)
class IntegrationResult:
    """Full result of an integration merge attempt."""
    outcome: MergeOutcome
    branch: str                         # 被 merge 的分支名
    merge_commit: str | None            # merge commit SHA (如果成功)
    pre_merge_sha: str | None           # integration branch HEAD before merge (for audit/debug)
    base_sha256: str | None             # base book.json SHA256
    merged_sha256: str | None           # merged book.json SHA256
    change_count: int                   # BookPatch 中的 change 数量
    patch_json: dict | None             # BookPatch JSON (如果成功)
    conflict_files: list[str]           # Git 冲突文件列表 (如果有)


@dataclass(frozen=True)
class WorktreeRemoveResult:
    """Result of removing a worktree."""
    worktree_path: Path
    branch: str
    branch_deleted: bool                # 分支是否被删除
    force_used: bool                    # 是否使用了 force


@dataclass(frozen=True)
class GCResult:
    """Result of garbage-collecting stale worktrees."""
    removed: list[WorktreeRemoveResult]
    skipped: list[str]                  # 跳过的 worktree 路径和原因
    pruned: int                         # git worktree prune 清理的条目数
```

#### 4.1.2 Git 操作封装函数

```python
class GitError(RuntimeError):
    """Raised when a Git subprocess command fails."""
    def __init__(self, message: str, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def find_repo_root(path: Path) -> Path:
    """Find the Git repository root containing *path*.

    Raises GitError if *path* is not inside a Git repository.
    """
    ...


def create_worktree(
    repo_root: Path,
    branch: str,
    *,
    worktree_path: Path | None = None,
    base_ref: str = "HEAD",
    timeout: int = 30,
) -> WorktreeCreateResult:
    """Create a new Git worktree with a new branch.

    If *worktree_path* is None, uses default naming:
    ``<repo_root>/../<repo_name>-<branch_slug>/``

    Raises GitError on failure (e.g. branch already exists).
    """
    ...


def list_worktrees(
    repo_root: Path,
    *,
    agent_only: bool = False,
    timeout: int = 10,
) -> list[WorktreeInfo]:
    """List all worktrees, optionally filtering to agent/* branches.

    Uses ``git worktree list --porcelain`` for stable parsing.
    """
    ...


def remove_worktree(
    repo_root: Path,
    branch: str,
    *,
    force: bool = False,
    delete_branch: bool = True,
    timeout: int = 30,
) -> WorktreeRemoveResult:
    """Remove a worktree and optionally its branch.

    If *delete_branch* and the branch has been merged, deletes with ``-d``.
    If *force* and the branch has not been merged, deletes with ``-D``.

    Raises GitError if removal fails.
    """
    ...


def gc_worktrees(
    repo_root: Path,
    *,
    max_age_days: int = 7,
    dry_run: bool = False,
    timeout: int = 60,
) -> GCResult:
    """Garbage-collect orphaned agent worktrees older than *max_age_days*.

    An agent worktree is considered orphaned if:
    - Its branch matches ``agent/*``
    - The branch has at least one commit beyond the fork point
      (``git log <base>..<agent> --oneline`` has output; branches
      with no new commits are skipped to avoid false positives on
      freshly created worktrees)
    - Its last commit is older than *max_age_days*
    - The branch has not been merged into the current HEAD

    Uses ``git worktree prune`` first, then scans remaining agent worktrees.
    """
    ...
```

#### 4.1.3 Integration merge 函数

```python
def merge_and_validate(
    repo_root: Path,
    work_dir_rel: str,
    branch: str,
    *,
    timeout: int = 60,
) -> IntegrationResult:
    """Merge an agent branch and validate the result semantically.

    v1 always auto-aborts/rollbacks on any conflict (PD5 reject-and-report).

    Flow:
    1. Capture pre-merge base Book snapshot from work_dir/edit_state/book.json.
    1b. Record ``pre_merge_sha = _get_head_sha(repo_root)`` for rollback.
    2. Verify branch exists: ``git rev-parse --verify <branch>``.
    3. ``git merge --no-ff <branch>``
    4. If Git conflict: abort merge, return git_conflict outcome.
    5. If Git success: parse merged book.json.
    6. If parse fails: ``git reset --hard <pre_merge_sha>``, return parse_error.
    7. ``diff_books(base, merged)`` -> BookPatch.
    8. ``validate_book_patch(base, patch)`` + ``apply_book_patch(base, patch)``.
    9. Round-trip check (defensive assertion, catches Phase 6 regressions;
       on failure, output detailed diagnostics including base/merged/applied
       model dumps for debugging).
    10. If validation fails: ``git reset --hard <pre_merge_sha>``, return
        semantic_conflict outcome.
    11. If all pass: return accepted outcome with merge details.

    All rollback paths use ``git reset --hard <pre_merge_sha>`` (recorded
    before the merge) instead of ``HEAD~1``, which is fragile when the merge
    commit is not exactly one step back (e.g. fast-forward edge cases or
    interrupted states).

    On timeout during any git subprocess, attempts ``git merge --abort``
    as best-effort cleanup before raising GitError.

    *work_dir_rel* is the relative path from repo_root to the work directory
    (e.g. ``work/book``).

    Raises GitError for unexpected Git failures.
    """
    ...


def abort_merge(
    repo_root: Path,
    *,
    timeout: int = 10,
) -> None:
    """Abort an in-progress merge.

    Raises GitError if no merge is in progress or abort fails.
    """
    ...
```

#### 4.1.4 Git ref 解析辅助

```python
def resolve_book_at_ref(
    repo_root: Path,
    ref: str,
    work_dir_rel: str,
) -> Book:
    """Load and parse a Book from a specific Git ref.

    Uses ``git show <ref>:<work_dir_rel>/edit_state/book.json`` to extract
    the file content without checking out the ref.

    Internally normalizes *work_dir_rel* to use forward slashes (``/``)
    so that ``git show`` paths work correctly on Windows.

    Raises GitError if ref doesn't exist or file not found.
    Raises ValueError if JSON is invalid or Book schema fails.
    """
    ...


def resolve_book_path_at_ref(
    repo_root: Path,
    ref: str,
    work_dir_rel: str,
    *,
    timeout: int = 10,
) -> str:
    """Return the raw JSON content of book.json at a Git ref.

    Uses ``git show <ref>:<path>``.

    Internally normalizes *work_dir_rel* to use forward slashes (``/``)
    so that ``git show`` paths work correctly on Windows.
    """
    ...
```

---

## 5. 文件布局

### 5.1 新增文件

| 文件 | 用途 |
|---|---|
| `src/epubforge/editor/workspace.py` | Git 操作封装、worktree 生命周期管理、integration merge、ref 解析 |
| `src/epubforge/editor/workspace_cli.py` | `workspace` 子命令组 Typer app 定义 |
| `tests/editor/test_workspace.py` | workspace 单元测试：Git 操作、merge、GC |
| `tests/editor/test_workspace_cli.py` | workspace CLI 集成测试 |

### 5.2 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/epubforge/editor/app.py` | 注册 `workspace` 子命令组：`editor_app.add_typer(workspace_app, name="workspace")` |
| `src/epubforge/editor/tool_surface.py` | 新增 `run_workspace_create()`、`run_workspace_list()`、`run_workspace_merge()`、`run_workspace_remove()`、`run_workspace_gc()` 业务函数；扩展 `run_diff_books()` 支持 `--base-ref` / `--proposed-ref` |
| `src/epubforge/editor/__init__.py` | 导出 workspace 相关类型 |

> **签名约定**：`run_workspace_*` 函数遵循与 `tool_surface.py` 中其他 `run_*` 函数相同的签名模式，包括 `cfg: Config` 作为第一个参数。这确保了 workspace 操作与现有 tool surface 的调用约定一致。

---

## 6. Git 操作封装详细设计

### 6.1 subprocess 封装基础设施

新增内部 helper 函数，所有 Git 命令通过此函数执行：

```python
import subprocess
from pathlib import Path


_DEFAULT_GIT_TIMEOUT = 30  # seconds


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
        args: Git command arguments (without leading 'git').
        cwd: Working directory for the command.
        timeout: Maximum seconds before TimeoutExpired.
        check: If True and returncode != 0, raise GitError.
        input_text: Optional stdin text.

    Raises:
        GitError: When check=True and command fails, or when timeout expires.
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
        # Wrap timeout as GitError with clear message for callers.
        # The caller (e.g. merge_and_validate) is responsible for
        # attempting cleanup (git merge --abort) on timeout.
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
```

### 6.2 路径安全性

所有接受 branch name 的函数必须进行安全检查：

```python
import re

_BRANCH_PATTERN = re.compile(r"^agent/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")

def _validate_branch_name(branch: str) -> str:
    """Validate and return a safe branch name.

    Phase 7 only allows branches matching 'agent/<kind>-<id>' pattern (PD3).
    Rejects path traversal, shell metacharacters, and non-agent branches.

    Raises ValueError on invalid branch name.
    """
    if not branch:
        raise ValueError("branch name must not be empty")
    if not _BRANCH_PATTERN.fullmatch(branch):
        raise ValueError(
            f"branch name {branch!r} does not match required pattern "
            "'agent/<kind>-<id>[/<sub>]'; only alphanumeric, dot, dash, underscore allowed"
        )
    return branch
```

worktree 路径规范化：

```python
def _default_worktree_path(repo_root: Path, branch: str) -> Path:
    """Compute the default worktree directory path.

    Convention: ``<repo_root>/../<repo_name>-<branch_slug>/``
    where branch_slug replaces '/' with '-'.

    Note: slug collision is theoretically possible (e.g. ``agent/a-b``
    and ``agent/a/b`` both slug to ``agent-a-b``). In practice this is
    unlikely with the ``agent/<kind>-<id>`` naming convention. If it
    does happen, ``git worktree add`` will fail because the target
    directory already exists, producing a clear error for the caller.
    """
    repo_name = repo_root.name
    branch_slug = branch.replace("/", "-")
    return repo_root.parent / f"{repo_name}-{branch_slug}"
```

所有路径操作后必须验证 resolved path 不逃逸到非预期位置：

```python
def _ensure_path_safe(path: Path, parent: Path) -> Path:
    """Verify resolved path is under parent. Raise ValueError otherwise."""
    resolved = path.resolve()
    if not resolved.is_relative_to(parent.resolve()):
        raise ValueError(f"path {path} escapes expected parent {parent}")
    return resolved
```

### 6.3 各 Git 操作的 subprocess 调用

#### find_repo_root

```python
def find_repo_root(path: Path) -> Path:
    result = _run_git(
        ["rev-parse", "--show-toplevel"],
        cwd=path if path.is_dir() else path.parent,
        timeout=5,
    )
    return Path(result.stdout.strip())
```

#### create_worktree

```text
git worktree add <worktree_path> -b <branch> <base_ref>
```

- 如果 branch 已存在，`git worktree add` 会失败，GitError 传播给调用者。
- `base_ref` 默认 `HEAD`，也可以是 `main`、`master` 或任意 commit SHA。
- 创建后验证 worktree 路径存在且包含 `.git` 文件。

#### list_worktrees

```text
git worktree list --porcelain
```

porcelain 输出格式稳定，每个 worktree 由空行分隔：

```
worktree /path/to/repo
HEAD abc123def456
branch refs/heads/main

worktree /path/to/worktree-1
HEAD def456abc123
branch refs/heads/agent/scanner-1
```

解析为 `WorktreeInfo` 列表。`agent_only=True` 时过滤 `branch` 以 `refs/heads/agent/` 开头的条目。

#### remove_worktree

```text
git worktree remove <path> [--force]
git branch -d <branch>       # if delete_branch and merged
git branch -D <branch>       # if force and not merged
```

- 先通过 `list_worktrees` 找到 branch 对应的 worktree path。
- 如果找不到对应 worktree（可能已被手动删除），先 `git worktree prune` 再尝试 `git branch -d/-D`。

#### gc_worktrees

```text
git worktree prune                           # 清理已删除目录的 worktree 记录
git worktree list --porcelain                # 获取剩余 worktree
git log <base>..<agent> --oneline            # 检查是否有新 commit（空 = 跳过）
git log -1 --format=%ct <branch>             # 获取 branch 最后一次 commit 的 Unix 时间戳
git branch --merged HEAD                     # 检查 branch 是否已合并
```

- 先 prune，然后对每个 `agent/*` worktree：
  1. 检查是否有新 commit（`git log <base>..<agent> --oneline`）；无则跳过。
  2. 检查最后 commit 时间。
  3. 超过 `max_age_days` 且未 merged 的 worktree 执行 `remove_worktree(force=True)`。

### 6.4 超时策略

| 操作 | 默认超时 | 理由 |
|---|---|---|
| `rev-parse` | 5s | 纯本地操作 |
| `worktree add` | 30s | 涉及文件系统复制 |
| `worktree list` | 10s | 纯本地操作 |
| `worktree remove` | 30s | 涉及文件系统删除 |
| `merge --no-ff` | 60s | 大仓库合并可能较慢 |
| `merge --abort` | 10s | 纯本地操作 |
| `show <ref>:<path>` | 10s | 读取 Git object |
| `log -1 --format=%ct` | 5s | 单条 log |
| `branch --merged` | 10s | 分支检查 |
| `worktree prune` | 10s | 纯本地操作 |

### 6.5 stderr 处理

- `check=True`（默认）时，非零 returncode 抛 `GitError`，stderr 截取前 500 字符作为错误消息。
- `check=False` 时（如 `merge`），调用者根据 returncode 和 stderr 分类处理。
- 所有 stderr 日志记录完整输出（debug 级别），但错误消息只包含截取部分，避免泄露路径信息到 JSON 输出。

---

## 7. Workspace 生命周期详细设计

### 7.1 创建 worktree

#### 分支命名规范（PD3）

```
agent/<agent-kind>-<agent-id>
agent/<agent-kind>-<agent-id>/<sub-task>
```

示例：
- `agent/scanner-1`
- `agent/fixer-ch1-a3x`
- `agent/scanner-1/ch-1`

#### worktree 路径规范

```
<repo_root>/../<repo_name>-<branch_slug>/
```

示例：
- 分支 `agent/scanner-1` -> 路径 `../epubforge-agent-scanner-1/`
- 分支 `agent/scanner-1/ch-1` -> 路径 `../epubforge-agent-scanner-1-ch-1/`

#### 创建流程

1. `_validate_branch_name(branch)` — 安全检查。
2. `find_repo_root(work_dir)` — 定位仓库根目录。
3. 计算 `worktree_path`（如果未显式提供）。
4. 检查 `worktree_path` 是否已存在 — 如果存在则报错（`git worktree add` 也会在目标路径已存在时报错，但提前检查可提供更友好的错误消息）。
5. `_run_git(["worktree", "add", str(worktree_path), "-b", branch, base_ref], cwd=repo_root)`。
6. 验证 `worktree_path` 存在。
7. 获取 HEAD commit SHA：`_run_git(["rev-parse", "--short", "HEAD"], cwd=worktree_path)`。
8. 返回 `WorktreeCreateResult`（不含 `work_dir`；CLI 层自行计算 `worktree_path / work_dir_rel`）。

#### 初始状态验证

创建后 worktree 包含主仓库当前 HEAD 的完整工作树，包括 `edit_state/`。agent 可以直接使用 `projection export` 读取当前状态。

### 7.2 列出 worktree

```python
def list_worktrees(repo_root: Path, *, agent_only: bool = False) -> list[WorktreeInfo]:
```

- 解析 `git worktree list --porcelain` 输出。
- `agent_only=True` 时过滤 branch 匹配 `agent/*`。
- `is_main=True` 标记主 worktree（第一个条目或 `branch refs/heads/main` 或 `branch refs/heads/master`）。
- `prunable` 通过检查 worktree 路径是否存在来判断。

#### porcelain 格式解析

```python
def _parse_worktree_porcelain(output: str) -> list[WorktreeInfo]:
    """Parse ``git worktree list --porcelain`` output.

    Each worktree block:
        worktree <path>
        HEAD <sha>
        branch refs/heads/<name>
        [bare]
        [prunable gitdir file points to non-existent location ...]

    Blocks are separated by blank lines.
    """
    ...
```

### 7.3 删除/清理 worktree

#### 正常删除（branch 已 merged）

1. `list_worktrees` 找到 branch 对应的 worktree path。
2. `_run_git(["worktree", "remove", str(worktree_path)])`。
3. 如果 `delete_branch`：`_run_git(["branch", "-d", branch])`。
4. 返回 `WorktreeRemoveResult(branch_deleted=True, force_used=False)`。

#### 强制删除（branch 未 merged，或 worktree 路径已损坏）

1. `_run_git(["worktree", "remove", str(worktree_path), "--force"])`。
2. 如果 `delete_branch` 且 `force=True`：`_run_git(["branch", "-D", branch])`。
3. 返回 `WorktreeRemoveResult(branch_deleted=True, force_used=True)`。

#### 边界情况

- worktree 路径不存在但 Git 仍有记录 → 先 `git worktree prune`，再删除 branch。
- branch 不存在但 worktree 路径存在 → 先 `git worktree remove`，branch 删除跳过。
- 主 worktree 不可删除 → 检测 `is_main` 并拒绝。

### 7.4 孤立 worktree GC

```python
def gc_worktrees(
    repo_root: Path,
    *,
    max_age_days: int = 7,
    dry_run: bool = False,
) -> GCResult:
```

流程：

1. `git worktree prune` — 清理已经不存在的 worktree 目录记录。
2. `list_worktrees(agent_only=True)` — 获取所有 agent worktree。
3. 对每个 worktree：
   a. 检查是否已 merged：`git branch --merged HEAD` 是否包含此分支。
   b. 如果已 merged：跳过（应该由正常 merge workflow 清理）。
   c. 检查分支是否有超过 fork point 的新 commit：
      `git log <base_branch>..<agent_branch> --oneline`
      如果无输出，说明 agent 尚未提交任何新 commit；跳过此分支，
      避免将刚创建但尚未开始工作的 worktree 误判为超龄。
   d. 获取最后 commit 时间戳：`git log -1 --format=%ct <branch>`。
   e. 计算 age = `now - commit_timestamp`。
   f. 如果 `age > max_age_days * 86400` 且 **未 merged**：加入删除候选。
4. 对每个候选：
   a. 如果 `dry_run`：加入 skipped 列表，说明原因。
   b. 否则：`remove_worktree(force=True)` 并加入 removed 列表。
5. 返回 `GCResult`。

**注意**：步骤 3c 防止 GC 误判新建分支为超龄。刚创建的 worktree
（agent 尚未 commit）的 `git log -1` 返回的是 fork-point 的 commit 时间，
可能很久之前。通过检查 `git log <base>..<agent> --oneline` 是否有输出，
可以区分"有新 commit 但很旧"和"从未 commit"的分支。
备选方案：使用 worktree 目录的 mtime 作为活跃时间下界。

---

## 8. Integration merge 详细设计

### 8.1 合并流程

#### 术语说明

本节中的 **"base"** 指 integration branch 在 merge 前的 HEAD 状态（即主设计 §8 中的 "current"）。它是 `git merge` 执行前 integration branch 最新的 `book.json`。这与 agent fork point（agent 分支从 integration branch 分叉出去的那个 commit）不同。

为什么只需要 current（base）和 merged 两方？因为 Git 的三方合并（three-way merge）已经处理了 base/ours/theirs 的文本合并。Phase 7 的语义验证只需要比较 merge 前（current/base）和 merge 后（merged）的 Book 语义差异，确认合并结果在 Book IR 层面合法即可。

变量名保持 `base`（与 Phase 6 `diff_books(base, proposed)` 参数名一致），但在代码注释中必须明确：`base` = integration branch pre-merge HEAD = 主设计 "current"。

#### 完整流程

`merge_and_validate` 的完整流程：

```text
Step 1: 快照 base Book
    base_book_path = repo_root / work_dir_rel / "edit_state" / "book.json"
    base = load_book(base_book_path)
    base_raw = base_book_path.read_bytes()
    base_sha256 = sha256(base_raw)

Step 1b: 记录 pre-merge SHA 用于回滚
    pre_merge_sha = _get_head_sha(repo_root)
    # All rollback paths below use this SHA instead of HEAD~1.
    # This is more robust: HEAD~1 assumes the merge commit is exactly
    # one step back, which may not hold in edge cases (e.g. interrupted
    # states, fast-forward fallback, or if the commit graph is unusual).

Step 2: 验证分支存在性
    result = _run_git(["rev-parse", "--verify", branch],
                      cwd=repo_root, check=False)
    如果 returncode != 0:
        return IntegrationResult(
            outcome=MergeOutcome(
                status="git_conflict",
                message=f"branch '{branch}' does not exist"
            ),
            conflict_files=[],
            ...
        )

Step 3: git merge --no-ff <branch>
    result = _run_git(["merge", "--no-ff", branch, "-m", f"Merge {branch}"],
                      cwd=repo_root, check=False)

Step 4: 检查 Git 结果
    如果 returncode != 0:
        conflict_files = _parse_conflict_files(result.stdout, result.stderr)
        _run_git(["merge", "--abort"], cwd=repo_root)
        return IntegrationResult(
            outcome=MergeOutcome(status="git_conflict", message=...),
            conflict_files=conflict_files,
            ...
        )

Step 5: parse merged Book
    merged_book_path = repo_root / work_dir_rel / "edit_state" / "book.json"
    try:
        merged = load_book(merged_book_path)
        merged_raw = merged_book_path.read_bytes()
        merged_sha256 = sha256(merged_raw)
    except Exception as exc:
        _run_git(["reset", "--hard", pre_merge_sha], cwd=repo_root)
        return IntegrationResult(
            outcome=MergeOutcome(status="parse_error", message=str(exc)),
            ...
        )

Step 6: diff_books(base, merged) -> BookPatch
    try:
        patch = diff_books(base, merged)
    except DiffError as exc:
        _run_git(["reset", "--hard", pre_merge_sha], cwd=repo_root)
        return IntegrationResult(
            outcome=MergeOutcome(status="semantic_conflict", message=str(exc)),
            ...
        )

Step 7: validate + apply round-trip
    try:
        validate_book_patch(base, patch)
        applied = apply_book_patch(base, patch)
    except PatchError as exc:
        _run_git(["reset", "--hard", pre_merge_sha], cwd=repo_root)
        return IntegrationResult(
            outcome=MergeOutcome(status="semantic_conflict", message=exc.reason),
            ...
        )

Step 8: round-trip 验证（防御性断言）
    # This is a defensive assertion that catches Phase 6 diff/apply
    # regressions. If diff_books and apply_book_patch are correct,
    # this should never fail. When it does, output detailed diagnostics
    # for debugging the Phase 6 regression.
    如果 applied.model_dump(mode="json") != merged.model_dump(mode="json"):
        # Build diagnostic: identify which top-level keys differ
        applied_dump = applied.model_dump(mode="json")
        merged_dump = merged.model_dump(mode="json")
        diff_keys = [k for k in set(applied_dump) | set(merged_dump)
                     if applied_dump.get(k) != merged_dump.get(k)]
        _run_git(["reset", "--hard", pre_merge_sha], cwd=repo_root)
        return IntegrationResult(
            outcome=MergeOutcome(
                status="semantic_conflict",
                message=(
                    "round-trip mismatch: applied patch does not reproduce "
                    f"merged Book (divergent keys: {diff_keys}). "
                    "This indicates a Phase 6 diff/apply regression."
                ),
            ),
            ...
        )

Step 9: accept
    return IntegrationResult(
        outcome=MergeOutcome(status="accepted", message="merge validated"),
        merge_commit=_get_head_sha(repo_root),
        pre_merge_sha=pre_merge_sha,
        base_sha256=base_sha256,
        merged_sha256=merged_sha256,
        change_count=len(patch.changes),
        patch_json=patch.model_dump(mode="json"),
        ...
    )
```

### 8.2 冲突分类和处理策略（PD5）

| 场景 | Git 结果 | Phase 6 结果 | Phase 7 行为 |
|---|---|---|---|
| 不同 blocks 修改 | Git merge 成功 | diff/apply/audit 成功 | accept |
| 同字段文本冲突 | Git conflict (rc!=0) | 不调用 diff | reject: `git_conflict`, abort merge |
| Git merge 成功但 book.json 不是合法 JSON | Git 成功 | 不调用 diff | reject: `parse_error`, reset to `<pre_merge_sha>` |
| Git merge 成功但 Book schema 无效 | Git 成功 | 不调用 diff | reject: `parse_error`, reset to `<pre_merge_sha>` |
| Git merge 成功但 `diff_books` 抛 `DiffError` | Git 成功 | DiffError | reject: `semantic_conflict`, reset to `<pre_merge_sha>` |
| Git merge 成功但 `apply_book_patch` 失败 | Git 成功 | PatchError | reject: `semantic_conflict`, reset to `<pre_merge_sha>` |
| Git merge 成功但 round-trip 不匹配 | Git 成功 | mismatch | reject: `semantic_conflict`, reset to `<pre_merge_sha>` |
| Git merge 改了 Book metadata | Git 成功 | DiffError unsupported | reject: `semantic_conflict`, reset to `<pre_merge_sha>` |

### 8.3 回滚机制

- **Git 冲突**（merge 未完成）：`git merge --abort` 清理工作树和 index。
- **Git 成功但语义失败**（merge commit 已创建）：`git reset --hard <pre_merge_sha>` 回退到 merge 前记录的 commit。`pre_merge_sha` 在 Step 1b 中通过 `_get_head_sha(repo_root)` 获取并保存。使用显式 SHA 而非 `HEAD~1`，因为 `HEAD~1` 假设 merge commit 恰好是最新一步，在边缘情况下（如中断状态、异常 commit graph）不够健壮。
- **超时回滚**：任何 git 子进程超时后，先尝试 `git merge --abort`（best effort，忽略其错误），然后抛出 `GitError`。这确保即使 merge 过程中超时，工作树也不会停留在不一致状态。如果 `.git/index.lock` 残留（极端情况），`GitError` 消息中应包含 "stale index.lock may need manual removal" 的提示。
- **原子性保证**：所有 reject 路径都执行回滚操作后才返回结果。调用者无需手动清理。

### 8.4 成功后 worktree cleanup

`merge_and_validate` 本身不做 worktree cleanup。成功后由 supervisor 调用 `workspace remove` 清理。这保持了关注点分离：

- `merge_and_validate`：只负责 merge + validate。
- `workspace remove`：只负责 worktree + branch cleanup。

supervisor 的典型流程：

```python
result = merge_and_validate(repo_root, work_dir_rel, branch)
if result.outcome.status == "accepted":
    remove_worktree(repo_root, branch)
```

---

## 9. diff-books CLI 扩展

### 9.1 新增参数

在现有 `epubforge editor diff-books` 命令上新增 `--base-ref` 和 `--proposed-ref` 参数：

```
epubforge editor diff-books <work> \
  --base-ref <git-ref> \
  --proposed-ref <git-ref>
```

### 9.2 参数关系

| 参数组合 | 行为 |
|---|---|
| `--base-file` + `--proposed-file` | 现有行为，读取文件路径（Phase 6） |
| `--base-ref` + `--proposed-ref` | Phase 7 新增，通过 Git ref 解析 book.json |
| `--proposed-file`（无 base） | 现有行为，base 默认为 `<work>/edit_state/book.json` |
| `--proposed-ref`（无 base） | Phase 7 新增，base 默认为 `<work>/edit_state/book.json`（工作树当前状态） |
| `--base-ref` + `--proposed-file` | 允许混合，base 从 Git ref 读取，proposed 从文件读取 |
| `--base-file` + `--proposed-ref` | 允许混合 |
| 同时指定 `--base-file` 和 `--base-ref` | 错误，互斥 |
| 同时指定 `--proposed-file` 和 `--proposed-ref` | 错误，互斥 |

### 9.3 Git ref 解析为 Book 的逻辑

```python
def _resolve_book_from_ref_or_file(
    repo_root: Path,
    work_dir_rel: str,
    *,
    ref: str | None,
    file_path: Path | None,
    label: str,
) -> tuple[Book, bytes]:
    """Resolve a Book from either a Git ref or a file path.

    Exactly one of ref/file_path must be provided.
    """
    if ref is not None:
        json_content = resolve_book_path_at_ref(repo_root, ref, work_dir_rel)
        raw = json_content.encode("utf-8")
        book = Book.model_validate_json(json_content)
        return book, raw
    elif file_path is not None:
        return _read_book_snapshot(file_path, label=label)
    else:
        raise ValueError(f"either --{label}-ref or --{label}-file must be provided")
```

`resolve_book_path_at_ref` 内部使用：

```text
git show <ref>:<work_dir_rel>/edit_state/book.json
```

注意：`work_dir_rel` 是从 repo_root 到 work_dir 的相对路径。需要正确构造 Git 路径：

```python
def resolve_book_path_at_ref(
    repo_root: Path,
    ref: str,
    work_dir_rel: str,
    *,
    timeout: int = 10,
) -> str:
    # Normalize path separators: git show requires forward slashes
    # even on Windows where Path uses backslashes.
    normalized_rel = work_dir_rel.replace("\\", "/")
    git_path = f"{normalized_rel}/edit_state/book.json"
    result = _run_git(
        ["show", f"{ref}:{git_path}"],
        cwd=repo_root,
        timeout=timeout,
    )
    return result.stdout
```

### 9.4 CLI 输出格式

输出格式与现有 `diff-books` 完全一致，新增 `base_ref` 和 `proposed_ref` 字段：

```json
{
  "diff_applies": true,
  "round_trip_verified": true,
  "change_count": 3,
  "base_sha256": "...",
  "proposed_sha256": "...",
  "base_ref": "main",
  "proposed_ref": "agent/scanner-1",
  "patch": { ... },
  "unsupported_diffs": [],
  "review_groups": []
}
```

---

## 10. CLI 命令设计

### 10.1 命令树

```
epubforge editor workspace create <work> --branch <name> [--base-ref <ref>]
epubforge editor workspace list <work> [--agent-only]
epubforge editor workspace merge <work> --branch <name> [--timeout <seconds>]
epubforge editor workspace remove <work> --branch <name> [--force]
epubforge editor workspace gc <work> [--max-age-days <days>] [--dry-run]
```

### 10.2 workspace create

**用途**：为 agent 创建一个新的 Git worktree 和分支。

**参数**：

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | 主仓库中的 work directory |
| `--branch` | str | 是 | - | 分支名，必须匹配 `agent/<kind>-<id>` 模式 |
| `--base-ref` | str | 否 | `HEAD` | 基于哪个 ref 创建 worktree |

**行为**：

1. 验证 `work` 是有效 work directory 且已初始化（`edit_state/book.json` 存在）。
2. 验证 `--branch` 匹配命名规范。
3. 调用 `find_repo_root` 定位仓库根。
4. 计算 `work_dir_rel` = work 相对于 repo_root 的相对路径。
5. 调用 `create_worktree`。
6. 输出 JSON 结果。

**stdout JSON 输出**（成功）：

```json
{
  "created": true,
  "worktree_path": "/abs/path/to/epubforge-agent-scanner-1",
  "branch": "agent/scanner-1",
  "work_dir": "/abs/path/to/epubforge-agent-scanner-1/work/book",
  "commit": "abc1234",
  "base_ref": "HEAD"
}
```

> Note: `work_dir` in the CLI JSON output is computed by the CLI layer
> (`worktree_path / work_dir_rel`), not returned by `create_worktree()`.
> The core function `WorktreeCreateResult` does not include `work_dir`.

**错误处理**：

| 场景 | exit code | 错误 JSON |
|---|---|---|
| work dir 不存在 | 1 | `{"error": "work dir does not exist: ..."}` |
| edit_state 未初始化 | 1 | `{"error": "editor state is not initialized: ..."}` |
| 分支名无效 | 2 | `{"error": "branch name ... does not match required pattern ..."}` |
| 分支已存在 | 1 | `{"error": "git worktree add failed: ... branch ... already exists"}` |
| 不在 Git 仓库中 | 1 | `{"error": "not a git repository: ..."}` |

### 10.3 workspace list

**用途**：列出所有（或仅 agent）worktree。

**参数**：

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | 主仓库中的 work directory |
| `--agent-only` | bool (flag) | 否 | `False` | 只列出 agent/* 分支的 worktree |

**stdout JSON 输出**：

```json
{
  "worktrees": [
    {
      "path": "/abs/path/to/epubforge",
      "branch": "main",
      "commit": "abc1234",
      "is_main": true,
      "is_bare": false,
      "prunable": false
    },
    {
      "path": "/abs/path/to/epubforge-agent-scanner-1",
      "branch": "agent/scanner-1",
      "commit": "def5678",
      "is_main": false,
      "is_bare": false,
      "prunable": false
    }
  ],
  "count": 2
}
```

**错误处理**：

| 场景 | exit code | 错误 JSON |
|---|---|---|
| work dir 不存在 | 1 | `{"error": "work dir does not exist: ..."}` |
| 不在 Git 仓库中 | 1 | `{"error": "not a git repository: ..."}` |

### 10.4 workspace merge

**用途**：合并 agent 分支并进行语义验证。

**参数**：

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | 主仓库中的 work directory |
| `--branch` | str | 是 | - | 要 merge 的分支名 |
| `--timeout` | int | 否 | `60` | git merge 操作的超时秒数 |

> v1 始终自动 abort/rollback（与 PD5 reject-and-report 一致）。
> 未来如需手动冲突解决模式，可添加 `--manual-conflict` 参数。

**stdout JSON 输出**（accepted）：

```json
{
  "outcome": "accepted",
  "message": "merge validated",
  "branch": "agent/scanner-1",
  "merge_commit": "abc1234567",
  "pre_merge_sha": "def7890123",
  "base_sha256": "...",
  "merged_sha256": "...",
  "change_count": 5,
  "patch": { ... },
  "conflict_files": []
}
```

**stdout JSON 输出**（git_conflict）：

```json
{
  "outcome": "git_conflict",
  "message": "merge conflict in edit_state/book.json",
  "branch": "agent/scanner-1",
  "merge_commit": null,
  "pre_merge_sha": "def7890123",
  "base_sha256": "...",
  "merged_sha256": null,
  "change_count": 0,
  "patch": null,
  "conflict_files": ["edit_state/book.json"]
}
```

**stdout JSON 输出**（semantic_conflict）：

```json
{
  "outcome": "semantic_conflict",
  "message": "diff_books failed: unsupported Book-level delta(s): ...",
  "branch": "agent/scanner-1",
  "merge_commit": null,
  "pre_merge_sha": "def7890123",
  "base_sha256": "...",
  "merged_sha256": "...",
  "change_count": 0,
  "patch": null,
  "conflict_files": []
}
```

**exit code 规则**：

| outcome | exit code |
|---|---|
| `accepted` | 0 |
| `git_conflict` | 1 |
| `semantic_conflict` | 2 |
| `parse_error` | 2 |

**错误处理**：

| 场景 | exit code | 错误 JSON |
|---|---|---|
| 分支不存在 | 1 | `{"error": "branch ... does not exist"}` |
| 已有进行中的 merge | 1 | `{"error": "another merge is already in progress"}` |
| 工作树有未提交的更改 | 1 | `{"error": "working tree has uncommitted changes"}` |

### 10.5 workspace remove

**用途**：删除 worktree 和可选删除分支。

**参数**：

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | 主仓库中的 work directory |
| `--branch` | str | 是 | - | 要删除的分支名 |
| `--force` | bool (flag) | 否 | `False` | 强制删除未 merged 的分支 |

**stdout JSON 输出**：

```json
{
  "removed": true,
  "worktree_path": "/abs/path/to/epubforge-agent-scanner-1",
  "branch": "agent/scanner-1",
  "branch_deleted": true,
  "force_used": false
}
```

**错误处理**：

| 场景 | exit code | 错误 JSON |
|---|---|---|
| 分支未 merged 且无 --force | 1 | `{"error": "branch ... is not fully merged. Use --force to delete."}` |
| worktree 是主 worktree | 2 | `{"error": "cannot remove main worktree"}` |

### 10.6 workspace gc

**用途**：清理超龄的孤立 agent worktree。

**参数**：

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | 主仓库中的 work directory |
| `--max-age-days` | int | 否 | `7` | 超过此天数的未 merged worktree 将被清理 |
| `--dry-run` | bool (flag) | 否 | `False` | 只报告不执行删除 |

**stdout JSON 输出**：

```json
{
  "removed": [
    {
      "worktree_path": "/abs/path/...",
      "branch": "agent/scanner-old",
      "branch_deleted": true,
      "force_used": true
    }
  ],
  "skipped": [
    {"path": "/abs/path/...", "reason": "dry_run"},
    {"path": "/abs/path/...", "reason": "branch merged, should be cleaned by remove"}
  ],
  "pruned": 2,
  "dry_run": false
}
```

---

## 11. 测试策略

### 11.1 测试基础设施

所有测试需要一个真实的 Git 仓库。使用 `tmp_path` fixture 创建临时 Git 仓库：

```python
@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal Git repo with an initialized edit_state."""
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
    # Create work/book/edit_state/ with minimal book.json
    work_dir = repo / "work" / "book"
    _init_work_dir(work_dir)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo
```

### 11.2 单元测试：`tests/editor/test_workspace.py`

| 类别 | 用例 | 描述 |
|---|---|---|
| **repo root** | `test_find_repo_root` | 从 work_dir 找到 repo root |
| **repo root** | `test_find_repo_root_not_git` | 非 Git 目录抛 GitError |
| **branch naming** | `test_validate_branch_name_valid` | `agent/scanner-1` 等合法名称通过 |
| **branch naming** | `test_validate_branch_name_invalid` | `main`、`../escape`、`agent/../escape` 等非法名称拒绝 |
| **create** | `test_create_worktree_basic` | 创建 worktree，验证路径存在、分支存在、book.json 可读 |
| **create** | `test_create_worktree_custom_path` | 使用自定义 worktree_path |
| **create** | `test_create_worktree_base_ref` | 基于非 HEAD ref 创建 |
| **create** | `test_create_worktree_branch_exists` | 分支已存在时抛 GitError |
| **create** | `test_create_worktree_default_path` | 默认路径命名规范 |
| **list** | `test_list_worktrees_initial` | 初始仓库只有主 worktree |
| **list** | `test_list_worktrees_with_agents` | 创建多个 agent worktree 后列出 |
| **list** | `test_list_worktrees_agent_only` | `agent_only=True` 过滤 |
| **remove** | `test_remove_worktree_merged` | merged 分支正常删除 |
| **remove** | `test_remove_worktree_unmerged_no_force` | 未 merged 无 force 抛错 |
| **remove** | `test_remove_worktree_unmerged_force` | 未 merged + force 成功删除 |
| **remove** | `test_remove_worktree_main_rejected` | 尝试删除主 worktree 被拒 |
| **gc** | `test_gc_no_stale` | 无超龄 worktree，gc 返回空 removed |
| **gc** | `test_gc_removes_stale` | 超龄 worktree 被删除 |
| **gc** | `test_gc_dry_run` | dry_run 模式不实际删除 |
| **gc** | `test_gc_skips_merged` | 已 merged 的不做 force 删除 |
| **merge** | `test_merge_no_conflict` | 不同 block 修改，merge 成功，语义验证通过 |
| **merge** | `test_merge_git_conflict` | 同字段冲突，Git merge 失败，abort 并报告 |
| **merge** | `test_merge_semantic_conflict` | Git 成功但语义验证失败，reset 并报告 |
| **merge** | `test_merge_parse_error` | Git 成功但 book.json 损坏，reset 并报告 |
| **merge** | `test_merge_accepted_with_patch` | 验证返回的 patch JSON 可用于 apply |
| **merge** | `test_merge_rollback_on_semantic_failure` | 验证 semantic 失败后 HEAD 回到 pre_merge_sha |
| **merge** | `test_merge_rollback_uses_pre_merge_sha` | 验证回滚使用记录的 pre_merge_sha 而非 HEAD~1 |
| **merge** | `test_merge_timeout_cleanup` | 模拟 merge 超时后尝试 git merge --abort 清理 |
| **merge** | `test_merge_branch_not_found` | 分支不存在时返回友好错误而非 Git 错误 |
| **merge** | `test_merge_round_trip_diagnostics` | round-trip 失败时输出 divergent keys 诊断信息 |
| **ref resolve** | `test_resolve_book_at_ref` | 从 ref 读取 book.json |
| **ref resolve** | `test_resolve_book_at_ref_not_found` | ref 不存在或文件不存在抛 GitError |
| **ref resolve** | `test_resolve_book_at_ref_invalid_json` | ref 中的 book.json 不是合法 JSON 抛 ValueError |

### 11.3 CLI 集成测试：`tests/editor/test_workspace_cli.py`

| 用例 | 描述 |
|---|---|
| `test_cli_workspace_create` | 通过 CLI 创建 worktree，验证 JSON 输出 |
| `test_cli_workspace_create_bad_branch` | 无效分支名，exit code 2 |
| `test_cli_workspace_list` | 列出 worktree，验证 JSON 输出 |
| `test_cli_workspace_list_agent_only` | `--agent-only` 过滤 |
| `test_cli_workspace_merge_accepted` | 合并成功，exit code 0 |
| `test_cli_workspace_merge_conflict` | Git 冲突，exit code 1 |
| `test_cli_workspace_merge_semantic` | 语义冲突，exit code 2 |
| `test_cli_workspace_remove` | 删除 worktree，验证 JSON 输出 |
| `test_cli_workspace_remove_force` | `--force` 删除未 merged 分支 |
| `test_cli_workspace_gc` | GC 清理超龄 worktree |
| `test_cli_workspace_gc_dry_run` | `--dry-run` 不实际删除 |
| `test_cli_diff_books_with_refs` | `--base-ref` + `--proposed-ref` 工作 |
| `test_cli_diff_books_mixed_ref_file` | `--base-ref` + `--proposed-file` 混合使用 |
| `test_cli_diff_books_ref_conflict` | 同时指定 `--base-ref` 和 `--base-file` 报错 |

### 11.4 End-to-end 工作流测试

| 用例 | 描述 |
|---|---|
| `test_full_agent_workflow` | 创建 worktree -> 在 worktree 中 agent-output submit -> git commit -> merge -> remove |
| `test_concurrent_agents_no_overlap` | 两个 agent 修改不同 block，依次 merge 都成功 |
| `test_concurrent_agents_conflict` | 两个 agent 修改同一 block，第二个 merge 产生 Git 冲突 |

### 11.5 质量 gates

```bash
uv run pytest tests/editor/test_workspace.py
uv run pytest tests/editor/test_workspace_cli.py
uv run pyrefly check
```

---

## 12. 分阶段实施任务

### Sub-phase 7A：Git 操作封装基础

任务：

1. 新增 `src/epubforge/editor/workspace.py`。
2. 实现 `GitError`、`_run_git`、`_validate_branch_name`、`_default_worktree_path`、`_ensure_path_safe`。
3. 实现 `find_repo_root`。
4. 实现 `_parse_worktree_porcelain`。
5. 测试：repo root 发现、分支名验证、路径安全。

验收：

- `test_find_repo_root` 通过。
- `test_validate_branch_name_valid/invalid` 通过。
- `_run_git` 正确处理超时和非零 returncode。

### Sub-phase 7B：Worktree 创建和列出

任务：

1. 实现 `create_worktree`。
2. 实现 `list_worktrees`。
3. 实现返回类型 `WorktreeInfo`、`WorktreeCreateResult`。
4. 测试：创建、列出、porcelain 解析。

验收：

- `test_create_worktree_basic` 通过。
- `test_list_worktrees_with_agents` 通过。
- 创建的 worktree 中 `edit_state/book.json` 可被 `load_book` 正常读取。

### Sub-phase 7C：Worktree 删除和 GC

任务：

1. 实现 `remove_worktree`。
2. 实现 `gc_worktrees`。
3. 实现返回类型 `WorktreeRemoveResult`、`GCResult`。
4. 测试：正常删除、强制删除、GC 逻辑。

验收：

- `test_remove_worktree_merged/unmerged_force` 通过。
- `test_gc_removes_stale` 通过。
- GC 不误删主 worktree 或已 merged 的 worktree。

### Sub-phase 7D：Integration merge

任务：

1. 实现 `merge_and_validate`。
2. 实现 `abort_merge`。
3. 实现冲突文件解析 `_parse_conflict_files`。
4. 实现回滚逻辑。
5. 测试：无冲突 merge、Git 冲突、语义冲突、parse 错误、回滚验证。

验收：

- `test_merge_no_conflict` 通过（含 round-trip 验证）。
- `test_merge_git_conflict` 通过（abort 后 HEAD 不变）。
- `test_merge_semantic_conflict` 通过（reset 后 HEAD 回到 merge 前）。
- `test_merge_parse_error` 通过。

### Sub-phase 7E：Git ref 解析和 diff-books 扩展

任务：

1. 实现 `resolve_book_at_ref`、`resolve_book_path_at_ref`。
2. 扩展 `tool_surface.py` 的 `build_diff_books_result` 支持 ref 参数。
3. 扩展 `app.py` 的 `diff-books` 命令新增 `--base-ref` / `--proposed-ref` 参数。
4. 测试：ref 解析、混合参数使用。

验收：

- `test_resolve_book_at_ref` 通过。
- `test_cli_diff_books_with_refs` 通过。
- `--base-ref` / `--base-file` 互斥检查工作。

### Sub-phase 7F：CLI 命令组

任务：

1. 新增 `src/epubforge/editor/workspace_cli.py`。
2. 实现 `workspace create/list/merge/remove/gc` 五个子命令。
3. 在 `app.py` 中注册 `workspace` 子命令组。
4. 实现 `tool_surface.py` 中的 `run_workspace_*` 业务函数。
5. CLI 集成测试。

验收：

- 所有 CLI 命令可通过 `epubforge editor workspace <cmd>` 访问。
- JSON 输出格式正确。
- 错误情况返回正确 exit code。

### Sub-phase 7G：End-to-end 工作流测试

任务：

1. 实现完整 agent 工作流测试。
2. 实现并发 agent 测试（无冲突和有冲突场景）。
3. 补充边界情况测试。

验收：

- `test_full_agent_workflow` 通过。
- `test_concurrent_agents_no_overlap` 通过。
- `test_concurrent_agents_conflict` 通过。

---

## 13. 验收标准

### 13.1 必须满足

1. `create_worktree` 创建的 worktree 中，`EditorPaths` 可正常工作，`load_book` 可读取 `book.json`。
2. `list_worktrees` 正确解析 porcelain 输出，`agent_only` 过滤有效。
3. `remove_worktree` 在 merged 分支上正常删除 worktree 和分支。
4. `merge_and_validate` 对无冲突修改返回 `accepted`，且 round-trip 验证通过。
5. `merge_and_validate` 对 Git 冲突返回 `git_conflict` 并自动 abort。
6. `merge_and_validate` 对语义冲突返回 `semantic_conflict` 并回滚 merge commit。
7. `merge_and_validate` 对 book.json parse 失败返回 `parse_error` 并回滚。
8. 所有回滚后 HEAD 与 pre_merge_sha 一致（使用记录的 SHA 而非 HEAD~1）。
9. 分支命名验证拒绝非 `agent/*` 模式。
10. 所有 subprocess 调用有超时保护。
11. `diff-books --base-ref / --proposed-ref` 正确解析 Git ref 为 Book。
12. CLI 命令输出合法 JSON，exit code 遵循设计。

### 13.2 应满足

1. `gc_worktrees` 能正确识别和清理超龄 worktree。
2. End-to-end agent 工作流测试覆盖创建-编辑-提交-合并-清理全流程。
3. 并发 agent 测试覆盖无冲突和有冲突场景。
4. 错误消息包含可操作诊断信息。

### 13.3 可延后

1. 远程仓库操作（push/pull）。
2. 更复杂的冲突分类（如自动识别冲突类型并建议修复）。
3. worktree 中的 projection 自动 export。
4. merge queue / 批量 merge。
5. 跨 work_dir 的多 book 支持。

---

## 14. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Git 不在 PATH 中 | 所有 Git 操作失败 | `find_repo_root` 首先检查 `git --version`；失败时提供明确错误消息 |
| Git worktree 路径冲突（同名目录已存在） | `create_worktree` 失败 | 创建前检查路径是否存在；提供 `--worktree-path` 参数允许自定义路径 |
| Git merge commit 后 reset 失败 | 仓库状态不一致 | `reset --hard <pre_merge_sha>` 失败时在 IntegrationResult 中标记 dirty 状态，提示手动修复；使用 pre_merge_sha 而非 HEAD~1 避免误回滚 |
| 大仓库 merge 超时 | merge 命令超时 | 默认 60s 超时；CLI 暴露超时参数；提示用户增加超时或减小 diff 范围 |
| Git porcelain 格式在不同 Git 版本间变化 | worktree list 解析失败 | 测试在项目 CI 环境验证；解析器对未知字段容错（跳过而非报错） |
| worktree 中 edit_state 路径与主仓库不同 | EditorPaths 解析失败 | 设计文档已确认 worktree 具有完整工作树，路径结构一致；测试覆盖 |
| agent 在 worktree 中工作时 worktree 被意外删除 | agent 命令失败 | worktree 删除只应由 supervisor 执行（PD6）；agent 不管理 worktree |
| `git show <ref>:<path>` 对大文件慢 | diff-books --ref 参数响应慢 | 设置 10s 超时；book.json 通常 < 10MB |
| merge 后 book.json 虽合法但 projection 文件过时 | agent 如果读取 projection 会看到旧数据 | merge_and_validate 不自动 re-export projection；supervisor 在 accept 后手动 export |
| 并发 merge（两个 merge 同时运行） | Git 锁冲突 | Git 自身的 index.lock 机制防止并发；返回 GitError 给调用者 |
| merge 过程中超时 | 工作树可能停留在 merge-in-progress 状态；`.git/index.lock` 可能残留 | `_run_git` 将 `TimeoutExpired` 包装为 `GitError`；`merge_and_validate` 在捕获超时后尝试 `git merge --abort`（best effort）；错误消息提示用户检查 `.git/index.lock`；如果 lock 文件残留，用户可手动删除或下次操作前由 `_run_git` 检测并报告 |

---

## 15. Open questions register

> 状态说明：`default-proceed` 表示无需等待人类，按默认假设实现；`unresolved-after-review` 表示经过多轮 plan-review 仍未关闭，仍按默认假设实现并保留复核点。

| ID | 问题 | 影响 | 默认假设 / 实现路径 | 推荐决策 | 何时复核 | 状态 |
|---|---|---|---|---|---|---|
| OQ-01 | merge 语义失败后的回滚方式：`git merge --abort` vs `git reset --hard <pre_merge_sha>` | `--abort` 只能在 merge 未完成时使用；如果 merge 已 commit（`--no-ff` 默认创建 commit），需要 `reset` | Git merge 有冲突时（未完成）用 `--abort`；Git merge 成功但语义失败时（已 commit）用 `reset --hard <pre_merge_sha>`（在 merge 前记录 SHA） | 分两种路径处理，使用 pre_merge_sha 而非 HEAD~1 | 如果发现 `--no-ff` 在冲突时也创建了 commit（不应该），需要统一为 `reset` | default-proceed |
| OQ-02 | work_dir_rel 如何计算 | `merge_and_validate` 需要 `work_dir_rel` 来定位 `edit_state/book.json` | 从 CLI 接收 `work` 参数，用 `work.resolve().relative_to(repo_root.resolve())` 计算 | 在 `run_workspace_merge` 中计算并传入 | 如果 work_dir 不在 repo_root 下（symlink 等），relative_to 会失败 | default-proceed |
| OQ-03 | agent 提交 projection 到 Git 的时机 | PD2 决定提交 projection，但何时提交不明确 | agent 完成 `agent-output submit --apply` 后，应 `projection export` 然后 `git add edit_state/` 并 commit；projection export 由 agent 或 supervisor 脚本负责，不由 Phase 7 自动触发 | Phase 7 不自动 export/commit projection；在 agent 工作循环文档中说明建议流程 | 如果实测发现 agent 经常忘记 export，考虑在 `submit --apply` 后自动 export | default-proceed |
| OQ-04 | gc_worktrees 如何确定 worktree 的最后活跃时间 | `git log -1 --format=%ct <branch>` 获取的是最后 commit 时间，不是"最后工作时间"；刚创建但未 commit 的分支会返回 fork-point 时间（可能很老），导致误删 | 使用 branch 最后 commit 时间作为活跃时间代理，但 GC 扫描时先检查 `git log <base>..<agent> --oneline` 是否有输出：无输出表示 agent 尚未提交新 commit，跳过此分支不参与 age 计算。备选方案：用 worktree 目录 mtime 作为活跃时间下界 | 先检查有无新 commit，再做 age 判断 | 如果需要更精确的活跃时间（如文件 mtime），在 GC 中添加 mtime 检查 | default-proceed |
| OQ-05 | merge 前是否检查主分支有未提交更改 | 未提交更改会导致 merge 失败或产生意外结果 | `merge_and_validate` 开头运行 `git status --porcelain`，如果有未提交更改则拒绝 merge，报告 `{"error": "working tree has uncommitted changes"}` | 始终检查 clean working tree | - | default-proceed |
| OQ-06 | Git `merge --no-ff` 的 commit message 格式 | 需要标准化 merge commit 信息 | 使用 `-m "Merge agent/<kind>-<id> into <current_branch>"`；不包含 patch 详情 | 简洁的默认消息；supervisor 可后续 amend | - | default-proceed |
| OQ-07 | 是否允许 merge 到非 main/master 分支 | orchestrator 可能有 integration branch | 不限制目标分支；`merge_and_validate` 在当前 HEAD 所在的 branch 上执行 merge | 由 supervisor 确保在正确分支上调用 | 如果需要显式 target branch 参数，添加 `--target-branch` | default-proceed |
| OQ-08 | workspace create 是否自动初始化 edit_state | 如果 worktree 基于的 ref 没有 edit_state，create 后 agent 无法工作 | 不自动初始化；create 要求主仓库的 `edit_state/book.json` 已存在（通过验证 work_dir 是 initialized 的）；worktree 继承主仓库的 edit_state | create 前验证 `ensure_initialized` | - | default-proceed |
| OQ-09 | 多个 work_dir 在同一仓库中的情况 | 一个仓库可能有 `work/book-a/` 和 `work/book-b/` | Phase 7 的 workspace 命令每次只操作一个 work_dir；worktree 是仓库级别的（包含所有 work_dir）；agent 只应操作其被分配的 work_dir | worktree 包含完整仓库内容；CLI 通过 `work` 参数指定操作哪个 work_dir | 如果多 work_dir 场景常见，考虑 sparse checkout 或 per-book worktree | default-proceed |

---

## 16. Agent 工作循环参考

以下是 Phase 7 完成后，完整的 agent 端到端工作流程：

### 16.1 Supervisor 侧

```bash
# 1. 创建 worktree
epubforge editor workspace create work/book --branch agent/scanner-1

# 输出:
# {"created": true, "worktree_path": "../epubforge-agent-scanner-1", "branch": "agent/scanner-1", ...}

# 2. 将 worktree 信息传递给 agent（通过 prompt、环境变量等）

# ... agent 完成工作后 ...

# 3. 合并 agent 的修改
epubforge editor workspace merge work/book --branch agent/scanner-1

# 输出 (成功):
# {"outcome": "accepted", "merge_commit": "abc1234", "change_count": 5, ...}

# 4. 清理 worktree
epubforge editor workspace remove work/book --branch agent/scanner-1

# 5. 定期 GC
epubforge editor workspace gc work/book --max-age-days 7
```

### 16.2 Agent 侧（在 worktree 中）

```bash
# agent 在 worktree 目录中工作
cd ../epubforge-agent-scanner-1

# 1. 读取当前状态
epubforge editor projection export work/book --chapter ch-1

# 2. 开始 agent output
epubforge editor agent-output begin work/book \
  --kind scanner --chapter ch-1 --agent scanner-1

# 3. 推理后提交修改
epubforge editor agent-output add-command work/book <output-id> \
  --command-file scratch/command.json

epubforge editor agent-output add-note work/book <output-id> \
  --text "page 12 footnote density is abnormal"

# 4. 提交并应用
epubforge editor agent-output submit work/book <output-id> --apply

# 5. 可选：重新 export 查看修改后状态
epubforge editor projection export work/book --chapter ch-1

# 6. 可选：继续下一轮编辑（重复 2-5）

# 7. 提交到 Git
git add edit_state/
git commit -m "scanner-1 scan chapter ch-1"
```

### 16.3 Integration 验证流程

```bash
# supervisor 侧使用 diff-books 查看修改前后差异
epubforge editor diff-books work/book \
  --base-ref main \
  --proposed-ref agent/scanner-1

# 或者使用 workspace merge 一步完成 merge + validate
epubforge editor workspace merge work/book --branch agent/scanner-1
```

---

## 17. 参考

- 主计划：`.refactor-planning/agentic-improvement/agentic-improvement.md`
- Phase 6 Book diff：`.refactor-planning/agentic-improvement/phase6-book-diff.md`
- Semantic IR：`src/epubforge/ir/semantic.py`
- BookPatch / apply：`src/epubforge/editor/patches.py`
- Book diff：`src/epubforge/editor/diff.py`
- EditorPaths / state：`src/epubforge/editor/state.py`
- CLI 注册：`src/epubforge/editor/app.py`
- Tool surface：`src/epubforge/editor/tool_surface.py`
- Subprocess 模式参考：`src/epubforge/editor/scratch.py`
