# Phase 2 实施计划：AgentOutput 模型 + CLI 命令组

> 依赖：Phase 1（BookPatch 模型）已完成。假定 `BookPatch`、`PatchScope`、`IRChange` 类型、
> `validate_book_patch`、`apply_book_patch` 已在 `editor/patches.py` 中存在。
>
> **Phase 1 接口规范（本计划所有代码示例均遵循）**：
> - `validate_book_patch(book: Book, patch: BookPatch) -> None` — 参数顺序 `(book, patch)`，失败时抛出 `PatchError`
> - `apply_book_patch(book: Book, patch: BookPatch) -> Book` — 返回新 Book 对象，失败时抛出 `PatchError`
> - `PatchError(reason: str, patch_id: str)` — 携带 `.reason` 和 `.patch_id` 属性
> - `PatchScope(chapter_uid=None)` — `chapter_uid=None` 表示 book-wide scope（见 Phase 1 §2.3 [R2]）

---

## 1. 文件布局

### 新建文件

| 文件路径 | 说明 |
|---|---|
| `src/epubforge/editor/agent_output.py` | `AgentOutput` Pydantic 模型、存储路径助手、validate/submit 业务逻辑 |
| `src/epubforge/editor/patch_commands.py` | `PatchCommand` 模型（Phase 2 中作为透传结构，Phase 3 中会加编译逻辑） |
| `src/epubforge/editor/agent_output_cli.py` | Typer CLI 命令组 `agent_output_app`（注意：不叫 `agent_output_commands.py`，以区别于 `patch_commands.py`） |
| `tests/test_agent_output.py` | 所有 Phase 2 测试 |

> [R1: addressed] **D1**：文件命名统一。PatchCommand 模型放在 `patch_commands.py`（不叫 `commands.py`），CLI 放在 `agent_output_cli.py`（不叫 `agent_output_commands.py`）。两个名字不再产生混淆。

### 修改文件

| 文件路径 | 修改内容 |
|---|---|
| `src/epubforge/editor/state.py` | 新增 `agent_outputs_dir`、`agent_outputs_archives_dir` 到 `EditorPaths`；在 `resolve_editor_paths` 中补充对应字段 |
| `src/epubforge/editor/tool_surface.py` | 新增 `run_agent_output_begin`、`run_agent_output_add_note`、`run_agent_output_add_question`、`run_agent_output_add_command`、`run_agent_output_add_patch`、`run_agent_output_add_memory_patch`、`run_agent_output_validate`、`run_agent_output_submit` 八个函数 |
| `src/epubforge/editor/app.py` | 注册 `agent_output_app` Typer 子命令组，并挂载到 `editor_app` |

### 不修改文件

- `ops.py`、`apply.py`、`leases.py` 在 Phase 2 中不触碰（Phase 4 才会删除）。
- `memory.py` 不修改（`MemoryPatch`、`OpenQuestion` 直接 import 复用）。
- `patches.py` 不修改（Phase 1 产出，直接 import）。

---

## 2. AgentOutput 模型

### 2.1 文件位置

`src/epubforge/editor/agent_output.py`

### 2.2 完整模型定义

```python
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from epubforge.editor._validators import (
    StrictModel,
    require_non_empty,
    validate_utc_iso_timestamp,
    validate_uuid4,
)
from epubforge.editor.patch_commands import PatchCommand
from epubforge.editor.memory import MemoryPatch, OpenQuestion
from epubforge.editor.patches import BookPatch

AgentKind = Literal["scanner", "fixer", "reviewer", "supervisor"]


class AgentOutput(StrictModel):
    output_id: str
    kind: AgentKind
    agent_id: str
    chapter_uid: str | None = None
    created_at: str
    updated_at: str
    patches: list[BookPatch] = Field(default_factory=list)
    commands: list[PatchCommand] = Field(default_factory=list)
    memory_patches: list[MemoryPatch] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
```

### 2.3 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `output_id` | `str` (UUID4) | 全局唯一标识。`begin` 命令自动生成 |
| `kind` | Literal 4选1 | agent 角色，决定权限检查规则（见 §5） |
| `agent_id` | `str` | 非空字符串，标识产出这个 output 的 agent 实例（如 `scanner-1`、`fixer-ch3`） |
| `chapter_uid` | `str \| None` | chapter 范围。`None` 表示全书范围。supervisor 可不限；scanner/fixer/reviewer 提交 patch 时必须与此一致 |
| `created_at` | `str` (UTC ISO) | `begin` 命令写入，之后不可变 |
| `updated_at` | `str` (UTC ISO) | 每次 `add-*` 命令后更新 |
| `patches` | `list[BookPatch]` | 底层 UID-addressed Book IR 变更。可直接 apply |
| `commands` | `list[PatchCommand]` | 高层 macro 命令，submit 时编译为 `BookPatch` |
| `memory_patches` | `list[MemoryPatch]` | 对 `EditMemory` 的变更（可多个，逐个顺序 merge） |
| `open_questions` | `list[OpenQuestion]` | 需要 supervisor/reviewer 决策的问题 |
| `notes` | `list[str]` | agent 观察备注，不直接修改状态，仅归档 |
| `evidence_refs` | `list[str]` | VLM observation id 或其他 evidence 引用（Phase 2 不校验，留 TODO 注释，等 Phase 9 VLM 系统）|

> [R1: addressed] **D5**：`evidence_refs` 有意不校验，计划中明确说明这是 Phase 9 VLM 系统的预留字段，Phase 2 只存储不验证。

### 2.4 字段 validator

```python
@field_validator("output_id")
@classmethod
def _validate_output_id(cls, value: str) -> str:
    return validate_uuid4(value, field_name="output_id")

@field_validator("agent_id")
@classmethod
def _validate_agent_id(cls, value: str) -> str:
    return require_non_empty(value, field_name="agent_id")

@field_validator("chapter_uid")
@classmethod
def _validate_chapter_uid(cls, value: str | None) -> str | None:
    if value is None:
        return None
    return require_non_empty(value, field_name="chapter_uid")

@field_validator("created_at", "updated_at")
@classmethod
def _validate_timestamps(cls, value: str, info) -> str:
    return validate_utc_iso_timestamp(value, field_name=info.field_name)

@field_validator("notes")
@classmethod
def _validate_notes(cls, value: list[str]) -> list[str]:
    return [note.strip() for note in value if note.strip()]

@model_validator(mode="after")
def _validate_timestamps_order(self) -> AgentOutput:
    if self.updated_at < self.created_at:
        raise ValueError("updated_at must be >= created_at")
    return self
```

### 2.5 PatchCommand 模型（patch_commands.py，Phase 2 版本）

Phase 2 中 `PatchCommand` 是一个**占位结构**，Phase 3 会扩充编译逻辑。当前只需能存储、校验、序列化。

```python
# src/epubforge/editor/patch_commands.py

from __future__ import annotations

from typing import Any, Literal
from pydantic import Field, field_validator

from epubforge.editor._validators import StrictModel, require_non_empty


class PatchCommand(StrictModel):
    """High-level ergonomic command. Compiled to BookPatch in Phase 3."""
    command_id: str
    op: str   # e.g. "split_block", "merge_blocks", "pair_footnote", etc.
    agent_id: str
    rationale: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("command_id")
    @classmethod
    def _validate_command_id(cls, value: str) -> str:
        from epubforge.editor._validators import validate_uuid4
        return validate_uuid4(value, field_name="command_id")

    @field_validator("op", "agent_id", "rationale")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)
```

> **Phase 3 注意**：Phase 3 会把 `op` 从自由字符串收敛为 `Literal[...]`，并添加
> `compile() -> BookPatch` 方法。Phase 2 故意保持宽松，让 agent 可以先写命令再等 Phase 3 补齐编译器。

---

## 3. 存储格式

### 3.1 目录结构

```
edit_state/
  agent_outputs/                     # in-progress output 文件
    <output_id>.json
  agent_outputs/archives/            # submit 成功后归档
    <output_id>_<submitted_at>.json
  book.json
  memory.json
  ...
```

- `agent_outputs/` 目录由 `begin` 命令首次创建（`mkdir -p`）。
- 每个 in-progress output 以 `<output_id>.json` 命名。
- submit 成功后归档到 `agent_outputs/archives/<output_id>_<submitted_at>.json`，其中 `submitted_at` 格式为 `YYYYMMDD-HHMMSS`（ISO 8601 紧凑形式，方便文件名排序）。
- 归档写入使用写临时文件再 `os.replace` 的原子操作（见 §6.3）。

### 3.2 在 state.py 中的路径扩展

```python
AGENT_OUTPUTS_DIRNAME = "agent_outputs"
AGENT_OUTPUTS_ARCHIVES_DIRNAME = "archives"

@dataclass(frozen=True)
class EditorPaths:
    # ...existing fields...
    agent_outputs_dir: Path
    agent_outputs_archives_dir: Path
```

在 `resolve_editor_paths` 中补充：

```python
agent_outputs_dir=edit_state_dir / AGENT_OUTPUTS_DIRNAME,
agent_outputs_archives_dir=edit_state_dir / AGENT_OUTPUTS_DIRNAME / AGENT_OUTPUTS_ARCHIVES_DIRNAME,
```

### 3.3 JSON Schema

`AgentOutput` 直接使用 Pydantic `model_dump_json(indent=2)` 序列化，存储为标准 JSON。
文件内容即 `AgentOutput.model_json_schema()` 所描述的结构，不额外包装外层信封。

示例文件内容（简化）：

```json
{
  "output_id": "a1b2c3d4-...",
  "kind": "scanner",
  "agent_id": "scanner-1",
  "chapter_uid": "ch-001-a3f",
  "created_at": "2026-04-24T10:00:00Z",
  "updated_at": "2026-04-24T10:05:00Z",
  "patches": [],
  "commands": [],
  "memory_patches": [],
  "open_questions": [],
  "notes": ["第 12 页脚注密度异常，需要复查 callout 归属。"],
  "evidence_refs": []
}
```

---

## 4. CLI 命令详细规格

### 4.1 命令组注册方式

在 `app.py` 中新增 Typer 子组：

```python
from epubforge.editor.agent_output_cli import agent_output_app

# 挂载到 editor_app
editor_app.add_typer(agent_output_app, name="agent-output")
```

`src/epubforge/editor/agent_output_cli.py` 包含 Typer 命令组定义，统一通过 `_run()` 模式
调用 `tool_surface.py` 中的业务函数，保持与现有命令风格一致。

### 4.2 `agent-output begin`

**功能**：创建新的 `AgentOutput` 文件，返回 `output_id`。

**命令签名**：

```
epubforge editor agent-output begin <work> \
  --kind <scanner|fixer|reviewer|supervisor> \
  --agent <agent_id> \
  [--chapter <chapter_uid>]
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` (位置参数) | 是 | work 目录 |
| `--kind` | str | 是 | agent 角色 |
| `--agent` | str | 是 | agent 标识 |
| `--chapter` | str | 否 | chapter UID；缺省表示全书范围 |

**行为**：

1. `resolve_editor_paths(work)` → `ensure_initialized(paths)`
2. 如果指定了 `--chapter`，加载 `book.json` 并验证该 chapter UID 存在；否则报 `CommandError`
3. **[R1: addressed] S 类规则**：如果 `kind` 为 `scanner`，且未指定 `--chapter`，报错 `CommandError("scanner must specify --chapter")`。Scanner 必须绑定到特定 chapter（因为 scanner 需要更新 `chapter_status.read_passes`，全书范围的 scanner 无法满足此要求）。
4. 生成 `output_id = str(uuid4())`
5. 构建 `AgentOutput(output_id=..., kind=..., agent_id=..., chapter_uid=..., created_at=now, updated_at=now)`
6. `paths.agent_outputs_dir.mkdir(parents=True, exist_ok=True)`
7. `atomic_write_model(paths.agent_outputs_dir / f"{output_id}.json", output)`
8. stdout 输出 JSON：`{"output_id": "<uuid>", "path": "<absolute_path>"}` [R2: removed base_version/op_log_version]

> [R1: addressed, R2: removed] **D6**：`begin` 返回值不再包含 `base_version`；版本控制由 Git worktree 负责（Phase 7），字段级冲突由 BookPatch changes 中的 `old` 前置条件捕获。

**错误处理**：

- `--kind` 不在允许值内 → exit 2，输出 `{"error": "--kind must be one of: scanner, fixer, reviewer, supervisor"}`
- `--agent` 为空 → exit 2
- `kind == scanner` 且未指定 `--chapter` → exit 2，`{"error": "scanner must specify --chapter"}`
- `--chapter` 指定但不存在 → exit 1，`{"error": "chapter not found: <uid>"}`
- editor 未初始化 → exit 1，`{"error": "editor state is not initialized: ..."}`

---

### 4.3 `agent-output add-note`

**功能**：追加一条观察备注字符串到指定 output 的 `notes` 列表。

**命令签名**：

```
epubforge editor agent-output add-note <work> <output_id> \
  --text <note_text>
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` | 是 | work 目录 |
| `output_id` | `str` | 是 | 位置参数，目标 output 的 UUID |
| `--text` | str | 是 | 备注内容（非空字符串） |

**行为**：

1. `load_agent_output(paths, output_id)` — 读取并反序列化 output 文件
2. 检查 `text.strip()` 非空
3. `output.notes.append(text.strip())`
4. 更新 `output.updated_at = now`
5. 原子写回 `<output_id>.json`
6. stdout 输出：`{"output_id": "<uuid>", "notes_count": <n>}`

**错误处理**：

- output 文件不存在 → exit 1，`{"error": "output not found: <output_id>"}`
- `--text` 为空或仅空白 → exit 2，`{"error": "--text must not be empty"}`

---

### 4.4 `agent-output add-question`

**功能**：追加一条 `OpenQuestion` 到指定 output 的 `open_questions` 列表。

**命令签名**：

```
epubforge editor agent-output add-question <work> <output_id> \
  --question <question_text> \
  [--context-uid <uid>]...  \
  [--option <option_text>]...
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` | 是 | |
| `output_id` | `str` | 是 | 位置参数 |
| `--question` | str | 是 | 问题文本 |
| `--context-uid` | str（可重复） | 否 | 关联 block/chapter UID |
| `--option` | str（可重复） | 否 | 候选答案选项 |

**行为**：

1. 加载 output
2. 生成 `q_id = str(uuid4())`
3. 构建 `OpenQuestion(q_id=q_id, question=question, context_uids=context_uids, options=options, asked_by=output.agent_id)`
   - 注意：`asked_by` 强制使用 `output.agent_id`，不允许调用方自行指定（参见测试 §8.12 T7）
4. 对所有 `context_uid`，验证其存在于当前 book（chapter uid 或 block uid）；任一不存在则报错
5. `output.open_questions.append(question_obj)`
6. 更新 `output.updated_at = now`
7. 原子写回
8. stdout 输出：`{"output_id": "...", "q_id": "...", "questions_count": <n>}`

**错误处理**：

- `--question` 为空 → exit 2
- `--context-uid` 中有不存在的 UID → exit 1，`{"error": "uid not found: <uid>"}`

---

### 4.5 `agent-output add-command`

**功能**：从 JSON 文件追加一条 `PatchCommand` 到 `commands` 列表。

**命令签名**：

```
epubforge editor agent-output add-command <work> <output_id> \
  --command-file <path_to_json>
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` | 是 | |
| `output_id` | `str` | 是 | 位置参数 |
| `--command-file` | `Path` | 是 | 包含单个 PatchCommand 对象的 JSON 文件 |

**行为**：

1. 读取 `--command-file` 文件内容
2. `json.loads(...)` — 非法 JSON 则报错
3. `PatchCommand.model_validate(parsed)` — schema 不合法则报错
4. 加载 output
5. 检查 `command.agent_id == output.agent_id`（Phase 2 中只做宽松警告，不强制报错；Phase 3 后可根据需要收紧）
6. `output.commands.append(command)`
7. 更新 `output.updated_at = now`
8. 原子写回
9. stdout 输出：`{"output_id": "...", "command_id": "...", "commands_count": <n>}`

**错误处理**：

- `--command-file` 不存在 → exit 2，`{"error": "command file not found: <path>"}`
- 文件不是合法 JSON → exit 1，`{"error": "invalid JSON: ..."}`
- PatchCommand schema 不合法 → exit 1，`{"error": "PatchCommand validation failed: ..."}`

---

### 4.6 `agent-output add-patch`

**功能**：从 JSON 文件追加一条 `BookPatch` 到 `patches` 列表。

**命令签名**：

```
epubforge editor agent-output add-patch <work> <output_id> \
  --patch-file <path_to_json>
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` | 是 | |
| `output_id` | `str` | 是 | 位置参数 |
| `--patch-file` | `Path` | 是 | 包含单个 BookPatch 对象的 JSON 文件 |

**行为**：

1. 读取文件，`json.loads`
2. `BookPatch.model_validate(parsed)` — schema 校验
3. 加载 output
4. 检查 `patch.scope` 与 `output.chapter_uid` 是否一致（见 §5.7）
5. `output.patches.append(patch)`
6. 更新 `output.updated_at`
7. 原子写回
8. stdout 输出：`{"output_id": "...", "patch_id": "...", "patches_count": <n>}`

**错误处理**：

- 文件不存在 → exit 2
- JSON 不合法 → exit 1
- BookPatch schema 不合法 → exit 1
- scope 与 chapter_uid 不一致 → exit 1，`{"error": "patch scope mismatch: ..."}`

---

### 4.7 `agent-output add-memory-patch`

**功能**：追加一条 `MemoryPatch` 到 `memory_patches` 列表。

`MemoryPatch` 从 JSON 文件提供（Phase 2 只支持文件方式）。

**命令签名**：

```
epubforge editor agent-output add-memory-patch <work> <output_id> \
  --patch-file <path_to_json>
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` | 是 | |
| `output_id` | `str` | 是 | 位置参数 |
| `--patch-file` | `Path` | 是 | 包含单个 MemoryPatch 对象的 JSON 文件 |

**行为**：

1. 读取文件，`json.loads`
2. `MemoryPatch.model_validate(parsed)`
3. 加载 output
4. **[R1: addressed] S4**：对 MemoryPatch 中的 UID 引用做即时预验证（加载当前 book，检查 `mp.conventions[*].evidence_uids` 和 `mp.patterns[*].affected_uids` 的 UID 存在性）。如果此时 book 加载成本过高，允许推迟到 `validate` 命令再检查，但必须在此处打印 warning。Phase 2 选择**推迟到 validate** 以保持 `add-*` 命令轻量，但文档明确说明 validate 阶段会检查。
5. `output.memory_patches.append(mp)`
6. 更新 `output.updated_at`
7. 原子写回
8. stdout 输出：`{"output_id": "...", "memory_patches_count": <n>}`

**错误处理**：

- 文件不存在 → exit 2
- JSON 不合法 → exit 1
- MemoryPatch schema 不合法 → exit 1

---

### 4.8 `agent-output validate`

**功能**：对指定 output 执行完整验证，不修改任何状态。

**命令签名**：

```
epubforge editor agent-output validate <work> <output_id>
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` | 是 | |
| `output_id` | `str` | 是 | 位置参数 |

**行为**：

1. 加载 output（schema 不合法则直接失败）
2. 加载 `book.json`
3. 执行 §5 中所有验证规则
4. 收集所有 errors，统一返回（不 fail-fast，尽量多报错）
5. stdout 输出：
   - 验证通过：`{"valid": true, "output_id": "...", "errors": []}`（exit 0）
   - 验证失败：`{"valid": false, "output_id": "...", "errors": ["...", ...]}`（exit 1）

---

### 4.9 `agent-output submit`

**功能**：validate → compile commands → apply patches → apply memory patches → archive output。

**命令签名**：

```
epubforge editor agent-output submit <work> <output_id> \
  [--apply] \
  [--stage]
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `work` | `Path` | 是 | |
| `output_id` | `str` | 是 | 位置参数 |
| `--apply` | flag | 否 | 实际应用修改。不传则只做 dry-run 验证 |
| `--stage` | flag | 否 | 写入 staging 文件，不修改 book.json（见下方说明） |

> [R1: addressed] **V2**：`--stage` 模式接口已设计，但 Phase 2 仅作占位实现（打印 `{"staged": false, "message": "stage mode not yet implemented, will be added in Phase 4"}` 并 exit 0）。Phase 4 删除旧 staging 系统时再正式实现此模式。不在 Phase 2 实现的原因：旧 `propose-op` 系统的 staging 文件格式（JSONL `OpEnvelope`）与新 `BookPatch` 格式不兼容，在 Phase 4 统一迁移前不应引入第二种 staging 格式。

**行为（`--apply` 模式）**：见 §6。

**行为（不传任何 flag，dry-run）**：

1. 执行所有 validate 检查
2. stdout 输出验证结果，不修改任何文件，exit 0（通过）或 1（失败）

**行为（`--stage` 模式，Phase 2 占位）**：

1. stdout 输出：`{"staged": false, "message": "stage mode not yet implemented, will be added in Phase 4"}`
2. exit 0

**错误处理**：

- validate 任一失败 → exit 1，不执行任何修改
- apply 过程中 `apply_book_patch` 抛出 `PatchError` → 回滚到 apply 前状态（见 §6），exit 1

---

## 5. Validate 逻辑

`validate_agent_output(output: AgentOutput, book: Book) -> list[str]`

返回 errors 列表。空列表 = 合法。

### 5.1 顶层 schema 校验

已由 Pydantic `AgentOutput.model_validate` 完成（load 时触发）。validate 命令只处理语义层。

### 5.2 chapter_uid 存在性校验 [R2: removed base_version/op_log_version]

```python
if output.chapter_uid is not None:
    chapter_uids = {ch.uid for ch in book.chapters}
    if output.chapter_uid not in chapter_uids:
        errors.append(f"chapter_uid not found: {output.chapter_uid}")
```

### 5.3 BookPatch 校验

> [R1: addressed] **S1**：Phase 1 定义 `validate_book_patch(book: Book, patch: BookPatch) -> None`，失败时抛出 `PatchError`。Phase 2 的 validate 需要收集所有 patch 的错误（不 fail-fast），因此在此处包装 try/except：

```python
from epubforge.editor.patches import validate_book_patch, PatchError

for i, patch in enumerate(output.patches):
    try:
        validate_book_patch(book, patch)   # 参数顺序：(book, patch)
    except PatchError as e:
        errors.append(f"patches[{i}] ({patch.patch_id}): {e.reason}")
```

`validate_book_patch` 在 Phase 1 中是 fail-fast 的（单个 patch 级别），Phase 2 的循环在外层捕获后继续处理下一个 patch，从而实现跨 patch 的完整错误收集。

### 5.4 PatchCommand 校验

Phase 2 中 `PatchCommand` 的 `op` 字段未绑定 Literal；暂时只做 schema 合法性检查：

```python
for i, cmd in enumerate(output.commands):
    # Phase 2: structural validation only
    if not cmd.op.strip():
        errors.append(f"commands[{i}]: op must not be empty")
    if not cmd.rationale.strip():
        errors.append(f"commands[{i}]: rationale must not be empty")
```

> Phase 3 补充编译后，validate 将调用 `compile_patch_command(cmd, book)` 并检查编译结果。

### 5.5 MemoryPatch 校验

> [R1: addressed] **S4**：补充对 `mp.conventions[*].evidence_uids` 和 `mp.patterns[*].affected_uids` 中 UID 引用的存在性校验。

`MemoryPatch` 本身是 Pydantic 模型，结构校验在 load 时完成。
validate 阶段额外检查所有 UID 引用的存在性：

```python
for i, mp in enumerate(output.memory_patches):
    # Check chapter_status UIDs
    for status in mp.chapter_status:
        if status.chapter_uid not in {ch.uid for ch in book.chapters}:
            errors.append(
                f"memory_patches[{i}].chapter_status: "
                f"chapter_uid not found: {status.chapter_uid}"
            )

    # Check open_questions context UIDs
    for q in mp.open_questions:
        for uid in q.context_uids:
            if not _uid_exists(uid, book):
                errors.append(
                    f"memory_patches[{i}].open_questions[q_id={q.q_id}]: "
                    f"context_uid not found: {uid}"
                )

    # [R1: addressed] S4 — Check convention evidence_uids
    for conv in mp.conventions:
        for uid in conv.evidence_uids:
            if not _uid_exists(uid, book):
                errors.append(
                    f"memory_patches[{i}].conventions[key={conv.canonical_key}]: "
                    f"evidence_uid not found: {uid}"
                )

    # [R1: addressed] S4 — Check pattern affected_uids
    for pattern in mp.patterns:
        for uid in pattern.affected_uids:
            if not _uid_exists(uid, book):
                errors.append(
                    f"memory_patches[{i}].patterns[key={pattern.canonical_key}]: "
                    f"affected_uid not found: {uid}"
                )
```

### 5.6 scope 一致性校验

> [R1: addressed, R2: simplified] **D2/A3**：`PatchScope` 已简化——`chapter_uid=None` 即 book-wide（Phase 1 §2.3 [R2]）。不再需要辅助函数。

如果 `output.chapter_uid` 不为 `None`，所有 patches 的 scope 必须明确指定相同的 `chapter_uid`（不允许 book-wide scope）：

```python
for i, patch in enumerate(output.patches):
    if output.chapter_uid is not None:
        if patch.scope.chapter_uid is None:
            errors.append(
                f"patches[{i}]: chapter-scoped output (chapter_uid={output.chapter_uid!r}) "
                f"may not contain book-wide patches (scope.chapter_uid=None)"
            )
        elif patch.scope.chapter_uid != output.chapter_uid:
            errors.append(
                f"patches[{i}]: scope.chapter_uid {patch.scope.chapter_uid!r} "
                f"does not match output.chapter_uid {output.chapter_uid!r}"
            )
```

### 5.7 kind-specific 权限规则

#### scanner

> [R1: addressed] **V3/D2**：scanner 不允许 topology 变更（`insert_node`、`delete_node`、`move_node`），不允许 book-wide scope，且 `chapter_uid` 必须非 None（`begin` 时已强制）。

```python
if output.kind == "scanner":
    for i, patch in enumerate(output.patches):
        for j, change in enumerate(patch.changes):
            if change.op != "set_field":
                errors.append(
                    f"scanner output patches[{i}].changes[{j}]: "
                    f"scanner may only submit set_field changes, got {change.op!r}"
                )
        if patch.scope.chapter_uid is None:
            errors.append(
                f"scanner output patches[{i}]: book-wide scope requires supervisor"
            )
```

> [R1: addressed] **V1**：scanner 必须更新对应 chapter 的 `read_passes`。在 validate 阶段检查：

```python
    if output.kind == "scanner" and output.chapter_uid is not None:
        has_read_pass_update = any(
            cs.chapter_uid == output.chapter_uid and cs.read_passes > 0
            for mp in output.memory_patches
            for cs in mp.chapter_status
        )
        if not has_read_pass_update:
            errors.append(
                f"scanner output must include a chapter_status entry for "
                f"chapter_uid={output.chapter_uid!r} with read_passes > 0"
            )
```

#### fixer

> [R1: addressed] **V3**：fixer 不允许直接提交 topology patch（`insert_node`、`delete_node`、`move_node`）；topology 变更必须通过 `PatchCommand` 提交（Phase 3 编译时验证），直接 `BookPatch` 只允许 `set_field` 和 `replace_node`。

```python
if output.kind == "fixer":
    topology_ops = {"insert_node", "delete_node", "move_node"}
    for i, patch in enumerate(output.patches):
        for j, change in enumerate(patch.changes):
            if change.op in topology_ops:
                errors.append(
                    f"fixer output patches[{i}].changes[{j}]: "
                    f"fixer may not submit topology changes directly via BookPatch "
                    f"({change.op!r}); use PatchCommand instead (enforced by Phase 3 compiler)"
                )
        if output.chapter_uid is not None and patch.scope.chapter_uid is None:
            errors.append(
                f"fixer output patches[{i}]: chapter-scoped fixer "
                f"may not use book-wide scope"
            )
```

> **设计说明**：与总体设计（agentic-improvement.md §3）保持一致——topology patch 只能由 supervisor 直接 BookPatch 提交，fixer 必须通过 PatchCommand macro（Phase 3 编译时才能包含 topology 操作）。Phase 2 在 PatchCommand 层面不做细粒度检查（`op` 是自由字符串），Phase 3 收紧。

#### reviewer

> [R1: addressed] **D4**：reviewer 权限更接近 scanner——只允许 `set_field`，不允许 `replace_node` 和所有 topology 操作。

```python
if output.kind == "reviewer":
    allowed_ops = {"set_field"}
    for i, patch in enumerate(output.patches):
        for j, change in enumerate(patch.changes):
            if change.op not in allowed_ops:
                errors.append(
                    f"reviewer output patches[{i}].changes[{j}]: "
                    f"reviewer may only submit set_field changes, got {change.op!r}"
                )
```

> **D4 设计决策**：reviewer 的角色以观察和标注为主，允许 `replace_node` 可以完全替换 block 内容，权限过大。Phase 2 将 reviewer 限制为仅 `set_field`，与 scanner 一致。如需修改，应升级为 supervisor 角色提交。

#### supervisor

- 无额外权限限制（supervisor 可提交任意合法 patch，包括 topology 操作）
- 但仍须通过所有 BookPatch validate 检查

### 5.8 OpenQuestion context_uid 存在性

`add-question` 时已实时检查，validate 阶段再做一次完整检查：

```python
for i, q in enumerate(output.open_questions):
    for uid in q.context_uids:
        if not _uid_exists(uid, book):
            errors.append(
                f"open_questions[{i}] (q_id={q.q_id}): context_uid not found: {uid}"
            )
```

### 5.9 _uid_exists 辅助函数

```python
def _uid_exists(uid: str, book: Book) -> bool:
    """Check if uid is a chapter uid or block uid in book."""
    for chapter in book.chapters:
        if chapter.uid == uid:
            return True
        for block in chapter.blocks:
            if block.uid == uid:
                return True
    return False
```

---

## 6. Submit --apply 详细流程

`run_agent_output_submit(work, output_id, apply, cfg)` 的 `--apply` 路径：

```
1. resolve_editor_paths(work)
2. ensure_initialized(paths)
3. load output file → AgentOutput
4. load book.json → Book
5. load memory.json → EditMemory
6. validate_agent_output(output, book) → errors
   ├─ errors 非空 → emit_json({valid: false, errors: [...]}) → exit 1（不修改任何文件）
   └─ errors 为空 → 继续
7. compile_commands(output.commands, book) → compiled_patches: list[BookPatch]
   ├─ Phase 2: commands 列表为空（或直接跳过），compiled_patches = []
   └─ Phase 3 后：每个 PatchCommand 编译为 BookPatch
8. all_patches = compiled_patches + output.patches
9. apply_patches_sequentially(all_patches, book) → (new_book, error)
   - 任一失败 → emit_json({error: "...", failed_at_patch: patch_id}) → exit 1
     （此时 book.json 未被修改，整体回滚保证）
10. apply_memory_patches_sequentially(output.memory_patches, memory, agent_id, now)
    → (new_memory, all_decisions)
    [R1: addressed] S5 — 按顺序逐个 merge，第 i+1 个 merge 的输入是第 i 个 merge 的输出
    - 若 merge_edit_memory 抛出异常 → emit_json({error: "memory merge failed: ..."}) → exit 1
      （此时 book.json 也未被写入，因为步骤 11 在本步骤之后）
11. atomic_write_model(paths.book_path, new_book)
    [R1: addressed] S3 — 使用 atomic_write_model（state.py 中已实现，写临时文件再 os.replace）
12. save_memory(paths, new_memory)
    （save_memory 内部也使用 atomic_write_model）
13. archive_agent_output(paths, output, submitted_at=now)
    [R1: addressed] S3/D3 — 见 §6.3，使用 atomic_write_text 写临时文件再 os.replace
14. emit_json({
      "submitted": true,
      "output_id": output.output_id,
      "patches_applied": len(all_patches),
      "memory_patches_applied": len(output.memory_patches),
      "archive_path": str(archive_path),
      "memory_decisions": [d.model_dump(mode="json") for d in all_decisions]
    })  # [R2: removed new_book_version/op_log_version]
15. exit 0
```

> **步骤顺序说明（T4 回滚安全性）**：book.json 写入（步骤 11）在 memory merge（步骤 10）完成之后、archive（步骤 13）之前。如果 memory merge 失败（步骤 10 抛异常），book.json 尚未被写入，系统保持一致状态。如果 book.json 写入后进程崩溃（步骤 11 完成但步骤 12/13 未执行），重启后 memory.json 与 book.json 会不一致——这是 Phase 2 的已知限制，在 §10 遗留问题中记录。Phase 7（Git worktree）引入事务语义后可解决。

### 6.1 apply_patches_sequentially

> [R1: addressed] **S2**：Phase 1 定义 `apply_book_patch(book: Book, patch: BookPatch) -> Book`，失败时抛出 `PatchError`，没有返回 result 对象。正确的包装方式：

```python
from epubforge.editor.patches import apply_book_patch, PatchError

def apply_patches_sequentially(
    patches: list[BookPatch],
    book: Book,
) -> tuple[Book, str | None]:
    """
    Returns (updated_book, error_message).
    error_message is None on success.
    If any patch fails, returns original book (unchanged) and error message.
    """
    current = book
    for patch in patches:
        try:
            current = apply_book_patch(current, patch)   # 参数顺序：(book, patch)
        except PatchError as e:
            return book, f"patch {patch.patch_id} failed: {e.reason}"
    return current, None
```

### 6.2 apply_memory_patches_sequentially

> [R1: addressed] **S5**：`AgentOutput.memory_patches` 是 `list[MemoryPatch]`（多个）。如果一个 output 包含多个 MemoryPatch，必须连续 merge，第 i+1 次 merge 的输入 memory 是第 i 次 merge 的输出。`merge_edit_memory` 实际签名为 `(memory, patch, *, updated_at, updated_by, question_id_factory=None) -> MemoryMergeResult`，返回 `.memory` 和 `.decisions`。

```python
from epubforge.editor.memory import merge_edit_memory, MemoryMergeDecision

def apply_memory_patches_sequentially(
    memory_patches: list[MemoryPatch],
    memory: EditMemory,
    *,
    agent_id: str,
    now: str,
) -> tuple[EditMemory, list[MemoryMergeDecision]]:
    """
    Apply each MemoryPatch to the evolving memory state.
    Returns (final_memory, all_decisions_accumulated).
    """
    current_memory = memory
    all_decisions: list[MemoryMergeDecision] = []
    for mp in memory_patches:
        result = merge_edit_memory(
            current_memory,
            mp,
            updated_at=now,
            updated_by=agent_id,
        )
        current_memory = result.memory
        all_decisions.extend(result.decisions)
    return current_memory, all_decisions
```

### 6.3 archive_agent_output [R2: removed op_log_version]

> [R1: addressed] **D3/S3**：归档使用写临时文件再 `os.replace` 的原子操作，而不是先写再删的非原子模式。这样即使进程在归档过程中崩溃，也只会留下一个完整文件（原文件或归档文件），不会出现两者同时存在的歧义状态。

```python
def archive_agent_output(paths: EditorPaths, output: AgentOutput, submitted_at: str) -> Path:
    submitted_compact = submitted_at.replace(":", "").replace("-", "").replace("T", "-").rstrip("Z")
    archive_name = f"{output.output_id}_{submitted_compact}.json"
    archive_path = paths.agent_outputs_archives_dir / archive_name
    paths.agent_outputs_archives_dir.mkdir(parents=True, exist_ok=True)
    # Read the current (in-progress) file content
    src = paths.agent_outputs_dir / f"{output.output_id}.json"
    content = src.read_text(encoding="utf-8")
    # Atomically write to archive (temp → os.replace), then remove source
    atomic_write_text(archive_path, content)
    src.unlink()
    return archive_path
```

`atomic_write_text`（已在 `state.py` 实现）使用 `uuid4` 命名临时文件 + `os.replace` 完成原子写入。归档写入成功后才执行 `src.unlink()`。如果 `atomic_write_text` 成功但 `src.unlink()` 失败（极端情况），`load_agent_output` 在加载时需检查对应 archives 目录是否已有同 `output_id` 的归档文件，若有则视为已提交，返回 `CommandError("output already submitted")`。

---

## 7. 与现有代码的集成

### 7.1 app.py 修改

```python
# 在现有 import 区域末尾添加
from epubforge.editor.agent_output_cli import agent_output_app

# 在 editor_app 初始化后添加（推荐放在所有 @editor_app.command 注册之后）
editor_app.add_typer(agent_output_app, name="agent-output")
```

最终 CLI 结构：

```
epubforge editor
  init
  doctor
  propose-op          (Phase 4 删除)
  apply-queue         (Phase 4 删除)
  acquire-lease       (Phase 4 删除)
  release-lease       (Phase 4 删除)
  acquire-book-lock   (Phase 4 删除)
  release-book-lock   (Phase 4 删除)
  run-script
  compact
  snapshot
  render-page
  vlm-page
  render-prompt
  agent-output        (Phase 2 新增)
    begin
    add-note
    add-question
    add-command
    add-patch
    add-memory-patch
    validate
    submit
```

### 7.2 state.py 修改要点

在 `EditorPaths` dataclass 中新增：

```python
agent_outputs_dir: Path
agent_outputs_archives_dir: Path
```

在 `resolve_editor_paths` 中新增：

```python
agent_outputs_dir=edit_state_dir / "agent_outputs",
agent_outputs_archives_dir=edit_state_dir / "agent_outputs" / "archives",
```

在 `ensure_initialized` 中**不**检查 `agent_outputs_dir`（目录按需创建，不是 init 的必备条件）。

### 7.3 tool_surface.py 修改要点

新增以下 8 个函数（签名示例）：

```python
def run_agent_output_begin(
    work: Path, kind: str, agent: str, chapter: str | None, cfg: Config
) -> int: ...

def run_agent_output_add_note(
    work: Path, output_id: str, text: str, cfg: Config
) -> int: ...

def run_agent_output_add_question(
    work: Path, output_id: str, question: str,
    context_uids: list[str], options: list[str], cfg: Config
) -> int: ...

def run_agent_output_add_command(
    work: Path, output_id: str, command_file: Path, cfg: Config
) -> int: ...

def run_agent_output_add_patch(
    work: Path, output_id: str, patch_file: Path, cfg: Config
) -> int: ...

def run_agent_output_add_memory_patch(
    work: Path, output_id: str, patch_file: Path, cfg: Config
) -> int: ...

def run_agent_output_validate(
    work: Path, output_id: str, cfg: Config
) -> int: ...

def run_agent_output_submit(
    work: Path, output_id: str, apply: bool, stage: bool, cfg: Config
) -> int: ...
```

所有函数内部调用 `resolve_editor_paths`、`ensure_initialized`，以 `CommandError` 报错，以 `emit_json` 输出，保持与现有命令完全一致的风格。

### 7.4 agent_output.py 的辅助函数

为了让 `tool_surface.py` 调用干净，`agent_output.py` 应暴露：

```python
def load_agent_output(paths: EditorPaths, output_id: str) -> AgentOutput:
    """Load and validate AgentOutput from disk. Raises CommandError if not found or already archived."""
    ...

def save_agent_output(paths: EditorPaths, output: AgentOutput) -> None:
    """Atomically save AgentOutput back to disk using atomic_write_model."""
    ...

def validate_agent_output(output: AgentOutput, book: Book) -> list[str]:
    """Full semantic validation. Returns list of error strings (empty = valid)."""
    ...

def submit_agent_output(
    output: AgentOutput,
    book: Book,
    memory: EditMemory,
    paths: EditorPaths,
    *,
    now: str,
) -> SubmitResult:
    """validate → compile → apply patches → apply memory → save book → save memory → archive."""
    ...
```

---

## 8. 测试计划

测试文件：`tests/test_agent_output.py`

### 8.1 AgentOutput 模型单元测试

| 测试名 | 验证内容 |
|---|---|
| `test_agent_output_valid_scanner` | 合法 scanner output，全字段通过 |
| `test_agent_output_valid_supervisor_no_chapter` | supervisor with chapter_uid=None 合法 |
| `test_agent_output_invalid_kind` | kind 非法值报 ValidationError |
| `test_agent_output_empty_agent_id` | agent_id 空字符串报错 |
| `test_agent_output_invalid_timestamps` | updated_at < created_at 报错 |
| `test_agent_output_extra_fields_forbidden` | extra 字段报 ValidationError（StrictModel） |
| `test_patch_command_valid` | PatchCommand 基本合法校验 |
| `test_patch_command_empty_op` | op 空字符串报错 |

### 8.2 begin 命令测试

| 测试名 | 验证内容 |
|---|---|
| `test_begin_creates_file` | begin 成功后 agent_outputs/<id>.json 存在 |
| `test_begin_returns_output_id_and_path` | stdout JSON 包含 output_id、path（不含 base_version）[R2: removed base_version/op_log_version] |
| `test_begin_invalid_kind` | --kind 非法 → exit 2 |
| `test_begin_missing_agent` | --agent 未传 → exit 2 |
| `test_begin_nonexistent_chapter` | --chapter 指定不存在的 UID → exit 1 |
| `test_begin_not_initialized` | editor 未 init → exit 1 |
| `test_begin_scanner_requires_chapter` | kind=scanner 且未指定 --chapter → exit 2 [R1: addressed] |

### 8.3 add-note 命令测试

| 测试名 | 验证内容 |
|---|---|
| `test_add_note_appends` | 多次 add-note 后 notes 列表增长 |
| `test_add_note_trims_whitespace` | leading/trailing 空格被 strip |
| `test_add_note_empty_text` | --text 空字符串 → exit 2 |
| `test_add_note_updates_updated_at` | updated_at 时间戳更新 |
| `test_add_note_nonexistent_output` | output_id 不存在 → exit 1 |
| `test_add_note_idempotent_append` | 相同文本添加两次结果为两条记录（append 语义，不去重）[R1: addressed] T2 |

### 8.4 add-question 命令测试

| 测试名 | 验证内容 |
|---|---|
| `test_add_question_basic` | 成功追加，stdout 包含 q_id |
| `test_add_question_with_context_uids` | 合法 context_uid 通过 |
| `test_add_question_invalid_context_uid` | context_uid 不存在于 book → exit 1 |
| `test_add_question_with_options` | options 列表正确存储 |
| `test_add_question_empty_question` | --question 空 → exit 2 |
| `test_add_question_asked_by_is_agent_id` | 构造的 OpenQuestion.asked_by 固定为 output.agent_id，不可指定 [R1: addressed] T7 |

### 8.5 add-command 命令测试

| 测试名 | 验证内容 |
|---|---|
| `test_add_command_valid_file` | 成功追加，stdout 包含 command_id |
| `test_add_command_missing_file` | --command-file 不存在 → exit 2 |
| `test_add_command_invalid_json` | 文件内容非 JSON → exit 1 |
| `test_add_command_schema_violation` | PatchCommand schema 不合法 → exit 1 |

### 8.6 add-patch 命令测试

| 测试名 | 验证内容 |
|---|---|
| `test_add_patch_valid` | 成功追加，stdout 包含 patch_id |
| `test_add_patch_missing_file` | → exit 2 |
| `test_add_patch_invalid_json` | → exit 1 |
| `test_add_patch_schema_violation` | BookPatch schema 不合法 → exit 1 |
| `test_add_patch_scope_mismatch` | patch.scope.chapter_uid != output.chapter_uid → exit 1 |

### 8.7 add-memory-patch 命令测试

| 测试名 | 验证内容 |
|---|---|
| `test_add_memory_patch_valid` | 成功追加 |
| `test_add_memory_patch_schema_violation` | MemoryPatch 不合法 → exit 1 |

### 8.8 validate 命令测试

| 测试名 | 验证内容 |
|---|---|
| `test_validate_clean_output` | 无 patches/notes 的干净 output → valid: true |
| `test_validate_invalid_chapter_uid` | output.chapter_uid 不存在 → valid: false |
| `test_validate_patch_uid_missing` | patch 引用不存在的 block UID → valid: false |
| `test_validate_scanner_topology_patch` | scanner 提交 insert_node → valid: false |
| `test_validate_reviewer_topology_patch` | reviewer 提交 delete_node → valid: false |
| `test_validate_reviewer_replace_node_rejected` | reviewer 提交 replace_node → valid: false [R1: addressed] D4 |
| `test_validate_supervisor_any_patch` | supervisor 提交 topology patch → valid: true |
| `test_validate_memory_patch_unknown_chapter` | memory_patch chapter_status uid 不存在 → valid: false |
| `test_validate_memory_patch_evidence_uid_missing` | memory_patch convention.evidence_uids 含不存在 UID → valid: false [R1: addressed] S4 |
| `test_validate_memory_patch_affected_uid_missing` | memory_patch pattern.affected_uids 含不存在 UID → valid: false [R1: addressed] S4 |
| `test_validate_multiple_errors` | 多处问题时 errors 列表完整收集，不 fail-fast |
| `test_validate_scanner_no_read_pass_update` | scanner output 无 read_passes 更新 → valid: false [R1: addressed] V1 |
| `test_validate_scanner_with_read_pass_update` | scanner output 有 read_passes > 0 的 chapter_status → valid: true [R1: addressed] V1 |
| `test_validate_fixer_direct_topology_patch_rejected` | fixer 直接提交 insert_node BookPatch → valid: false [R1: addressed] V3 |
| `test_validate_scope_chapter_none_is_book_wide` | PatchScope(chapter_uid=None) 被视为 book-wide，scanner 提交时报错 [R2: simplified] |

### 8.9 submit --apply 测试

| 测试名 | 验证内容 |
|---|---|
| `test_submit_dry_run_no_side_effects` | 不传 --apply，book.json 不变 |
| `test_submit_apply_empty_patches` | patches/commands 为空时 book.json 不变，版本不变，output 归档 |
| `test_submit_apply_with_set_field_patch` | 应用 set_field patch 后 book.json 字段更新 |
| `test_submit_apply_with_memory_patch` | memory.json 正确 merge |
| `test_submit_apply_validation_fail_no_side_effects` | validate 失败时 book.json、memory.json 均不变 |
| `test_submit_apply_archives_output` | 成功后 agent_outputs/<id>.json 被移至 archives/ |
| `test_submit_apply_patch_fail_rollback` | 第二个 patch apply 失败 → book.json 保持原状 |
| `test_submit_apply_multiple_memory_patches` | 两个 MemoryPatch 的连续 merge：第二次 merge 的输入是第一次 merge 的输出 [R1: addressed] S5 |
| `test_submit_apply_second_submit_fails` | output 已归档后再次 submit → exit 1，"output not found" [R1: addressed] T8 |
| `test_submit_stage_placeholder` | --stage 模式返回占位响应，不修改任何文件 [R1: addressed] V2 |

### 8.10 并发与边界测试

> [R1: addressed, R2: removed base_version/op_log_version] **T1/D7**：Phase 2 明确只支持串行工作模式。并发隔离由 Phase 7 Git worktree 负责；`test_concurrent_submit_base_version_conflict` 已移除（基于 base_version 的拒绝逻辑已删除）。字段级冲突由 BookPatch changes 中的 `old` 前置条件捕获。

| 测试名 | 验证内容 |
|---|---|
| `test_add_note_duplicate_appended_not_deduplicated` | 相同 note 添加两次，结果为两条记录（append 语义明确）[R1: addressed] T2 |
| `test_load_output_corrupted_json` | output JSON 被手动损坏 → CommandError with clear message [R1: addressed] T6 |
| `test_archive_target_already_exists` | 归档目标文件已存在 → 使用 atomic_write_text 覆盖（可接受），不报错 [R1: addressed] T5 |

### 8.11 测试 fixtures 规范

```python
# conftest.py 或 test_agent_output.py 顶部

import pytest
from pathlib import Path
from epubforge.editor.state import resolve_editor_paths
from epubforge.editor.tool_surface import run_init

@pytest.fixture
def initialized_work_dir(tmp_path, minimal_book_json):
    """Return an initialized work directory with a minimal book."""
    work = tmp_path / "testbook"
    work.mkdir()
    # Write minimal 05_semantic.json
    (work / "05_semantic.json").write_text(minimal_book_json, encoding="utf-8")
    run_init(work=work, cfg=mock_cfg())
    return work

@pytest.fixture
def minimal_book_json():
    """Return a minimal valid Book JSON with one chapter and one block."""
    ...
```

所有 CLI 命令测试通过直接调用 `tool_surface.run_*` 函数（绕过 Typer）来测试业务逻辑，
保持与现有测试风格（`test_editor_ops.py`、`test_editor_apply.py`）一致。

### 8.12 补充说明（evaluate 测试覆盖率）

> [R1: addressed] **T3**：Phase 2 添加一个 smoke test 验证大型 output 不会导致超时：
> `test_validate_large_output_smoke`：构造包含 50 个 patches 和 50 个 memory_patches 的 output，
> `validate_agent_output` 必须在 2 秒内完成（`_uid_exists` 是 O(n) 线性扫描，
> 50 patches × 50 changes × O(chapters × blocks) 的 UID 查找可能在大型 book 上变慢，
> 此处暂用 minimal book 验证基本性能，后续可改为预建 UID set 优化）。

---

## 9. 实施顺序建议

1. **`state.py`** — 新增 `agent_outputs_dir`、`agent_outputs_archives_dir` 字段（最小改动）
2. **`patch_commands.py`** — 定义 Phase 2 版本的 `PatchCommand` 占位模型
3. **`agent_output.py`** — 定义 `AgentOutput` 模型 + `load_agent_output`、`save_agent_output`、`validate_agent_output`
4. **`tool_surface.py`** — 实现 8 个 `run_agent_output_*` 函数（依赖上面三个）
5. **`agent_output_cli.py`** — Typer 命令组注册
6. **`app.py`** — `add_typer(agent_output_app, name="agent-output")`
7. **`tests/test_agent_output.py`** — 按 §8 逐步补充测试

---

## 10. 遗留问题与 Phase 3 接口

Phase 2 刻意留下以下"桩"，等待后续 Phase 填充：

| 项目 | 当前 Phase 2 状态 | 目标 Phase |
|---|---|---|
| `PatchCommand.op` | 自由字符串，不校验具体命令名 | Phase 3：`Literal["split_block", "merge_blocks", ...]` |
| `compile_commands` 函数 | 返回空列表（no-op） | Phase 3：实现每个 macro 的编译逻辑 |
| scanner 权限的"低风险命令"细化 | 只检查 set_field，有 TODO 注释 | Phase 3：细化 allowed op list |
| `PatchCommand` agent_id vs output agent_id | 只做警告 | Phase 3：可收紧为 error |
| `submit --stage` 模式 | 返回占位响应（见 §4.9） | Phase 4：旧 staging 系统迁移时正式实现 |
| `evidence_refs` 字段校验 | 不校验（TODO 注释） | Phase 9：VLM evidence 系统建立后补充 |
| 并发 submit 竞争窗口 | Phase 2 仅支持单 agent 串行工作 | Phase 7：Git worktree 模式引入隔离机制 |
| book/memory 写入事务一致性 | book 写入后进程崩溃可导致 memory 不一致（已知限制） | Phase 7：Git commit 作为事务边界解决 |
| `_uid_exists` 性能 | O(chapters × blocks) 线性扫描，大型 book 可能成为瓶颈 | 优化为预建 `set[str]` 查找表（可在 Phase 2 实现时顺手做） |

> [R1: addressed, R2: removed base_version/op_log_version] **D7**：Phase 2 明确只支持单 agent 串行工作模式。多 agent 并发隔离由 Phase 7 Git worktree 负责；字段级冲突由 BookPatch changes 中的 `old` 前置条件捕获，不依赖全局版本号。

---

## 参考：目录结构最终样貌

```
src/epubforge/editor/
  _validators.py            (不变)
  agent_output.py           (新增)
  agent_output_cli.py       (新增，CLI Typer 命令组)
  app.py                    (修改：注册 agent-output 子命令组)
  patch_commands.py         (新增：PatchCommand 模型)
  memory.py                 (不变)
  patches.py                (Phase 1 产出，不变)
  state.py                  (修改：新增 agent_outputs_dir 路径)
  tool_surface.py           (修改：新增 8 个 run_agent_output_* 函数)
  ...其余文件不变...

edit_state/                 (运行时产生)
  agent_outputs/
    <uuid>.json             (in-progress outputs)
    archives/
      <uuid>_<ts>.json      (submitted outputs)
  book.json
  memory.json
  ...
```
