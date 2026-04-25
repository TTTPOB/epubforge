# Phase 8-9 实施计划：VLM Evidence Tool 与 Stage 3 简化

> 状态：初始实施计划
> 对应主设计：`agentic-improvement.md` §16（Move VLM out of pipeline stage semantics）、Phase 8（VLM as editor evidence tool）、Phase 9（Simplify Stage 3）
> 前置条件：Phase 1-7 完成（BookPatch / AgentOutput / PatchCommand / Projection / Book diff / Git workspace 已可用）
> 下游依赖：Phase 10（Doctor task generation）

---

## 0. Plan-review loop policy / no human blocking

本计划必须支持无人值守的后续实现流程。实现 worker / reviewer **不得阻塞等待人类在线答疑**，也不得依赖任何 ask-human 工具或等价机制。

规则：

1. **不 ask human**：遇到设计不确定性时，不发起 ask-human；把问题写入本文件的 [Open questions register](#12-open-questions-register)。
2. **默认假设可执行**：每个开放问题必须同时记录：影响、默认假设、推荐决策、触发复核的条件。实现者按默认假设继续推进，除非代码事实或测试证明该默认不可行。
3. **多轮 plan-review 后仍未解决的问题必须保留**：如果经过多轮 plan-review loop 仍存在开放问题，不删除、不隐藏；将其标记为 `unresolved-after-review`，并保留默认实现路径与复核条件。
4. **计划文件是异步决策载体**：所有需要用户之后查看的设计点、风险、折中、默认选择和后续复核点都写在本计划中。
5. **实现期间的新发现回写计划**：如果实现时发现本计划与实际代码不一致，应先修订本计划的相应条目，再继续实现；不要在聊天中等待人类裁决。

---

## 1. 目标与非目标

### 1.1 目标

Phase 8-9 将 VLM 从 ingestion pipeline 分支降级为 editor evidence 工具，同时简化 Stage 3 pipeline 为唯一模式：

1. **VLMObservation schema**（Phase 8）：引入结构化的 VLM 观测模型 `VLMObservation` 和 `VLMFinding`，替代当前 `_VLMPageResult` 的临时结构。
2. **VLM 观测存储与索引**（Phase 8）：VLM 观测结果写入 `edit_state/vlm_observations/` 并维护 `vlm_observation_index.json`，可被 `AgentOutput.evidence_refs` 和 `BookPatch.evidence_refs` 引用。
3. **`vlm-page` 升级**（Phase 8）：返回 `observation_id`，存储 `VLMObservation`，接受可选的 `--chapter`/`--blocks` IR scope 参数。
4. **`vlm-range` 新命令**（Phase 8）：支持多页范围的 VLM 分析，自动拆分为逐页请求，汇总为一组 observations。
5. **evidence_refs 验证**（Phase 8）：`AgentOutput` 和 `BookPatch` 的 `evidence_refs` 在 validate 时检查 observation index 中是否存在。
6. **移除 pipeline VLM 分支**（Phase 9）：`pipeline.py` 的 `run_extract` 始终使用 skip-VLM 模式。
7. **移除/弃用 `extract.py`**（Phase 9）：VLM mode extractor 不再作为 pipeline 入口。
8. **简化配置**（Phase 9）：弃用 `ExtractSettings.skip_vlm` 设置（保留字段但忽略其值）。
9. **简化 Stage3Manifest mode**（Phase 9）：mode 字段值统一为 `"docling"`（从 `"skip_vlm"` 改名）。
10. **清理 CLI**（Phase 9）：移除 `--skip-vlm/--no-skip-vlm` CLI 选项。

### 1.2 非目标

Phase 8-9 不做：

- **不移除 VLM 能力**：VLM 仍作为 editor 工具存在（`vlm-page`、`vlm-range`），只从 ingestion pipeline 移除。
- **不修改 Book IR schema**：不新增字段到 `semantic.py` 的 `Block`/`Chapter`/`Book` 模型。`Provenance.evidence_ref` 已存在，保持不变。
- **不引入外部 Git 库**：与 Phase 7 保持一致。
- **不实现 VLM auto-fix**：VLM 只产出 observation，不自动生成 BookPatch。agent 根据 observation 决定是否产出修改。
- **不实现 VLM streaming**：逐页串行调用，不做异步/并行 VLM 请求。
- **不实现 display_handle**：UID 重设计属于未来工作。
- **不做旧 workdir VLM artifact 迁移**：包含 `mode: "vlm"` 的旧 artifact 需要重新运行 pipeline。
- **不实现 Doctor task 生成**：属于 Phase 10。
- **不做 Stage 3 artifact → extract artifact 的重命名**：虽然存在相关讨论（`epubforge-rdz` issue），但名称变更影响范围大（文件路径 `03_extract/`、模型名称 `Stage3Manifest`、日志等），超出 Phase 9 scope。记录在开放问题中作为后续工作。

---

## 2. 当前代码事实与约束

### 2.1 现有 VLM 调用路径

`src/epubforge/editor/tool_surface.py` 行 706-852 的 `run_vlm_page()` 是当前唯一的 editor VLM 入口：

1. 从 `edit_state/meta.json` 读取 `Stage3EditorMeta`。
2. 验证 page 在 `selected_pages` 中。
3. 渲染 PDF 页面为 JPEG（使用 `_render_pdf_page_image`，基于 pypdfium2）。
4. 从 `EvidenceIndex.pages[str(page)]` 加载该页的 evidence items。
5. 构建 VLM prompt：系统提示 + 用户消息（evidence JSON + base64 图片）。
6. 调用 `LLMClient(cfg, use_vlm=True).chat_parsed()` 获取 `_VLMPageResult`。
7. 将结果写入 `edit_state/audit/vlm_pages/page_NNNN.json`。
8. stdout 输出 `{"output_path": ..., "page": ...}`。

**关键问题**：
- `_VLMPageResult` 是一个局部定义的 BaseModel（`page`、`issues: list[str]`、`suggestions: list[str]`、`notes: str`），不是可引用的稳定 schema。
- 结果存储路径 `edit_state/audit/vlm_pages/` 没有 observation ID，没有索引，不可被 evidence_refs 引用。
- 不接受 IR scope（chapter_uid、block_uids）参数。
- 不记录 `image_sha256` 或 `prompt_sha256`。

### 2.2 Pipeline VLM 分支

`src/epubforge/pipeline.py` 行 258 和 315-345：

```python
mode = "skip_vlm" if cfg.extract.skip_vlm else "vlm"
# ...
if cfg.extract.skip_vlm:
    from epubforge.extract_skip_vlm import extract_skip_vlm
    result = extract_skip_vlm(...)
else:
    from epubforge.extract import extract
    result = extract(...)
```

两条路径产出相同的 `Stage3ExtractionResult`，但 VLM 路径需要 LLM/VLM API key，且运行时间长很多。

### 2.3 Config 中的 skip_vlm

`src/epubforge/config.py` 行 65：

```python
class ExtractSettings(BaseModel):
    vlm_dpi: int = 200
    skip_vlm: bool = False
    max_vlm_batch_pages: int = 4
    enable_book_memory: bool = True
    ocr: OcrSettings = Field(default_factory=OcrSettings)
```

还有 `_ENV_MAP` 中的 `EPUBFORGE_EXTRACT_SKIP_VLM` 环境变量映射和 `_settings_for_artifact()` 中的分支。

### 2.4 Stage3Manifest mode 字段

`src/epubforge/stage3_artifacts.py` 行 52：

```python
class Stage3Manifest(BaseModel):
    mode: Literal["vlm", "skip_vlm"]
    # ...
```

同样用于 `Stage3ExtractionResult.mode`、`EvidenceIndex.mode`、`ExtractionMetadata.stage3_mode`（含 `"unknown"`）。

### 2.5 evidence_refs 字段现状

- `AgentOutput.evidence_refs: list[str]`（`agent_output.py` 行 64）——已存在，带 TODO 注释："Phase 9 VLM evidence system will add validation here"。
- `BookPatch.evidence_refs: list[str]`（`patches.py` 行 212）——已存在，docstring 提到 "VLMObservation ids or other evidence refs"。
- `Provenance.evidence_ref: str | None`（`semantic.py` 行 78）——已存在于 IR Block 级别。
- 目前均无验证逻辑。

### 2.6 EditorPaths 与存储位置

`src/epubforge/editor/state.py` 中 `EditorPaths` 是 frozen dataclass：

```python
@dataclass(frozen=True)
class EditorPaths:
    work_dir: Path
    edit_state_dir: Path    # work_dir / "edit_state"
    book_path: Path         # edit_state / "book.json"
    audit_dir: Path         # edit_state / "audit"
    agent_outputs_dir: Path # edit_state / "agent_outputs"
    # ...
```

当前 VLM 结果存于 `edit_state/audit/vlm_pages/`。Phase 8 将新增 `edit_state/vlm_observations/` 目录和 `edit_state/vlm_observation_index.json` 文件。需要在 `EditorPaths` 中增加对应路径字段。

### 2.7 LLMClient 接口

`src/epubforge/llm/client.py` 行 118-143：

```python
class LLMClient:
    def __init__(self, cfg: Config, *, use_vlm: bool = False) -> None: ...
    def chat_parsed(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        response_format: type[T],
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> T: ...
```

`chat_parsed` 支持 Pydantic `response_format`，VLM 调用通过 `use_vlm=True` 切换到 VLM provider。结果有本地 cache（基于 messages + response_format 的 hash）。

### 2.8 CLI 注册结构

`src/epubforge/editor/app.py` 使用 Typer。当前注册模式：

```python
editor_app = typer.Typer(help="Editor subsystem commands", no_args_is_help=True)

@editor_app.command("vlm-page")
def _vlm_page_cmd(...): ...

@editor_app.command("render-page")
def _render_page_cmd(...): ...

# 子命令组
from epubforge.editor.agent_output_cli import agent_output_app
from epubforge.editor.workspace_cli import workspace_app
editor_app.add_typer(agent_output_app, name="agent-output")
editor_app.add_typer(workspace_app, name="workspace")
```

顶层 CLI（`src/epubforge/cli.py`）的 `run` 和 `extract` 命令有 `--skip-vlm/--no-skip-vlm` 选项。

### 2.9 Prompt 中对 VLM 工具的引用

`src/epubforge/editor/prompts.py` 行 124-127 在 render_prompt 中输出 agent 可用工具：

```python
"  # render whole page as JPEG (no LLM/VLM):",
f"  epubforge editor render-page {work_dir_abs} --page {example_page}",
"  # call VLM on a page (writes to edit_state/audit/vlm_pages/):",
f"  epubforge editor vlm-page {work_dir_abs} --page {example_page}",
```

Phase 8 需要更新这些引用以反映新的命令签名和输出路径。

---

## 3. 架构

### 3.1 Phase 8-9 在整体 agentic workflow 中的位置

```text
                 +-----------------------+
                 |   Ingestion Pipeline  |
                 +-----------+-----------+
                             |
                   Stage 3: Docling-only
                   (Phase 9: 移除 VLM 分支)
                             |
                             v
                 +-----------------------+
                 |  editor init          |
                 |  (Book IR + evidence) |
                 +-----------+-----------+
                             |
              +--------------+--------------+
              |                             |
    Agent worktree            Supervisor integration
    (Phase 7)                 (Phase 7)
              |
              v
    +-----------------------+
    | Agent 工具             |
    |                       |
    | - projection export   |  (Phase 5)
    | - agent-output begin  |  (Phase 2)
    | - vlm-page [升级]     |  (Phase 8)  ← NEW
    | - vlm-range [新增]    |  (Phase 8)  ← NEW
    | - diff-books          |  (Phase 6)
    +-----------------------+
              |
              v
    +-----------------------+
    | VLMObservation 存储    |
    | (Phase 8)              |
    |                       |
    | vlm_observations/     |
    |   <obs_id>.json       |
    | vlm_observation_      |
    |   index.json          |
    +-----------------------+
              |
              v
    evidence_refs 引用
    (AgentOutput / BookPatch)
```

### 3.2 VLM 数据流（Phase 8）

```text
Agent 调用 vlm-page/vlm-range
    |
    +-> 确定 scope (page, chapter_uid, block_uids)
    +-> 渲染 PDF 页面为 JPEG
    +-> 加载当前 Book IR 中的对应 blocks（如果指定了 scope）
    +-> 构建 VLM prompt（系统提示 + evidence + 图片 + IR context）
    +-> LLMClient(cfg, use_vlm=True).chat_parsed(..., response_format=VLMPageAnalysis)
    +-> 构建 VLMObservation
    |     - observation_id: uuid4
    |     - image_sha256: 图片文件的 sha256
    |     - prompt_sha256: 完整 prompt 的 sha256
    |     - findings: structured VLMFinding list
    |     - raw_text: VLM 原始文本（如果可用）
    +-> 写入 edit_state/vlm_observations/<observation_id>.json
    +-> 更新 edit_state/vlm_observation_index.json
    +-> stdout 输出 {observation_id, page, findings_count, ...}
```

### 3.3 Pipeline 简化数据流（Phase 9）

```text
Before Phase 9:
    cfg.extract.skip_vlm == False → extract.py (VLM mode)
    cfg.extract.skip_vlm == True  → extract_skip_vlm.py

After Phase 9:
    (skip_vlm setting ignored) → extract_skip_vlm.py (always)
    mode = "docling" (renamed from "skip_vlm")
```

---

## 4. API 设计

### 4.1 设计决策汇总

| ID | 决策 | 理由 |
|---|---|---|
| PD1 | VLM observation 存储路径为 `edit_state/vlm_observations/` 而非 `edit_state/audit/vlm_pages/` | 旧路径是临时实现，无 ID 索引，语义不清晰。新路径与 `agent_outputs/` 平级，表明这是可被引用的 evidence 而非纯审计日志 |
| PD2 | 旧 `edit_state/audit/vlm_pages/` 不做迁移，不删除现有文件 | 旧文件是不可引用的临时输出。新系统从零开始，旧文件保持原位作为历史记录但不被新代码读取 |
| PD3 | `VLMObservation.observation_id` 使用 UUID4 | 与 `AgentOutput.output_id`、`BookPatch.patch_id` 保持一致 |
| PD4 | VLM response format 使用 `VLMPageAnalysis`（新定义）而非 `_VLMPageResult` | 新 schema 支持结构化 findings（`VLMFinding`），比旧的 issues/suggestions/notes 更有表达力 |
| PD5 | `vlm-range` 内部拆分为逐页请求，不做多页合并的单次 VLM 调用 | 每页一张图片更清晰，VLM 对单页分析更稳定。逐页调用也允许复用 `vlm-page` 核心逻辑 |
| PD6 | evidence_refs 验证只检查 observation_id 是否存在于 index，不检查 observation 内容是否支持修改 | 语义关联（"这个 observation 是否真的支持这个修改"）属于 reviewer agent 的判断，不适合机器自动校验 |
| PD7 | Stage3Manifest.mode 从 `"skip_vlm"` 改名为 `"docling"` | `"skip_vlm"` 暗示存在一个主路径被跳过；`"docling"` 是对实际行为的准确描述 |
| PD8 | `extract.py` 不删除文件，只在 `pipeline.py` 中不再导入 | 保留文件作为参考代码；删除时机可选在后续清理批次 |
| PD9 | `ExtractSettings.skip_vlm` 字段保留但忽略 | 不立即删除 config 字段，避免破坏用户 TOML 配置文件。字段保留但 pipeline 不再读取其值。记录 deprecation 注释 |
| PD10 | VLM prompt 在 Phase 8 中增加 IR context（当前 Book 中指定 scope 的 blocks），使 VLM 不仅看到 stage3 evidence 还看到当前编辑状态 | VLM 应比较 "当前 Book IR 认为这个页面有什么" vs "图片上实际有什么" |
| PD11 | observation_index 使用单一 JSON 文件而非目录索引 | observation 数量不会很多（每页最多几个），单文件索引足够；也便于 Git 追踪变化 |
| PD12 | Stage 3 artifact 不重命名为 "extract artifact" | 见非目标第 10 点。影响范围过大（路径 `03_extract/`、`Stage3Manifest`、`Stage3ActivePointer` 等），与 Phase 9 的简化目标无关 |
| PD13 | 兼容旧 mode 值：Stage3Manifest.mode 接受 `"vlm" | "skip_vlm" | "docling"`，但新创建的 manifest 只使用 `"docling"` | 允许读取旧 manifest 不报错，但新写入统一为 `"docling"` |
| PD14 | 简化后 `_settings_for_artifact` 保留 `enable_book_memory: True` 默认值 | 不改变现有默认行为。虽然 VLM 从 pipeline 移除，但 `enable_book_memory` 控制 Docling 提取时的滚动记忆功能，与 VLM 无关 |

### 4.2 新增类型：VLM Evidence 模型

#### 4.2.1 位于 `src/epubforge/editor/vlm_evidence.py`（新文件）

```python
from __future__ import annotations

import hashlib
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from epubforge.editor._validators import (
    StrictModel,
    require_non_empty,
    validate_utc_iso_timestamp,
    validate_uuid4,
)


class VLMFinding(StrictModel):
    """Single structured finding from a VLM observation."""

    finding_type: Literal[
        "missing_block",       # VLM 看到图片上有但 IR 中缺失的内容
        "extra_block",         # IR 中有但图片上不存在的内容
        "text_mismatch",       # IR block 的文本与图片不一致
        "role_mismatch",       # block 的 role/kind 分类错误
        "layout_issue",        # 布局问题（分栏、阅读顺序等）
        "table_error",         # 表格结构或内容错误
        "footnote_error",      # 脚注匹配或内容错误
        "figure_issue",        # 图片/caption 问题
        "heading_issue",       # 标题层级或内容问题
        "quality_ok",          # VLM 确认当前状态正确
        "other",               # 其他问题
    ]
    severity: Literal["info", "warning", "error"]
    block_uids: list[str] = Field(default_factory=list)
    """Block UIDs this finding refers to (may be empty if the issue is about missing content)."""
    description: str
    """Human-readable description of the finding."""
    suggested_fix: str | None = None
    """Optional suggestion for how to fix this issue."""

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return require_non_empty(value, field_name="description")


class VLMPageAnalysis(StrictModel):
    """VLM response format for page-level analysis.

    This is the response_format passed to LLMClient.chat_parsed().
    It is parsed from VLM output, then converted to VLMObservation.
    """

    page: int
    findings: list[VLMFinding] = Field(default_factory=list)
    summary: str = ""
    """Brief overall assessment of extraction quality for this page."""


class VLMObservation(StrictModel):
    """Stored VLM observation with full provenance metadata.

    This is the evidence unit that can be referenced by
    AgentOutput.evidence_refs and BookPatch.evidence_refs.
    """

    observation_id: str
    """UUID4, unique identifier for this observation."""
    page: int
    """1-based page number analyzed."""
    chapter_uid: str | None = None
    """Chapter UID scope (if provided at invocation time)."""
    related_block_uids: list[str] = Field(default_factory=list)
    """Block UIDs in scope (if provided at invocation time)."""
    model: str
    """VLM model identifier used for this observation."""
    image_sha256: str
    """SHA-256 hex digest of the rendered page JPEG."""
    prompt_sha256: str
    """SHA-256 hex digest of the serialized prompt messages."""
    findings: list[VLMFinding] = Field(default_factory=list)
    """Structured findings from the VLM analysis."""
    raw_text: str | None = None
    """Raw VLM response text (if available)."""
    created_at: str
    """ISO-8601 UTC timestamp."""
    dpi: int = 200
    """DPI used for rendering the page image."""
    source_pdf: str = ""
    """Workdir-relative path to the source PDF."""

    @field_validator("observation_id")
    @classmethod
    def _validate_observation_id(cls, value: str) -> str:
        return validate_uuid4(value, field_name="observation_id")

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: str) -> str:
        return validate_utc_iso_timestamp(value, field_name="created_at")

    @field_validator("image_sha256", "prompt_sha256")
    @classmethod
    def _validate_sha256(cls, value: str, info) -> str:
        value = require_non_empty(value, field_name=info.field_name)
        if len(value) != 64:
            raise ValueError(
                f"{info.field_name} must be a 64-character hex SHA-256 digest"
            )
        return value


class VLMObservationIndexEntry(StrictModel):
    """Summary entry in the observation index for quick lookup."""

    observation_id: str
    page: int
    chapter_uid: str | None = None
    findings_count: int
    created_at: str
    model: str


class VLMObservationIndex(StrictModel):
    """Index mapping observation_id to metadata for quick lookup.

    Stored at edit_state/vlm_observation_index.json.
    """

    schema_version: int = 1
    entries: dict[str, VLMObservationIndexEntry] = Field(default_factory=dict)
    """observation_id -> VLMObservationIndexEntry"""
```

#### 4.2.2 辅助函数（同文件）

```python
from pathlib import Path
from epubforge.editor.state import EditorPaths, atomic_write_model, atomic_write_text
import json


def _generate_observation_id() -> str:
    """Generate a new UUID4 observation ID."""
    return str(uuid4())


def _compute_sha256_bytes(data: bytes) -> str:
    """Compute hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _compute_sha256_str(data: str) -> str:
    """Compute hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def observation_path(paths: EditorPaths, observation_id: str) -> Path:
    """Return the storage path for a single observation JSON."""
    return paths.vlm_observations_dir / f"{observation_id}.json"


def save_vlm_observation(paths: EditorPaths, obs: VLMObservation) -> Path:
    """Atomically write a VLMObservation and update the index.

    Returns the path of the written observation file.
    """
    paths.vlm_observations_dir.mkdir(parents=True, exist_ok=True)
    obs_path = observation_path(paths, obs.observation_id)
    atomic_write_model(obs_path, obs)

    # Update index
    index = load_vlm_observation_index(paths)
    index.entries[obs.observation_id] = VLMObservationIndexEntry(
        observation_id=obs.observation_id,
        page=obs.page,
        chapter_uid=obs.chapter_uid,
        findings_count=len(obs.findings),
        created_at=obs.created_at,
        model=obs.model,
    )
    atomic_write_model(paths.vlm_observation_index_path, index)

    return obs_path


def load_vlm_observation_index(paths: EditorPaths) -> VLMObservationIndex:
    """Load the observation index, returning empty index if not found."""
    if not paths.vlm_observation_index_path.exists():
        return VLMObservationIndex()
    return VLMObservationIndex.model_validate_json(
        paths.vlm_observation_index_path.read_text(encoding="utf-8")
    )


def load_vlm_observation(paths: EditorPaths, observation_id: str) -> VLMObservation:
    """Load a single VLMObservation by ID.

    Raises FileNotFoundError if observation does not exist.
    """
    obs_path = observation_path(paths, observation_id)
    if not obs_path.exists():
        raise FileNotFoundError(f"VLM observation not found: {observation_id}")
    return VLMObservation.model_validate_json(
        obs_path.read_text(encoding="utf-8")
    )


def validate_evidence_refs(
    evidence_refs: list[str],
    paths: EditorPaths,
) -> list[str]:
    """Validate that all evidence_refs exist in the observation index.

    Returns a list of error strings. Empty list means all refs are valid.
    """
    if not evidence_refs:
        return []

    index = load_vlm_observation_index(paths)
    errors: list[str] = []
    for ref in evidence_refs:
        if ref not in index.entries:
            errors.append(
                f"evidence_ref {ref!r} not found in VLM observation index"
            )
    return errors
```

### 4.3 EditorPaths 扩展

在 `src/epubforge/editor/state.py` 的 `EditorPaths` dataclass 中新增：

```python
@dataclass(frozen=True)
class EditorPaths:
    # ... existing fields ...
    vlm_observations_dir: Path        # edit_state / "vlm_observations"
    vlm_observation_index_path: Path  # edit_state / "vlm_observation_index.json"
```

在 `resolve_editor_paths()` 中添加对应路径计算：

```python
vlm_observations_dir=edit_state_dir / "vlm_observations",
vlm_observation_index_path=edit_state_dir / "vlm_observation_index.json",
```

### 4.4 run_vlm_page 升级签名

```python
def run_vlm_page(
    work: Path,
    page: int,
    dpi: int,
    out: Path | None,
    cfg: Config,
    *,
    chapter: str | None = None,    # NEW: optional chapter_uid scope
    blocks: list[str] | None = None,  # NEW: optional block_uids scope
) -> int:
    """Render a page, call VLM with IR context, store VLMObservation.

    Returns 0 on success.
    stdout emits JSON with observation_id and findings summary.
    """
    ...
```

### 4.5 新增 run_vlm_range 函数

```python
def run_vlm_range(
    work: Path,
    start_page: int,
    end_page: int,
    dpi: int,
    cfg: Config,
    *,
    chapter: str | None = None,
    blocks: list[str] | None = None,
) -> int:
    """Run VLM analysis on a range of pages [start_page, end_page] inclusive.

    Internally calls run_vlm_page for each page in the range.
    Returns 0 on success.
    stdout emits JSON with list of observation_ids and aggregate summary.
    """
    ...
```

### 4.6 evidence_refs 验证集成

在 `agent_output.py` 的 `_validate_agent_output_impl()` 中新增验证步骤：

```python
# Phase 8: evidence_refs validation
from epubforge.editor.vlm_evidence import validate_evidence_refs

# Validate output-level evidence_refs
if output.evidence_refs:
    ref_errors = validate_evidence_refs(output.evidence_refs, paths)
    errors.extend(ref_errors)

# Validate evidence_refs in each BookPatch
for i, patch in enumerate(output.patches):
    if patch.evidence_refs:
        ref_errors = validate_evidence_refs(patch.evidence_refs, paths)
        for err in ref_errors:
            errors.append(f"patches[{i}].{err}")
```

**注意**：这需要 `_validate_agent_output_impl` 接收 `paths` 参数。当前签名是 `(output, book)`。需要扩展为 `(output, book, paths)`，或者将 evidence_refs 验证放在 `validate_agent_output` / `submit_agent_output` 的公共 API 层面。

设计决策：将 `paths` 参数添加到 `_validate_agent_output_impl`。这是一个内部函数，修改签名不影响公共 API。`validate_agent_output` 公共 API 也需要接收 `paths` 参数（添加为可选参数，`None` 时跳过 evidence_refs 验证）。

### 4.7 Stage3Manifest mode 字段扩展

```python
class Stage3Manifest(BaseModel):
    mode: Literal["vlm", "skip_vlm", "docling"]
    # ...
```

同步更新 `Stage3ExtractionResult.mode`、`EvidenceIndex.mode`。

`ExtractionMetadata.stage3_mode` 扩展：

```python
class ExtractionMetadata(BaseModel):
    stage3_mode: Literal["vlm", "skip_vlm", "docling", "unknown"] = "unknown"
    # ...
```

`Stage3EditorMeta.mode` 扩展：

```python
class Stage3EditorMeta(BaseModel):
    mode: Literal["vlm", "skip_vlm", "docling", "unknown"]
    # ...
```

### 4.8 pipeline.py 简化

```python
def _settings_for_artifact(cfg: Config) -> dict[str, Any]:
    """Build the settings snapshot. Always docling mode."""
    return {
        "docling": True,
        "contract_version": 3,
        "vlm_dpi": None,
        "max_vlm_batch_pages": None,
        "enable_book_memory": True,
        "vlm_model": None,
        "vlm_base_url": None,
    }


def run_extract(...) -> None:
    # ...
    mode = "docling"  # always docling

    # ... artifact_id computation ...

    from epubforge.extract_skip_vlm import extract_skip_vlm

    log.info("Stage 3: extracting (Docling evidence draft)...")
    log.info("Stage 3: provider_required=%s", False)
    with stage_timer(log, "3 extract"):
        result = extract_skip_vlm(...)

    # ...
```

---

## 5. 文件布局

### 5.1 新增文件

| 文件 | 用途 |
|---|---|
| `src/epubforge/editor/vlm_evidence.py` | VLMObservation/VLMFinding 模型、存储/加载/索引/验证辅助函数 |
| `tests/editor/test_vlm_evidence.py` | VLM evidence 模型和存储的单元测试 |
| `tests/editor/test_vlm_evidence_cli.py` | vlm-page/vlm-range CLI 集成测试 |
| `tests/test_stage3_simplify.py` | Stage 3 简化后的 pipeline/config 测试 |

### 5.2 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/epubforge/editor/state.py` | `EditorPaths` 新增 `vlm_observations_dir`、`vlm_observation_index_path`；`resolve_editor_paths` 新增对应路径 |
| `src/epubforge/editor/tool_surface.py` | `run_vlm_page` 重写（接受 scope 参数、产出 VLMObservation、更新索引）；新增 `run_vlm_range`；移除 `_VLMPageResult` 局部类 |
| `src/epubforge/editor/app.py` | 更新 `vlm-page` 命令签名（新增 `--chapter`、`--blocks` 参数）；新增 `vlm-range` 命令 |
| `src/epubforge/editor/agent_output.py` | `_validate_agent_output_impl` 增加 `paths` 参数和 evidence_refs 验证；`validate_agent_output` 增加可选 `paths` 参数；`submit_agent_output` 传递 `paths` |
| `src/epubforge/editor/prompts.py` | 更新 VLM 工具引用，反映新命令签名和输出格式 |
| `src/epubforge/pipeline.py` | `run_extract` 移除 VLM 分支，始终调用 `extract_skip_vlm`；`_settings_for_artifact` 简化 |
| `src/epubforge/config.py` | `ExtractSettings.skip_vlm` 添加 deprecation 注释；`_ENV_MAP` 中 `EPUBFORGE_EXTRACT_SKIP_VLM` 保留但记录 deprecated |
| `src/epubforge/cli.py` | `run` 和 `extract` 命令的 `--skip-vlm/--no-skip-vlm` 选项记录 deprecated，接受但忽略 |
| `src/epubforge/stage3_artifacts.py` | `Stage3Manifest.mode` 扩展接受 `"docling"`；`Stage3ExtractionResult.mode` 同；`EvidenceIndex.mode` 同 |
| `src/epubforge/ir/semantic.py` | `ExtractionMetadata.stage3_mode` 扩展接受 `"docling"` |
| `src/epubforge/extract_skip_vlm.py` | `extract_skip_vlm` 返回的 `mode` 从 `"skip_vlm"` 改为 `"docling"` |
| `tests/test_agent_output.py` | 更新测试以支持 `paths` 参数 |

---

## 6. 详细设计

### 6.1 VLMObservation 模型详细设计（Phase 8A）

#### 6.1.1 VLMFinding 的 finding_type 设计

`finding_type` 枚举覆盖 VLM 在 page-level 分析中可能发现的所有问题类别：

| finding_type | 含义 | 典型场景 |
|---|---|---|
| `missing_block` | 图片上有内容但 IR 中缺失 | 漏提取的段落、脚注、图片说明 |
| `extra_block` | IR 中有但图片上不存在 | 错误拆分或重复提取的 block |
| `text_mismatch` | 文本内容不一致 | OCR 错误、截断、格式化差异 |
| `role_mismatch` | block 的 kind/role 分类错误 | heading 被分类为 paragraph，或反之 |
| `layout_issue` | 布局或阅读顺序问题 | 分栏页面的 block 排序错误 |
| `table_error` | 表格结构或内容错误 | 行/列错位、merged cell 丢失 |
| `footnote_error` | 脚注相关错误 | callout 不匹配、脚注文本错误 |
| `figure_issue` | 图片或 caption 问题 | caption 缺失、图片引用错误 |
| `heading_issue` | 标题层级或内容问题 | 层级错误、标题文本不完整 |
| `quality_ok` | 确认当前状态正确 | 无问题（用于明确记录 VLM 认为该页面/block 正确） |
| `other` | 其他问题 | 不属于上述类别的问题 |

`severity` 分三级：

| severity | 含义 | agent 响应建议 |
|---|---|---|
| `info` | 信息性发现 | 记录但不需要修改 |
| `warning` | 可能需要修改 | scanner/fixer 应审查 |
| `error` | 明确需要修改 | fixer 应修复 |

#### 6.1.2 VLMPageAnalysis 的 VLM prompt 设计

VLM prompt 由三部分组成：

**系统提示**（system prompt）：

```text
You are a PDF extraction quality reviewer. You compare a rendered page image
against the structured extraction results (Book IR) for that page.

For each issue you find, produce a VLMFinding with:
- finding_type: one of missing_block, extra_block, text_mismatch,
  role_mismatch, layout_issue, table_error, footnote_error,
  figure_issue, heading_issue, quality_ok, other
- severity: info, warning, or error
- block_uids: list of affected block UIDs (empty if about missing content)
- description: clear description of the issue
- suggested_fix: optional suggestion for how to fix

If the page extraction looks correct, produce a single quality_ok finding.
```

**用户消息**：

```text
Page {page} analysis.

Current Book IR blocks on this page:
{blocks_json}

Stage 3 evidence for this page:
{evidence_json}

Review the image and compare against the extraction. Report all findings.
```

加上 base64 图片。

`blocks_json` 是从当前 Book IR 中提取的该页（或指定 scope）的 blocks，序列化为精简 JSON（uid, kind, text[:200], role, page）。

`evidence_json` 是从 `EvidenceIndex.pages[str(page)]` 加载的 stage3 evidence items（与当前实现相同）。

#### 6.1.3 存储格式

每个 observation 写入独立 JSON 文件：

```text
edit_state/vlm_observations/
  <observation_id>.json     # 完整 VLMObservation JSON
```

索引文件：

```text
edit_state/vlm_observation_index.json   # VLMObservationIndex JSON
```

索引示例：

```json
{
  "schema_version": 1,
  "entries": {
    "550e8400-e29b-41d4-a716-446655440000": {
      "observation_id": "550e8400-e29b-41d4-a716-446655440000",
      "page": 12,
      "chapter_uid": "ch-001-a3f",
      "findings_count": 3,
      "created_at": "2026-04-25T12:00:00Z",
      "model": "google/gemini-flash-3"
    }
  }
}
```

### 6.2 run_vlm_page 重写详细设计（Phase 8B）

#### 6.2.1 完整流程

```text
Step 1: 解析路径和验证
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    meta = load_editor_meta(paths)
    验证 meta.stage3 不为 None
    验证 page 在 meta.stage3.selected_pages 中

Step 2: 加载 Book IR 和 scope 验证
    book = load_editable_book(paths)
    如果 chapter 提供了:
        验证 chapter_uid 存在于 book.chapters 中
        chapter_blocks = 该 chapter 中 provenance.page == page 的 blocks
    如果 blocks 提供了:
        验证每个 block_uid 存在于 book 中
        scope_blocks = 指定的 blocks
    否则:
        scope_blocks = 所有 provenance.page == page 的 blocks

Step 3: 渲染 PDF 页面
    source_pdf = paths.work_dir / meta.stage3.source_pdf
    验证 source_pdf 存在
    tmp_img_path = 临时 JPEG 文件
    _render_pdf_page_image(source_pdf, page, dpi, tmp_img_path)
    img_bytes = tmp_img_path.read_bytes()
    image_sha256 = sha256(img_bytes)

Step 4: 加载 evidence
    evidence_items = 从 EvidenceIndex 加载 page 的 evidence
    evidence_text = json.dumps(evidence_items)

Step 5: 构建 blocks context
    blocks_context = [{
        "uid": b.uid,
        "kind": b.kind,
        "text": b.text[:200] if hasattr(b, "text") else "",
        "role": getattr(b, "role", None),
        "page": b.provenance.page,
    } for b in scope_blocks]
    blocks_json = json.dumps(blocks_context)

Step 6: 构建 VLM prompt 并计算 hash
    messages = [system_msg, user_msg_with_image]
    prompt_text = json.dumps(messages, ensure_ascii=False)
    prompt_sha256 = sha256(prompt_text)

Step 7: 调用 VLM
    vlm_client = LLMClient(cfg, use_vlm=True)
    analysis = vlm_client.chat_parsed(messages, response_format=VLMPageAnalysis)

Step 8: 构建 VLMObservation
    obs = VLMObservation(
        observation_id=uuid4(),
        page=page,
        chapter_uid=chapter,
        related_block_uids=[b.uid for b in scope_blocks if b.uid],
        model=vlm_client.model,
        image_sha256=image_sha256,
        prompt_sha256=prompt_sha256,
        findings=analysis.findings,
        raw_text=analysis.summary,
        created_at=now_utc_iso(),
        dpi=dpi,
        source_pdf=meta.stage3.source_pdf,
    )

**注意**：VLM 返回的 `VLMFinding.block_uids` 可能包含幻觉 UID。在构建 `VLMObservation` 时，对每个 finding 的 `block_uids` 做过滤，只保留 `scope_blocks` 中实际存在的 UID（如果 scope_blocks 非空）。

Step 9: 存储
    obs_path = save_vlm_observation(paths, obs)

Step 10: 输出
    emit_json({
        "observation_id": obs.observation_id,
        "page": page,
        "output_path": str(obs_path),
        "findings_count": len(obs.findings),
        "findings_summary": {
            ft: count for ft, count in Counter(f.finding_type for f in obs.findings).items()
        },
        "model": vlm_client.model,
    })

Step 11: 清理临时文件
    tmp_img_path.unlink(missing_ok=True)
```

#### 6.2.2 向后兼容

`run_vlm_page` 的旧参数签名 `(work, page, dpi, out, cfg)` 仍然可用——`chapter` 和 `blocks` 默认为 `None`。当 `out` 参数非 None 时，仍在指定路径写入一个向后兼容的结果文件（同时也写入 VLMObservation 到标准路径）。

但行为变化：

- stdout JSON 输出增加 `observation_id` 字段。
- 写入 `edit_state/vlm_observations/<obs_id>.json` 而非仅 `edit_state/audit/vlm_pages/`。
- 不再写入 `edit_state/audit/vlm_pages/`（PD2）。

### 6.3 vlm-range 命令详细设计（Phase 8C）

#### 6.3.1 CLI 参数

```
epubforge editor vlm-range <work> --start-page N --end-page M [--dpi D] [--chapter UID] [--blocks UID1,UID2,...]
```

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | Work directory |
| `--start-page` | int | 是 | - | 起始页码（1-based，inclusive） |
| `--end-page` | int | 是 | - | 结束页码（1-based，inclusive） |
| `--dpi` | int | 否 | 200 | 渲染 DPI |
| `--chapter` | str | 否 | None | Chapter UID scope |
| `--blocks` | str (逗号分隔) | 否 | None | Block UIDs scope |

#### 6.3.2 实现逻辑

```python
def run_vlm_range(
    work: Path,
    start_page: int,
    end_page: int,
    dpi: int,
    cfg: Config,
    *,
    chapter: str | None = None,
    blocks: list[str] | None = None,
) -> int:
    paths = resolve_editor_paths(work)
    ensure_work_dir(paths)
    ensure_initialized(paths)
    meta = load_editor_meta(paths)

    if meta.stage3 is None:
        raise CommandError("edit_state/meta.json has no stage3 section.")

    if start_page > end_page:
        raise CommandError(f"start_page ({start_page}) > end_page ({end_page})")

    # Filter to pages actually in selected_pages
    pages_in_range = [
        p for p in range(start_page, end_page + 1)
        if p in set(meta.stage3.selected_pages)
    ]

    if not pages_in_range:
        raise CommandError(
            f"no selected pages in range [{start_page}, {end_page}]"
        )

    observation_ids: list[str] = []
    results: list[dict] = []

    for page in pages_in_range:
        # Reuse run_vlm_page core logic (extract to a shared function)
        obs = _run_vlm_page_core(
            paths=paths,
            page=page,
            dpi=dpi,
            cfg=cfg,
            chapter=chapter,
            blocks=blocks,
        )
        observation_ids.append(obs.observation_id)
        results.append({
            "observation_id": obs.observation_id,
            "page": page,
            "findings_count": len(obs.findings),
        })

    emit_json({
        "observation_ids": observation_ids,
        "pages_analyzed": len(pages_in_range),
        "total_findings": sum(r["findings_count"] for r in results),
        "per_page": results,
    })
    return 0
```

#### 6.3.3 内部重构

为了让 `run_vlm_page` 和 `run_vlm_range` 共享核心逻辑，将实际的 VLM 调用逻辑提取为内部函数：

```python
def _run_vlm_page_core(
    *,
    paths: EditorPaths,
    page: int,
    dpi: int,
    cfg: Config,
    chapter: str | None = None,
    blocks: list[str] | None = None,
) -> VLMObservation:
    """Core VLM page analysis logic shared by run_vlm_page and run_vlm_range.

    Returns the saved VLMObservation.
    """
    ...
```

### 6.4 evidence_refs 验证详细设计（Phase 8D）

#### 6.4.1 validate_agent_output 签名变更

```python
def validate_agent_output(
    output: AgentOutput,
    book: Book,
    paths: EditorPaths | None = None,
) -> list[str]:
    """Full semantic validation of an AgentOutput against the current Book.

    When paths is provided, also validates evidence_refs against
    VLM observation index. When paths is None, evidence_refs validation
    is skipped (for backward compatibility in tests that don't set up
    a full edit_state).
    """
    result = _validate_agent_output_impl(output, book, paths=paths)
    return result.errors
```

#### 6.4.2 _validate_agent_output_impl 变更

```python
def _validate_agent_output_impl(
    output: AgentOutput,
    book: Book,
    *,
    paths: EditorPaths | None = None,
) -> AgentOutputValidationResult:
    # ... existing validation ...

    # Phase 8: evidence_refs validation
    if paths is not None:
        from epubforge.editor.vlm_evidence import validate_evidence_refs

        # Output-level evidence_refs
        ref_errors = validate_evidence_refs(output.evidence_refs, paths)
        errors.extend(ref_errors)

        # Per-patch evidence_refs
        for i, patch in enumerate(output.patches):
            if patch.evidence_refs:
                patch_ref_errors = validate_evidence_refs(
                    patch.evidence_refs, paths
                )
                for err in patch_ref_errors:
                    errors.append(f"patches[{i}]: {err}")

        # Per-compiled-patch evidence_refs
        for i, patch in enumerate(compiled_patches):
            if patch.evidence_refs:
                patch_ref_errors = validate_evidence_refs(
                    patch.evidence_refs, paths
                )
                for err in patch_ref_errors:
                    errors.append(f"compiled_patches[{i}]: {err}")

    return AgentOutputValidationResult(...)
```

#### 6.4.3 submit_agent_output 变更

`submit_agent_output` 已经接收 `paths` 参数，只需传递给 `_validate_agent_output_impl`：

```python
def submit_agent_output(
    output: AgentOutput,
    book: Book,
    memory: EditMemory,
    paths: EditorPaths,
    *,
    now: str,
) -> SubmitResult:
    validation = _validate_agent_output_impl(output, book, paths=paths)
    # ...
```

### 6.5 Pipeline 简化详细设计（Phase 9A）

#### 6.5.1 pipeline.py 修改清单

1. **`_settings_for_artifact()`**：移除 `if cfg.extract.skip_vlm` 分支，始终返回 docling 配置。

2. **`run_extract()`**：
   - `mode = "docling"`（不再读取 `cfg.extract.skip_vlm`）。
   - 始终 `from epubforge.extract_skip_vlm import extract_skip_vlm`。
   - 移除 `from epubforge.extract import extract` 分支。
   - 移除 `cfg.require_llm()` 和 `cfg.require_vlm()` 调用（pipeline stage 3 不需要 VLM API）。
   - 更新日志消息：`"Stage 3: extracting (Docling evidence draft)..."`。

#### 6.5.2 extract_skip_vlm.py 修改清单

1. `extract_skip_vlm()` 返回的 `Stage3ExtractionResult.mode` 从 `"skip_vlm"` 改为 `"docling"`。
2. 内部的 evidence index `mode` 从 `"skip_vlm"` 改为 `"docling"`。
3. 函数名保持 `extract_skip_vlm` 不变（函数重命名是可选的清理工作，不在 Phase 9 scope）。

#### 6.5.3 stage3_artifacts.py 修改清单

1. `Stage3Manifest.mode: Literal["vlm", "skip_vlm", "docling"]`——扩展枚举值。
2. `Stage3ExtractionResult.mode`——同上。
3. `EvidenceIndex.mode`——同上。
4. `build_desired_stage3_manifest` 的 `mode` 参数——同上。

#### 6.5.4 config.py 修改清单

```python
class ExtractSettings(BaseModel):
    vlm_dpi: int = 200
    skip_vlm: bool = False  # DEPRECATED: pipeline always uses docling mode. Kept for config file compatibility.
    max_vlm_batch_pages: int = 4  # Only used by editor vlm-page/vlm-range, not by pipeline.
    enable_book_memory: bool = True
    ocr: OcrSettings = Field(default_factory=OcrSettings)
```

`vlm_dpi` 和 `max_vlm_batch_pages` 仍然有用：它们被 editor 的 `vlm-page` 使用。`skip_vlm` 变为 dead code，保留但不读取。

#### 6.5.5 cli.py 修改清单

`run` 和 `extract` 命令的 `--skip-vlm/--no-skip-vlm` 选项：

```python
skip_vlm: bool | None = typer.Option(
    None,
    "--skip-vlm/--no-skip-vlm",
    help="[DEPRECATED] Ignored — pipeline always uses Docling-derived extraction. "
    "VLM is available as an editor tool (epubforge editor vlm-page/vlm-range).",
),
```

接受但不传递给 config（忽略其值）。

#### 6.5.6 ir/semantic.py 修改清单

```python
class ExtractionMetadata(BaseModel):
    stage3_mode: Literal["vlm", "skip_vlm", "docling", "unknown"] = "unknown"
    # ...
```

#### 6.5.7 editor/state.py 修改清单

```python
class Stage3EditorMeta(BaseModel):
    mode: Literal["vlm", "skip_vlm", "docling", "unknown"]
    skipped_vlm: bool  # DEPRECATED: always True for new workdirs
    # ...
```

### 6.6 Prompt 更新（Phase 8E）

`src/epubforge/editor/prompts.py` 中 VLM 工具引用更新：

```python
lines = [
    "### Page inspection tools",
    "  # render whole page as JPEG (no LLM/VLM):",
    f"  epubforge editor render-page {work_dir_abs} --page {example_page}",
    "  # call VLM to analyze a page (produces a VLMObservation with observation_id):",
    f"  epubforge editor vlm-page {work_dir_abs} --page {example_page} --chapter {chapter_uid}",
    "  # call VLM on a range of pages:",
    f"  epubforge editor vlm-range {work_dir_abs} --start-page {start_page} --end-page {end_page}",
    "",
    "  VLM observations are stored in edit_state/vlm_observations/ and can be",
    "  referenced in evidence_refs fields of AgentOutput and BookPatch.",
]
```

---

## 7. CLI 命令设计

### 7.1 vlm-page 命令（升级）

```
epubforge editor vlm-page <work> --page N [--dpi D] [--chapter UID] [--blocks UID1,UID2,...] [--out PATH]
```

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | Work directory |
| `--page` | int | 是 | - | 1-based page number |
| `--dpi` | int | 否 | 200 | Render DPI |
| `--chapter` | str | 否 | None | Chapter UID scope for IR context |
| `--blocks` | str (逗号分隔) | 否 | None | Block UIDs scope for IR context |
| `--out` | Path | 否 | None | 自定义输出路径（默认 vlm_observations/\<id\>.json） |

**stdout JSON 输出**：

```json
{
  "observation_id": "550e8400-e29b-41d4-a716-446655440000",
  "page": 12,
  "output_path": "/abs/path/to/edit_state/vlm_observations/550e8400-....json",
  "findings_count": 3,
  "findings_summary": {
    "text_mismatch": 2,
    "missing_block": 1
  },
  "model": "google/gemini-flash-3"
}
```

**exit code**：

| 情况 | exit code |
|---|---|
| 成功 | 0 |
| 参数无效 | 2 |
| 运行时错误 | 1 |

### 7.2 vlm-range 命令（新增）

```
epubforge editor vlm-range <work> --start-page N --end-page M [--dpi D] [--chapter UID] [--blocks UID1,UID2,...]
```

| 参数 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `work` | Path (positional) | 是 | - | Work directory |
| `--start-page` | int | 是 | - | 起始页码（inclusive） |
| `--end-page` | int | 是 | - | 结束页码（inclusive） |
| `--dpi` | int | 否 | 200 | Render DPI |
| `--chapter` | str | 否 | None | Chapter UID scope |
| `--blocks` | str (逗号分隔) | 否 | None | Block UIDs scope |

**stdout JSON 输出**：

```json
{
  "observation_ids": ["550e8400-...", "660f9500-..."],
  "pages_analyzed": 3,
  "total_findings": 7,
  "per_page": [
    {"observation_id": "550e8400-...", "page": 12, "findings_count": 3},
    {"observation_id": "660f9500-...", "page": 13, "findings_count": 2},
    {"observation_id": "770a0600-...", "page": 14, "findings_count": 2}
  ]
}
```

### 7.3 CLI --skip-vlm 弃用

`run` 和 `extract` 命令：

```
epubforge run <pdf> [--skip-vlm]  # DEPRECATED: ignored, always docling mode
epubforge extract <pdf> [--skip-vlm]  # DEPRECATED: ignored, always docling mode
```

---

## 8. 测试

### 8.1 VLM Evidence 模型测试：`tests/editor/test_vlm_evidence.py`

| 用例 | 描述 |
|---|---|
| `test_vlm_finding_valid` | 构建合法 VLMFinding，验证 schema 通过 |
| `test_vlm_finding_empty_description_rejected` | 空 description 被拒绝 |
| `test_vlm_finding_invalid_type_rejected` | 非法 finding_type 被拒绝 |
| `test_vlm_page_analysis_valid` | 构建合法 VLMPageAnalysis |
| `test_vlm_page_analysis_empty_findings` | 空 findings 列表合法 |
| `test_vlm_observation_valid` | 构建合法 VLMObservation，验证所有字段 |
| `test_vlm_observation_invalid_sha256` | 非 64 字符的 sha256 被拒绝 |
| `test_vlm_observation_invalid_id` | 非 UUID4 的 observation_id 被拒绝 |
| `test_vlm_observation_invalid_timestamp` | 非 UTC ISO 时间戳被拒绝 |
| `test_save_and_load_observation` | 保存后加载，round-trip 验证 |
| `test_observation_index_update` | 保存两个 observation，验证 index 包含两个 entry |
| `test_observation_index_empty` | 无 index 文件时加载返回空 index |
| `test_validate_evidence_refs_valid` | 存在的 observation_id 验证通过 |
| `test_validate_evidence_refs_invalid` | 不存在的 ref 返回错误 |
| `test_validate_evidence_refs_empty` | 空 refs 列表返回空错误 |
| `test_validate_evidence_refs_mixed` | 部分存在部分不存在，只报告不存在的 |

### 8.2 VLM CLI 测试：`tests/editor/test_vlm_evidence_cli.py`

| 用例 | 描述 |
|---|---|
| `test_vlm_page_returns_observation_id` | 调用 vlm-page（mock VLM），验证 stdout 包含 observation_id |
| `test_vlm_page_stores_observation` | 验证 observation 文件写入 vlm_observations/ |
| `test_vlm_page_updates_index` | 验证 index 文件更新 |
| `test_vlm_page_with_chapter_scope` | 使用 --chapter 参数，验证 chapter_uid 写入 observation |
| `test_vlm_page_with_blocks_scope` | 使用 --blocks 参数，验证 related_block_uids 写入 |
| `test_vlm_page_invalid_page` | page <= 0，exit code 2 |
| `test_vlm_page_page_not_selected` | page 不在 selected_pages，exit code 1 |
| `test_vlm_page_invalid_chapter` | 不存在的 chapter_uid，exit code 1 |
| `test_vlm_range_basic` | 调用 vlm-range（mock VLM），验证多个 observation 创建 |
| `test_vlm_range_filters_to_selected` | range 中非 selected 的页面被跳过 |
| `test_vlm_range_invalid_range` | start > end，exit code 2 |
| `test_vlm_range_no_pages_in_range` | range 中无 selected pages，exit code 1 |

**注意**：VLM CLI 测试需要 mock `LLMClient.chat_parsed`，不做真实 VLM 调用。使用 `unittest.mock.patch` 替换 VLM 响应。

### 8.3 evidence_refs 验证测试

在 `tests/test_agent_output.py` 中新增：

| 用例 | 描述 |
|---|---|
| `test_validate_evidence_refs_in_agent_output` | AgentOutput.evidence_refs 包含存在的 ref，验证通过 |
| `test_validate_evidence_refs_not_found` | evidence_refs 包含不存在的 ref，返回错误 |
| `test_validate_evidence_refs_in_patches` | BookPatch.evidence_refs 验证 |
| `test_validate_evidence_refs_skipped_when_no_paths` | paths=None 时跳过 evidence_refs 验证（向后兼容） |
| `test_submit_validates_evidence_refs` | submit_agent_output 验证 evidence_refs |

### 8.4 Stage 3 简化测试：`tests/test_stage3_simplify.py`

| 用例 | 描述 |
|---|---|
| `test_pipeline_always_docling_mode` | 验证 run_extract 始终使用 docling mode（mock extract_skip_vlm） |
| `test_pipeline_ignores_skip_vlm_false` | `skip_vlm=False` 时仍使用 docling mode |
| `test_pipeline_ignores_skip_vlm_true` | `skip_vlm=True` 时仍使用 docling mode |
| `test_settings_for_artifact_no_branch` | `_settings_for_artifact` 不再有分支 |
| `test_manifest_mode_docling` | 新创建的 manifest mode 是 "docling" |
| `test_manifest_mode_accepts_old_values` | mode="vlm" 或 "skip_vlm" 的旧 manifest 仍可加载 |
| `test_extraction_metadata_stage3_mode_docling` | ExtractionMetadata 接受 "docling" |
| `test_cli_skip_vlm_deprecated` | --skip-vlm 选项被接受但忽略（不影响 mode） |

### 8.5 现有测试更新

| 文件 | 变更 |
|---|---|
| `tests/test_config_skip_vlm.py` | 更新预期行为：skip_vlm 配置仍可解析但不影响 pipeline |
| `tests/test_e2e_skip_vlm.py` | 更新预期 mode 值从 "skip_vlm" 到 "docling" |
| `tests/test_agent_output.py` | 涉及 evidence_refs 的测试增加 paths 参数 |
| `tests/test_editor_stage3.py` | 更新 mode 预期值 |

### 8.6 质量 gates

```bash
uv run pytest tests/editor/test_vlm_evidence.py
uv run pytest tests/editor/test_vlm_evidence_cli.py
uv run pytest tests/test_agent_output.py
uv run pytest tests/test_stage3_simplify.py
uv run pyrefly check
```

---

## 9. 分阶段实施任务

### Sub-phase 8A：VLMObservation 模型和存储

**依赖**：无（可独立开始）。

**任务**：

1. 新增 `src/epubforge/editor/vlm_evidence.py`：定义 `VLMFinding`、`VLMPageAnalysis`、`VLMObservation`、`VLMObservationIndexEntry`、`VLMObservationIndex`。
2. 实现辅助函数：`save_vlm_observation`、`load_vlm_observation_index`、`load_vlm_observation`、`validate_evidence_refs`。
3. 修改 `src/epubforge/editor/state.py`：`EditorPaths` 新增 `vlm_observations_dir` 和 `vlm_observation_index_path`；`resolve_editor_paths` 添加对应路径。
4. 测试：`tests/editor/test_vlm_evidence.py` 中的模型验证和存储/加载测试。

**验收**：

- 所有模型 schema 测试通过。
- `save_vlm_observation` + `load_vlm_observation` round-trip 通过。
- `validate_evidence_refs` 对存在和不存在的 ref 返回正确结果。
- EditorPaths 新路径正确解析。

### Sub-phase 8B：run_vlm_page 重写

**依赖**：8A（需要 VLMObservation 模型和存储）。

**任务**：

1. 在 `tool_surface.py` 中提取 `_run_vlm_page_core()` 内部函数。
2. 重写 `run_vlm_page()`，新增 `chapter` 和 `blocks` 参数。
3. 构建包含 IR context 的 VLM prompt。
4. 使用 `VLMPageAnalysis` 作为 response_format。
5. 构建 `VLMObservation` 并通过 `save_vlm_observation` 存储。
6. 移除 `_VLMPageResult` 局部类。
7. 更新 `app.py` 中 `vlm-page` 命令签名，新增 `--chapter` 和 `--blocks` 选项。
8. 测试：mock VLM 的 CLI 测试。

**验收**：

- `vlm-page` 命令返回 `observation_id`。
- observation 写入 `edit_state/vlm_observations/`。
- index 更新。
- `--chapter` 和 `--blocks` 参数正确过滤 IR context。
- 旧 `--out` 参数仍可工作（但写到标准位置）。

### Sub-phase 8C：vlm-range 命令

**依赖**：8B（需要 `_run_vlm_page_core`）。

**任务**：

1. 在 `tool_surface.py` 中实现 `run_vlm_range()`。
2. 在 `app.py` 中注册 `vlm-range` 命令。
3. 测试：mock VLM 的 CLI 测试。

**验收**：

- `vlm-range` 命令对每页产生一个 observation。
- 非 selected 的页面被跳过。
- stdout 输出包含所有 observation_ids 和汇总信息。

### Sub-phase 8D：evidence_refs 验证集成

**依赖**：8A（需要 `validate_evidence_refs`）。

**任务**：

1. 修改 `agent_output.py`：`_validate_agent_output_impl` 增加 `paths` 参数。
2. 修改 `validate_agent_output` 公共 API，增加可选 `paths` 参数。
3. 修改 `submit_agent_output`，传递 `paths` 给验证。
4. 修改 `stage_agent_output`，同上。
5. 更新 `tool_surface.py` 中的 `run_agent_output_validate` 和 `run_agent_output_submit` 函数，传递 `paths` 参数给 `validate_agent_output`。
6. 更新 `agent_output.py` 中的 TODO 注释：将 `Phase 9` 引用改为 `Phase 8`（或直接删除 TODO，替换为实际验证逻辑）。
7. 更新现有测试以适配新签名。
8. 新增 evidence_refs 验证测试。

**验收**：

- `validate_agent_output(output, book, paths=paths)` 正确验证 evidence_refs。
- `paths=None` 时跳过验证（向后兼容）。
- `submit_agent_output` 在 evidence_refs 无效时返回错误。
- 现有测试不因签名变更而失败。

### Sub-phase 8E：Prompt 更新

**依赖**：8B（需要 vlm-page 新签名）。

**任务**：

1. 更新 `prompts.py` 中的 VLM 工具引用。
2. 添加 `vlm-range` 命令的引用。
3. 添加 evidence_refs 使用说明。

**验收**：

- `render_prompt` 输出包含新命令格式。

### Sub-phase 9A：Pipeline 简化

**依赖**：无（可与 Phase 8 并行实施。Pipeline 简化与 editor VLM 工具系统独立）。

**任务**：

1. 修改 `pipeline.py`：`_settings_for_artifact` 移除分支；`run_extract` 移除 VLM 分支。
2. 修改 `extract_skip_vlm.py`：返回的 mode 改为 `"docling"`。
3. 修改 `stage3_artifacts.py`：mode Literal 扩展。
4. 修改 `ir/semantic.py`：`ExtractionMetadata.stage3_mode` 扩展。
5. 修改 `editor/state.py`：`Stage3EditorMeta.mode` 扩展。
6. 在 `Stage3EditorMeta.skipped_vlm` 字段上添加 deprecation 注释：`# DEPRECATED: always True for new workdirs`。
7. 测试：`tests/test_stage3_simplify.py`。

**验收**：

- `run_extract` 不再导入 `extract.py`。
- 新 artifact 的 mode 为 `"docling"`。
- 旧 artifact 的 mode `"vlm"` 和 `"skip_vlm"` 仍可加载。

### Sub-phase 9B：Config 和 CLI 清理

**依赖**：9A。

**任务**：

1. 修改 `config.py`：`skip_vlm` 字段添加 deprecation 注释。
2. 修改 `cli.py`：`--skip-vlm` 选项标记为 deprecated，接受但忽略。
3. 更新配置相关测试。

**验收**：

- `--skip-vlm` 选项不报错但不影响行为。
- 包含 `skip_vlm: true` 的 TOML 文件仍可加载。

### Sub-phase 9C：extract.py 标记弃用

**依赖**：9A。

**任务**：

1. 在 `extract.py` 文件头部添加 deprecation 注释：
   ```python
   """DEPRECATED: VLM pipeline extraction mode.
   
   This module is no longer used by the ingestion pipeline (Phase 9).
   VLM analysis is now available as an editor tool (vlm-page, vlm-range).
   This file is kept as reference code and may be removed in a future version.
   """
   ```
2. 验证无其他模块导入 `extract.py`（除了旧测试）。
3. 更新或移除依赖 VLM pipeline mode 的测试。

**验收**：

- `extract.py` 不被 pipeline 导入。
- pyrefly check 通过。
- 现有测试全部通过。

### 依赖关系图

```text
8A: VLMObservation 模型 ──────┐
                               ├──> 8B: vlm-page 重写 ──┬──> 8C: vlm-range
                               │                         └──> 8E: Prompt 更新
                               ├──> 8D: evidence_refs 验证
                               │
                               └──> 9A: Pipeline 简化
                                        │
                                    ┌────┴────┐
                                    v         v
                               9B: Config   9C: extract.py
                                   清理       标记弃用
```

---

## 10. 验收标准

### 10.1 必须满足

1. `VLMObservation` 模型通过 Pydantic schema 验证，所有字段有正确的 validator。
2. `save_vlm_observation` 将 observation 写入 `edit_state/vlm_observations/<id>.json` 并更新 index。
3. `load_vlm_observation` 可正确读取已保存的 observation。
4. `validate_evidence_refs` 对存在的 ref 返回空错误列表，对不存在的 ref 返回错误。
5. `vlm-page` 命令返回 `observation_id`，结果可被 evidence_refs 引用。
6. `vlm-page --chapter --blocks` 参数正确过滤 IR context 到 VLM prompt。
7. `vlm-range` 命令对每页产生独立 observation。
8. `AgentOutput.evidence_refs` 在 validate/submit 时被检查（当 `paths` 参数提供时）。
9. `BookPatch.evidence_refs` 在 validate/submit 时被检查（当 `paths` 参数提供时）。
10. `run_extract` 不再有 VLM 分支，始终使用 docling 模式。
11. 新创建的 `Stage3Manifest.mode` 为 `"docling"`。
12. 旧 manifest（mode `"vlm"` 或 `"skip_vlm"`）仍可加载不报错。
13. `--skip-vlm` CLI 选项被接受但忽略。
14. 所有测试通过。
15. `pyrefly check` 通过。

### 10.2 应满足

1. VLM prompt 包含当前 Book IR blocks 的 context（不仅是 stage3 evidence）。
2. `vlm-range` 跳过不在 selected_pages 中的页面并报告跳过原因。
3. observation index 在并发写入时不损坏（atomic write）。
4. 错误消息包含可操作的诊断信息（如 "observation_id XXX not found in index, available: [...]"）。
5. `extract.py` 标注 DEPRECATED 注释。

### 10.3 可延后

1. VLM observation 的 GC/cleanup 机制（当观测过多时）。
2. VLM observation 与 projection export 的集成（在 projection 中标注哪些 blocks 有 VLM 观测）。
3. VLM observation 的 diff/比较功能（同一页面两次 VLM 分析的差异）。
4. `extract.py` 文件的物理删除。
5. Stage 3 artifact → extract artifact 的全面重命名。
6. VLM 并行调用优化。
7. VLM prompt 的进一步优化（如只包含当前 scope 的 blocks 而非全页 blocks）。

---

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| VLM 不稳定地遵循 VLMPageAnalysis schema | `chat_parsed` 返回解析错误 | `chat_parsed` 已有 retry 机制（`max_retries=2`）；VLMPageAnalysis 的 findings 字段有默认值，VLM 可返回空列表 |
| VLM response format 对不同 VLM provider 表现不同 | 某些 provider 可能不支持 structured output | VLMPageAnalysis 保持扁平结构（list of findings），不使用深层嵌套；如果 structured output 不可用，降级到文本解析（defer 到实测后决定是否需要） |
| observation_index.json 并发写入损坏 | 两个 vlm-page 命令同时运行时可能竞争 | `atomic_write_model` 使用 temp file + os.replace，是原子的。但两个进程同时读-修改-写 index 可能丢失一个的更新。缓解：Phase 8 scope 中单个 agent 串行调用 VLM，不做并行；如果未来需要并行，改用 file lock 或每个 observation 一个 index 条目 |
| mode 从 "skip_vlm" 改为 "docling" 导致旧 artifact 无法匹配 | `active_manifest_matches_desired` 返回 false，触发重新提取 | 预期行为：旧 workdir 需要重新运行 pipeline。这是明确的非目标（不做迁移）。在 Phase 9 发布说明中注明 |
| 移除 VLM pipeline 分支后，某些依赖 VLM mode 的测试失败 | 测试红灯 | 9C 中更新或移除这些测试 |
| `_validate_agent_output_impl` 签名变更破坏调用者 | 其他调用 validate/submit 的地方未传递 paths | `paths` 参数可选，默认 None 时跳过验证。所有直接调用者（`agent_output_cli.py` 中的命令）已有 `paths` 可用 |
| `extract.py` 中可能有被其他模块引用的工具函数 | 删除 import 路径导致 ImportError | Phase 9C 在标记弃用前扫描所有 import，确保无其他模块依赖 |
| VLM prompt 中的 blocks context 在大页面上可能很长 | 超出 VLM context window | 限制每个 block 的 text 截断到 200 字符；如果 blocks 数量过多（>50），只发送前 50 个并添加 truncation 提示 |
| evidence_refs 验证需要读取 index 文件 | 在没有 edit_state 的测试环境中失败 | `paths=None` 时跳过验证；测试明确提供或不提供 paths |

---

## 12. Open questions register

> 状态说明：`default-proceed` 表示无需等待人类，按默认假设实现；`unresolved-after-review` 表示经过多轮 plan-review 仍未关闭，仍按默认假设实现并保留复核点。

| ID | 问题 | 影响 | 默认假设 / 实现路径 | 推荐决策 | 何时复核 | 状态 |
|---|---|---|---|---|---|---|
| OQ-01 | VLM observation 是否应存储在 `edit_state/vlm_observations/` 还是 `edit_state/evidence/vlm_observations/` | 主设计提到 `edit_state/evidence/vlm_observations/` 但也提到 `edit_state/audit/vlm_pages/` | 使用 `edit_state/vlm_observations/`——与 `edit_state/agent_outputs/` 平级，简洁明了。未来如果有其他 evidence 类型（非 VLM），再考虑 `evidence/` 子目录 | `edit_state/vlm_observations/` | 如果 Phase 10 引入其他 evidence 类型，考虑统一到 `evidence/` 目录 | default-proceed |
| OQ-02 | vlm-page 是否应同时保留旧的 `audit/vlm_pages/` 输出 | 可能有脚本依赖旧路径 | 不再写入旧路径（PD2）。旧文件保留不删除。新代码只读写 `vlm_observations/` | 完全切换到新路径 | 如果发现有外部脚本依赖旧路径，添加兼容写入 | default-proceed |
| OQ-03 | VLMPageAnalysis 的 response_format 是否足以让所有 VLM provider 理解 | provider 差异可能导致解析失败 | Phase 8 第一版只支持 structured output 模式（OpenAI compatible）。如果某 provider 不支持，降级到 text + regex 解析是 Phase 8 之后的工作 | 先只支持 structured output | 如果测试发现主要 provider 无法生成合法 VLMPageAnalysis，添加 fallback 解析 | default-proceed |
| OQ-04 | evidence_refs 验证是否应在 validate 和 submit 两个路径都执行 | validate 是只读检查，submit 是写入操作 | 两个路径都执行验证。validate_agent_output 有 `paths` 参数；submit_agent_output 也传递 paths。保持一致性 | 两个路径都验证 | - | default-proceed |
| OQ-05 | Stage 3 artifact 的 `03_extract/` 目录名是否应改为 `03_docling/` | 主设计提到 rename 但标记为 non-goal | 不改名（PD12）。`03_extract` 是对功能的准确描述（extract = 提取），不依赖于具体模式 | 保持 `03_extract` | 如果未来决定做全面重命名，作为独立 batch 处理 | default-proceed |
| OQ-06 | `extract_skip_vlm.py` 是否应重命名为 `extract_docling.py` | 文件名中的 "skip_vlm" 暗示存在一个被跳过的主路径 | 不改名。文件名变更的 Git blame 成本较高，且所有 import 路径都要更新。添加模块 docstring 说明即可 | 保持 `extract_skip_vlm.py`，添加 docstring | 如果做全面重命名，一起处理 | default-proceed |
| OQ-07 | VLM observation 中的 block_uids 是否需要验证存在于 Book 中 | block_uids 可能由 VLM 返回，VLM 可能 hallucinate UID | findings 中的 block_uids 由调用者（`_run_vlm_page_core`）从实际 Book IR 中填充，不由 VLM 生成。VLM 的 findings 中的 block_uids 只包含 scope 内的 blocks。因此 block_uids 总是合法的 | block_uids 由代码填充，不由 VLM 生成 | - | default-proceed |
| OQ-08 | `validate_agent_output` 公共 API 增加 `paths` 参数是否破坏调用者 | 外部代码可能直接调用 validate_agent_output | `paths` 默认为 `None`，不影响现有调用者。只有需要 evidence_refs 验证的调用者才传递 paths | 可选参数，默认 None | - | default-proceed |
| OQ-09 | 是否需要 migration 脚本来更新旧 workdir 的 mode 值 | 旧 workdir 的 Stage3Manifest.mode 是 "skip_vlm" 或 "vlm" | 不需要 migration。旧 workdir 重新运行 pipeline 即可。Stage3Manifest.mode 扩展为接受旧值（PD13）确保读取不报错 | 不做 migration | - | default-proceed |
| OQ-10 | `_settings_for_artifact` 简化后，旧 artifact 的 artifact_id 会变化吗 | artifact_id 基于 settings 的 canonical JSON 计算。settings 结构变化会导致不同 artifact_id | 是的，新 settings 会产生不同的 artifact_id。这是预期行为：旧 workdir 需要重新提取。`active_manifest_matches_desired` 会返回 false，触发重新提取 | 预期行为，不做兼容 | - | default-proceed |
| OQ-11 | VLM prompt 中的 blocks context 应包含哪些字段 | 太多字段浪费 token，太少字段 VLM 缺乏上下文 | 每个 block 包含：uid, kind, text[:200], role (if paragraph), page, callout (if footnote), level (if heading)。不包含 provenance, bbox, display_lines 等 | 精简字段集 | 实测后根据 VLM 表现调整 | default-proceed |

---

## 13. 参考

- 主计划：`.refactor-planning/agentic-improvement/agentic-improvement.md`
- Phase 7 计划：`.refactor-planning/agentic-improvement/phase7-git-workspace.md`
- Phase 6 Book diff：`.refactor-planning/agentic-improvement/phase6-book-diff.md`
- Semantic IR：`src/epubforge/ir/semantic.py`
- BookPatch / apply：`src/epubforge/editor/patches.py`
- AgentOutput：`src/epubforge/editor/agent_output.py`
- Tool surface（VLM 实现）：`src/epubforge/editor/tool_surface.py`
- CLI 注册：`src/epubforge/editor/app.py`
- Stage 3 artifacts：`src/epubforge/stage3_artifacts.py`
- Pipeline：`src/epubforge/pipeline.py`
- Config：`src/epubforge/config.py`
- VLM extractor（将弃用）：`src/epubforge/extract.py`
- Skip-VLM extractor：`src/epubforge/extract_skip_vlm.py`
- Editor state/paths：`src/epubforge/editor/state.py`
- LLM client：`src/epubforge/llm/client.py`
- Prompts：`src/epubforge/editor/prompts.py`
- Validators：`src/epubforge/editor/_validators.py`