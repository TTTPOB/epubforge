# Exploration Reports — Refactoring Planning Round

这是 5 个并行 sonnet 探索 agent 返回的原始报告，作为 planning/review 阶段的唯一输入来源。

Orchestrator 已决定：
- 用户授权 break 兼容性（zxgb fixture 已完成 build，无历史包袱）
- 目标是产出详细的重构计划文档（不是立即执行）
- 计划中需要人决策的问题直接写在计划里（不用 ask-user 工具，用户稍后回来看）

---

## Report 1: 配置 / CLI / 环境变量

### 清单

**配置文件（3 个，均 TOML）**

| 文件 | 用途 |
|---|---|
| `config.example.toml` | 模板，展示所有可用字段，含注释，不应直接使用 |
| `config.local.toml` | 本地实际使用的配置（gitignore 外，含真实 api_key） |
| `pyproject.toml` | 构建/开发依赖，不含运行时配置值 |

`config.local.toml` 包含 `config.example.toml` 没有的字段：`[footnote_verify]`（`thinking_budget_tokens`、`model`、`providers`）和 `[vlm].extra_body.reasoning.*`，以及 `llm.max_tokens`。

**CLI 入口点（2 个，不同解析框架）**

- `epubforge`（`src/epubforge/cli.py`）: Typer；子命令 `run/parse/classify/extract/assemble/build`；全局选项 `--config/-c`、`--log-level/-L`、`--log-file`；参数命名 kebab-case。
- `python -m epubforge.editor.<cmd>`（`src/epubforge/editor/tool_surface.py`）: argparse（通过自定义 `JsonArgumentParser`，`editor/cli_support.py:30`）；命令 `init/import-legacy/doctor/propose-op/apply-queue/acquire-lease/release-lease/acquire-book-lock/release-book-lock/run-script/compact/snapshot/render-prompt`；kebab-case 为主但部分 dest 用 snake_case。

**环境变量（全部通过 `os.environ.get`）**

- `EPUBFORGE_LLM_*` / `EPUBFORGE_VLM_*`（`BASE_URL/API_KEY/MODEL/TIMEOUT/MAX_TOKENS/PROMPT_CACHING`）：`config.py:135–158`
- `EPUBFORGE_CONCURRENCY` / `_CACHE_DIR`：`config.py:159–162`
- `EPUBFORGE_EDITOR_LEASE_TTL_SECONDS` / `_COMPACT_THRESHOLD` / `_MAX_LOOPS`：`config.py:163–168`
- `EPUBFORGE_VLM_DPI` / `_MAX_SIMPLE_BATCH_PAGES` / `_MAX_COMPLEX_BATCH_PAGES` / `_ENABLE_BOOK_MEMORY`：`config.py:169–176`
- `EPUBFORGE_LOG_LEVEL`：`cli.py:43`（**绕过 `config.py`**）
- `EPUBFORGE_EDITOR_NOW`：`editor/scratch.py:28`（测试用）
- `EPUBFORGE_PROJECT_ROOT` / `_WORK_DIR` / `_EDIT_STATE_DIR`：`editor/scratch.py:99–101`（仅在子进程中注入，不读回）

### 解析方式矩阵（摘要）

优先级已实现为：`env > config.local.toml > config.toml > dataclass default`，顺序在 `config.py:60–184`。但 `log_level`、`editor_lease_ttl_seconds` 的 CLI 默认值脱离 `Config` 独立硬编码。`[proofread]`、`[footnote_verify]` TOML 节存在但**完全不被 `config.py` 读取**（死配置）。

### 发现的问题

**A. `editor_lease_ttl_seconds` 默认值三处重复**
- `config.py:37`（`1800`）
- `editor/tool_surface.py:337`（`--ttl default=1800`）
- `editor/leases.py:106`（`ttl: int = 1800`）
- `acquire-lease` CLI 不读 `Config`，用户改 TOML 无效。
- `book_exclusive` 的 `300` 同样重复：`tool_surface.py:378`、`leases.py:146`。

**B. `EPUBFORGE_LOG_LEVEL` 在 `Config` 之外单独读取（ad-hoc）**
- `cli.py:43` 直接 `os.environ.get`，无 TOML 支持，无类型声明。

**C. `[proofread]` 和 `[footnote_verify]` TOML 节死配置**
- `config.local.toml` 中定义但 `config.py:load_config` 不读取。

**D. `EPUBFORGE_EDITOR_NOW` / `_PROJECT_ROOT` / `_WORK_DIR` / `_EDIT_STATE_DIR` 游离于 `Config` 之外**
- `editor/scratch.py:28,99–101` 裸 `os.environ.get`，也不在 AGENTS.md。

**E. `vlm_max_tokens` 默认值硬编码在 `LLMClient.__init__` 中**
- `llm/client.py:123`：`if use_vlm and self.max_tokens is None: self.max_tokens = 16384`。该默认值对 `Config` 层不可见。

**F. 命名不对称——TOML key 与 `Config` field 映射**

| TOML | `Config` field |
|---|---|
| `[llm].timeout_seconds` | `llm_timeout` |
| `[runtime].concurrency` | `concurrency` |
| `[editor].lease_ttl_seconds` | `editor_lease_ttl_seconds` |
| `[extract].vlm_dpi` | `vlm_dpi` |

### Gap

`Config` dataclass 用手写 40+ 行 if 链完成 TOML→field 映射，缺少声明式 schema。stage-specific 配置（`[proofread]`、`[footnote_verify]`）走 ad-hoc 路线没进统一模型。方向：迁移到 pydantic-settings BaseSettings + 嵌套子模型（`ProofreadConfig`、`FootnoteVerifyConfig`），三层合并自动完成。

---

## Report 2: LLM / VLM 调用链路

### 调用链路图

```
CLI (cli.py: run/extract/...)
  └→ pipeline.py: run_extract()
       └→ extract.py: extract()
            ├── LLMClient(cfg, use_vlm=True)   ← 客户端创建，仅此一处
            └── _process_vlm_unit()
                 ├── 构造 messages: [system=VLM_SYSTEM, user=[text+image_url blocks]]
                 └── client.chat_parsed(messages, response_format=VLMGroupOutput, temperature=0)
                      ├── _cache_key() → disk lookup (work/.cache/<2-hex>/<sha256>.json)
                      ├── _apply_cache_control() → system message 插 cache_control:ephemeral
                      └── _call_parsed()
                           ├── completions.parse() [OpenAI structured outputs]
                           │     BadRequestError 400 → _call_json_object_fallback()
                           ├── finish_reason=="length" → 重试 max_tokens×2 (最多3次)
                           └── 返回 _CallResult → UsageTracker.record_miss() → disk cache write
```

观测入口：`observability.py: UsageTracker`（进程级单例），`stage_timer` 包裹每个阶段。

### 调用点清单

| 文件:行 | 用途 | 封装 |
|---|---|---|
| `extract.py:79` | 创建 VLM 客户端实例（唯一调用点） | `LLMClient(cfg, use_vlm=True)` |
| `extract.py:273` | VLM 逐单元处理（主循环） | `client.chat_parsed(..., response_format=VLMGroupOutput)` |
| `llm/client.py:216` | 结构化输出主路径 | `_client.chat.completions.parse()` |
| `llm/client.py:295` | json_object 降级路径 | `_client.chat.completions.create()` |

**当前生产代码中实际 LLM/VLM 调用点只有 1 个**（`extract.py:273`），`use_vlm=False` 从未被调用。

### 问题清单

**P1 — 大量死代码**
- `llm/prompts.py:160` — `CLEAN_SYSTEM` 定义完整，但 `src/` 中无任何 import
- `ir/semantic.py:199-209` — `CleanBlock` / `CleanOutput` 未被使用
- `ir/semantic.py:186-194` — `TocRefineItem` / `TocRefineOutput` 未被使用
- AGENTS.md 描述的 stage 5（refine-toc）、stage 6（proofread）在 `pipeline.py` 和 `cli.py` 中完全缺失

**P2 — `_call_parsed` 与 `_call_json_object_fallback` 之间的 usage 读取重复**
- `llm/client.py:250-253`（`_call_parsed`）和 `llm/client.py:316-319`（`_call_json_object_fallback`）是逐字相同的 5 行 usage 解包代码（含 `getattr` 防御）
- "budget 翻倍重试" 逻辑也在两处独立实现（`_call_parsed:235-242` vs `_call_json_object_fallback:300-307`）

**P3 — VLM 的 `max_tokens` 硬编码兜底**
- `llm/client.py:122-123`：VLM 路径在 `cfg.vlm_max_tokens is None` 时强制 `self.max_tokens = 16384`；LLM 路径没有，不对称且隐藏硬编码

**P4 — `LLMClient` 构造函数中大量 `if use_vlm else` 分支**
- `llm/client.py:117-127`：7 条 `cfg.vlm_X if use_vlm else cfg.llm_X` 折叠，`__init__` 里手工分支等效于 2 个独立配置对象

**P5 — 命名不一致**
- 外部参数 `use_vlm: bool`，内部字段 `self._kind = "VLM"|"LLM"`，配置 key `llm_model`/`vlm_model`，无统一 "profile" 概念

**P6 — `_apply_cache_control` 只处理 system message 的首个 text block**
- `llm/client.py:65-80`：若将来有多轮 few-shot，不会被缓存。目前 VLM 调用只有一条 system，暂无实际问题

**P7 — `editor/prompts.py` 模板是纯 f-string / `str.format()` 占位符，缺少静态检查**
- `editor/prompts.py:12-61`：`SCANNER_PROMPT`/`FIXER_PROMPT`/`REVIEWER_PROMPT` 运行时 `KeyError`

### Gap

客户端配置应归为 `ClientProfile` dataclass 由 factory 选取，`use_vlm` 布尔消失。usage 解包和 budget 翻倍重试应提取为共享辅助。死代码（`CLEAN_SYSTEM` 等）要么补上调用方要么删除。

---

## Report 3: EditOps 实现链路

### EditOps 全链路图

```
[LLM subagent 输出]
    │  JSON array: {ops: [OpEnvelope...], memory_patches: [...], ...}
    ↓
[propose-op.py] → run_propose_op()
    │  stdin JSON 解析 → OpEnvelope.model_validate() (Pydantic 校验)
    │  all-or-nothing batch 校验：任意一条失败 → 全批次拒绝
    ↓
[staging.jsonl]   edit_state/staging.jsonl（JSONL，追加）
    ↓
[apply-queue.py] → run_apply_queue()
    │  逐 envelope 调用 apply_envelope()
    │    1. 重复 op_id 检查
    │    2. base_version 检查
    │    3. CompactMarker / RevertOp 特殊分支
    │    4. _ensure_lease_access() 租约校验
    │    5. _check_preconditions()
    │    6. _check_new_uid_collisions()
    │    7. _apply_op()  — isinstance if/elif 链 (16 个分支)
    │    8. book.version += 1
    │    9. env.memory_patches → merge_edit_memory()
    │  每条 envelope 立即持久化 (apply one → write one，非事务批处理)
    ↓
[edit_state/book.json]        Book Pydantic v2 模型，版本号单调递增
[edit_state/edit_log.jsonl]   已接受 envelope 的 JSONL 追加日志
[edit_state/memory.json]      EditMemory（Pydantic v2），随 memory_patches 更新
[edit_state/edit_log.rejected.jsonl]  拒绝日志
```

Op 来源：全部由 LLM subagent 生成（scanner / fixer / reviewer），通过 `render_prompt()` 注入 book.version 和 memory 快照后，LLM 输出 JSON。不存在人工或规则计算生成的路径。

### Op 类型清单（18 种）

| Op 名称 | 定义位置 | 校验位置 | apply 处理位置 |
|---|---|---|---|
| `set_role` | ops.py:373 | field_validator + ALLOWED_ROLES | apply.py:482 |
| `set_style_class` | ops.py:392 | field_validator + regex | apply.py:487 |
| `set_text` | ops.py:408 | field_validator | apply.py:492 |
| `set_heading_level` | ops.py:420 | Literal[1,2,3] | apply.py:497 |
| `set_heading_id` | ops.py:431 | field_validator | apply.py:504 |
| `set_footnote_flag` | ops.py:449 | model_validator | apply.py:511 |
| `merge_blocks` | ops.py:469 | field_validator + model_validator | apply.py:523 |
| `split_block` | ops.py:496 | model_validator | apply.py:542 |
| `delete_block` | ops.py:557 | field_validator | apply.py:559 |
| `insert_block` | ops.py:567 | model_validator → BLOCK_PAYLOAD_MODELS | apply.py:565 |
| `pair_footnote` / `unpair_footnote` / `relink_footnote` / `mark_orphan` | ops.py:590 (FootnoteOp) | model_validator | apply.py:577 |
| `merge_chapters` | ops.py:652 | model_validator | apply.py:597 |
| `split_chapter` | ops.py:683 | model_validator | apply.py:623 |
| `relocate_block` | ops.py:702 | field_validator | apply.py:637 |
| `split_merged_table` | ops.py:743 | model_validator | apply.py:653 |
| `noop` | ops.py:716 | Literal | apply.py:479（跳过） |
| `compact_marker` | ops.py:721 | field_validator | apply.py:1070（特殊） |
| `revert` | ops.py:733 | field_validator（UUID4） | apply.py:1077（特殊） |

### 问题清单

**1. `_cjk_join` 逻辑重复，且 `apply.py` 的 `_join_text("cjk")` 是空实现**
- `assembler.py:601` 有完整 CJK/kana/hangul/hyphen 逻辑
- `apply.py:196-201` 的 `_join_text` 接受 `"cjk"` 但 `return "".join(parts)`——**等同于 concat**，CJK 语义丢失
- `merge_blocks` 用 `join="cjk"` 时行为与 `"concat"` 相同

**2. 校验函数三/四重复制**
- `_require_non_empty`、`_validate_utc_iso_timestamp`、`_validate_uuid4` 在 `editor/ops.py:35-63`、`editor/memory.py:36-65`、`editor/doctor.py`、`editor/leases.py:11-17` 各一份

**3. `_apply_op` 是巨型 if/elif 链，无 dispatch 机制**
- `apply.py:478-688`，16 个 `isinstance` 分支
- 新增 op（如 `split_merged_table`）需要在四处同时加分支：`_apply_op`、`_check_new_uid_collisions`、`_resolve_intra_chapter_uid`、`_target_effect_preconditions`

**4. `memory_patches` 的事务语义依赖隐含的 deep copy**
- apply 是在 `working = book.model_copy(deep=True)` 上（`apply.py:1065`）
- `merge_edit_memory` 失败会导致 envelope 被拒绝，但这一安全性靠深拷贝理解，代码无注释说明

**5. `staging.jsonl` 的 all-or-nothing 仅在 propose-op 阶段，apply-queue 逐条处理**
- `run_propose_op`（tool_surface.py:243）：batch 全通过才写 staging
- `run_apply_queue`（tool_surface.py:292-320）：逐 envelope 处理
- 两阶段语义不统一，LLM 提交批次可能部分应用部分拒绝

**6. 命名不一致**
- `edit`/`op`/`patch`/`envelope` 四词同层混用（`EditOp`、`edit_log`、`OpEnvelope`、`apply_envelope`、`MemoryPatch`、`memory_patches`）
- `FootnoteOp` 聚合四种操作（pair/unpair/relink/mark_orphan），其他操作各自独立类——两种建模风格并存

**7. `TableMergeRecord` provenance 不完整**
- `TableMergeRecord.segment_html` 包含原始 HTML，但 constituent uids 刻意省略（ir/semantic.py:120-122）
- `split_merged_table` 分配 `str(uuid4())` 随机 uid，非确定性，replay 不稳定

**8. `Book.version` 语义模糊**
- `Book.version: int = 0` 实际是操作日志版本（每 op +1），不是 IR schema 版本
- 没有独立 schema 迁移机制；`io.py:_normalize_legacy_payload` 用 `setdefault` 做向前兼容，无版本号守卫

### Gap

1. CJK join 提取到共享 `text_utils.py`，供 `apply._join_text` 和 `assembler._cjk_join` 共用
2. `_apply_op` 用注册表（`dict[str, Callable]` 或单分派）替换 16 分支 if/elif
3. apply-queue 提供可选 batch atomicity，或文档明确"逐条独立"是设计选择
4. 三/四份 validator 合并到 `editor/_validators.py`

---

## Report 4: 横向代码质量扫描

### 目录结构鸟瞰

| 包/模块 | 职责 |
|---|---|
| `epubforge/` 顶层 | Pipeline 入口（`cli.py`, `pipeline.py`）、IO 工具（`io.py`）、配置（`config.py`）、可观测性（`observability.py`）、工具函数（`fields.py`, `markers.py`, `query.py`） |
| `ir/` | Semantic IR Pydantic 模型：`semantic.py`、`book_memory.py`、`style_registry.py` |
| `parser/` | Docling PDF 解析器（单文件包） |
| `llm/` | LLM/VLM 客户端 + 缓存 + 系统提示 |
| `audit/` | 七个独立 checker 模块，共享 `models.py` |
| `editor/` | 完整的 agentic 编辑子系统：ops/apply/log/state/leases/memory/doctor/tool_surface + 大量 CLI 入口脚本 |

划分总体合理，无 `utils/`/`helpers/` 杂物桶，`audit/` 和 `editor/` 已独立。

### Top 问题

**严重**

1. **`_require_non_empty` / `_validate_uuid4` / `_validate_utc_iso_timestamp` 在 4 个文件里独立实现**
   - `editor/ops.py:35`、`editor/memory.py:36`、`editor/doctor.py:20`、`editor/leases.py:11`（`_require_non_empty`）
   - `_validate_uuid4`、`_validate_utc_iso_timestamp` 在 ops.py / memory.py 各一份
   - 最大内聚问题，应抽到 `editor/_validators.py`

2. **HTML 表格 regex 在两个 audit 模块重复**
   - `audit/tables.py:13-16` 和 `audit/table_merge.py:20-23` 同名几乎一致（`CELL_RE` 细节略异）
   - 迁移到 `audit/models.py` 或 `audit/_html.py`

**中等**

3. **pipeline/extract 绕过 `io.py` 直接操作文件**
   - `assembler.py:42,105`、`extract.py:63,109,133,144,373` 裸 `json.loads/dumps` + `read_text/write_text`
   - `editor/log.py:190-191` `.write_text`（非 `atomic_write_*`），同包其他地方却用 `atomic_write_model`

4. **测试中 `_prov()` 辅助在 10 个测试文件里各自复制**
   - `test_editor_ops.py:11`、`test_architecture_migration.py:18`、`test_epub_builder.py:14`、`test_audit_detectors.py:15`、`test_ir_semantic.py:33`、`test_editor_log.py:22`、`test_editor_tool_surface.py:28`、`test_foundations_helpers.py:22`、`test_editor_apply.py:35`、`test_audit_table_merge.py:9`
   - 无 `conftest.py`

5. **`pipeline.py` 混用 `console.print`（rich）和 `logging`**
   - 阶段日志用 `console.print`（`pipeline.py:23,57,60,...,127`）
   - 其余所有模块一律 `logging`

**轻度**

6. **`dict[str, Any]` 广泛用于 `extract.py` 内部数据结构**
   - `extract.py` 多处裸 `dict[str, Any]`，`_AnchorItem(TypedDict)` 是正确做法但其他 "unit" 没有对应 TypedDict

7. **`pillow` 在 `pyproject.toml` 声明但源码无 import**
   - 全局 grep 无结果；Docling/fitz 可能间接用，作为直接依赖声明是噪声

### 命名风格不一致

**`editor/` 子包混用 snake_case 和 kebab-case**
- snake_case：`cli_support.py`, `tool_surface.py`, `apply.py`, `leases.py`, `memory.py`
- kebab-case（非法 Python 模块名，只能作脚本）：`acquire-book-lock.py`, `apply-queue.py`, `import-legacy.py`, `propose-op.py`, `release-book-lock.py`, `release-lease.py`, `render-prompt.py`, `run-script.py`, `acquire-lease.py`

**base class 命名不一致**
- `EditorModel`（ops.py:75）、`MemoryModel`（memory.py:32）、`DoctorModel`（doctor.py:16）、`LeaseModel`（leases.py:41）——四个等价 `extra="forbid"` base，应统一为 `StrictModel`

**`load_*` vs `read_*`**
- `state.py:173`：`load_editable_book`/`load_editor_memory`/`load_lease_state`
- `state.py:196`：`read_staging`（同包同性质却用 `read_`）

### 可能的死代码 / 过度抽象

- `pipeline.py` Stage 8 标注：实际是第 7 条管道，注释与 AGENTS.md 不一致
- `ir/semantic.py` `TocRefineItem`/`TocRefineOutput`、`CleanBlock`/`CleanOutput`——这些 LLM 响应模型只被内部用但挂在 IR 包
- `EditorModel`/`MemoryModel`/`DoctorModel`/`LeaseModel` 四个假基类

### 总结

整体代码质量**良好但局部有明显技术债**。架构分层清晰、Pydantic v2 完善、全面 type hint、logging 统一这些都好。主要问题：`editor/` 内部重复（4 份 validator）和测试层 fixture 缺失（10 份 `_prov`）。

**如果只能选 3 件事：**
1. `editor/_validators.py` + 统一 `StrictModel`
2. `tests/conftest.py` + `_prov` / 基础 IR fixture
3. `audit/_html.py` 共享 HTML regex

---

## Report 5: AGENTS.md 过时审计

### 清单

| 路径 | 存在 |
|---|---|
| `/home/tpob/playground/epubforge/AGENTS.md` | 存在（唯一） |
| 任何子目录 AGENTS.md / CLAUDE.md | 无 |

### 过时条目

**2.1 Pipeline 阶段表与 CLI 实际不符**
- AGENTS.md 声称七阶段（stages 1–7），CLI `--from` 限 `1–4`，已注册命令只有 `parse/classify/extract/assemble/build`
- `refine-toc`（stage 5）和 `proofread`（stage 6）**在 CLI 和 pipeline.py 中根本不存在**
- `06_proofread.json` 仅在 `io.py:13` 作为旧遗留常量
- `build` 被注释标为 "Stage 8"（`cli.py:151`），但 AGENTS.md 表格写 stage 7
- 证据：`cli.py:84`（`--from max=4`）、`cli.py:151`、`pipeline.py:1`

**2.2 Pipeline 表第 5、6、7 行完全失效**
- Stage 5 (refine-toc → `05_semantic.json`)、stage 6 (proofread → `06_proofread.json`)、stage 7 (build → `out/<name>.epub`)
- 实际：`refine_toc`/`proofread` 模块文件不存在，`build` 对应 Stage 8，直接读 `edit_state/book.json` 或 `05_semantic.json`（`epub_builder.py:93–103`）

**2.3 Semantic IR 描述未提及新字段**
- 原文（行 49–51）：`Book → Chapter → Block[...]`，`Table` 无特殊字段
- 实际：`Table` 新增 `multi_page: bool`、`merge_record: TableMergeRecord | None`（`semantic.py:139–148`），`TableMergeRecord` 新类
- commit `5ec1c29` 引入

**2.4 `VLMPageOutput` 描述遗漏 `updated_book_memory`**
- 原文（行 51）未提
- 实际：`VLMPageOutput` 含 `updated_book_memory: BookMemory`（`semantic.py:282`），LLM prompts 要求每次返回（`llm/prompts.py:380`）

### 缺失条目

**3.1 Editor 子系统（整个模块缺失）**
- commits `3529ebd`→`19c6f3a` 引入完整 `editor/` 子系统：`apply_envelope`、`OpEnvelope`、`split_merged_table`、`memory_patches`（`ops.py:751,799,810`）、lease、doctor、audit detectors、scratch sandbox、snapshot
- AGENTS.md 零描述

**3.2 `audit/` 子系统缺失**
- commit `761ad23` 新增 `audit/`（`detect_structure_issues`/`detect_table_merge_issues`/`detect_footnote_issues`/`detect_dash_inventory`）
- AGENTS.md 零提

**3.3 `BookMemory` 机制缺失**
- `ir/book_memory.py` 用于 extract 阶段跨单元积累书本事实
- 可通过 `EPUBFORGE_ENABLE_BOOK_MEMORY=0` 禁用
- AGENTS.md 零提

**3.4 未文档化的 env vars**
- `EPUBFORGE_LLM_TIMEOUT` / `EPUBFORGE_VLM_TIMEOUT`
- `EPUBFORGE_LLM_MAX_TOKENS` / `EPUBFORGE_VLM_MAX_TOKENS`
- `EPUBFORGE_ENABLE_BOOK_MEMORY`
- `EPUBFORGE_EDITOR_LEASE_TTL_SECONDS` / `_COMPACT_THRESHOLD` / `_MAX_LOOPS`

**3.5 `_cjk_join` 覆盖扩展**
- commit `4ed3179` 扩展到 kana/hangul/fullwidth/hyphen continuation（`assembler.py:577–601`）
- AGENTS.md 无

**3.6 `book.json` 持久化责任转移**
- commit `ae00d09`：`write_initial_state` 不再写 `book.json`，调用方负责（`state.py:246`）
- AGENTS.md 未记录 `edit_state/` 目录结构

### 模糊 / 误导条目

**4.1 `--from` 范围误导**
- AGENTS.md 暗示支持 1–7，CLI 实际 `min=1, max=4`（`cli.py:84`）

**4.2 "Gemini 2.5/3" 表述**
- 原文行 41 写 "Gemini 2.5/3"，默认 VLM 是 `google/gemini-flash-3`（`config.py:24`），"2.5/3" 混淆版本

**4.3 `[llm] prompt_caching = false`**
- 原文行 43–44 仅提 `[llm]`，实际 `[vlm]` 也有独立的 `prompt_caching`（`config.py:101–102`）和 `EPUBFORGE_VLM_PROMPT_CACHING`

### 总体结论

**健康度：严重过时**

AGENTS.md 的 Pipeline Stages 表描述的是已不存在的七阶段架构，stages 5/6/7 的命令/文件均已失效。新增的 `editor/`、`audit/`、`BookMemory` 三大子系统完全缺失。

**建议更新范围：**
1. Pipeline 表改为 5 个阶段（1-4 + 8），删除 refine-toc/proofread，build 标为 Stage 8
2. Semantic IR 节补充 `TableMergeRecord`、`Table.multi_page`、`Table.merge_record`
3. 新增 "Editor 子系统" 节：`edit_state/` 目录结构、`OpEnvelope`、`apply_envelope`、`memory_patches`、`split_merged_table`
4. 新增 "Audit 子系统" 节：主要 detectors
5. Config 节补全缺失 env vars
6. 修正 `VLMPageOutput` 描述，提及 `updated_book_memory`

---

## 跨报告的重叠观察（orchestrator 整理）

以下几处是多份报告**共同指向**的问题，重构时优先级应较高：

1. **死代码 `CLEAN_SYSTEM` / `TocRefineOutput` / `CleanOutput` / stage 5-6**
   - Report 2（LLM）、Report 4（死代码）、Report 5（AGENTS.md 把它们写成"已实现"）

2. **`_cjk_join` 逻辑重复 + `apply._join_text("cjk")` 空实现**
   - Report 3（EditOps）、Report 5（AGENTS.md 未提扩展）

3. **`editor/` 子包 validator 4 份重复 + `EditorModel` 等 4 个 base class 重复**
   - Report 3 和 Report 4 都标为最高优先级

4. **Config 体系 ad-hoc**
   - Report 1（手写 TOML 映射 + 死 section）、Report 2（`vlm_max_tokens=16384` 硬编码）、Report 5（env vars 未文档化）

5. **AGENTS.md 严重过时，描述的是 pre-editor 架构**
   - Report 5 独立，但 Report 1/2/3 的新增功能全部在 AGENTS.md 缺失
