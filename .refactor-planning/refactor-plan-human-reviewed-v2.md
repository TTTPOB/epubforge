# epubforge Refactor Plan — Human Reviewed v2

**Status**: finalized — 所有决策点已由用户确认，串行审阅已收敛，可进入执行。
**Base**: `refactor-plan-v2.md`（已 APPROVED） + `human_input.md`（用户决策） + 已收敛的串行审阅 / nested-config 规划结论。
**Scope**: 优雅重构——消除重复、统一命名、清理死代码、修正文档。无用户可见的功能性变更（除 config 加载行为变更）。按新世界设计执行，不考虑兼容性、迁移路径、旧状态保留。
**Authorization**: 用户已授权 break 向后兼容；项目单人使用，当前无历史数据包袱。

---

## 0. Human Decisions 汇总（来自 `human_input.md`）

| 编号 | 决策 | 与 v2 推荐比较 |
|---|---|---|
| D1 | **A — `StrictModel`** | 与 v2 推荐一致 |
| D2 | **A — 彻底删除 `CLEAN_SYSTEM`** | 与 v2 推荐一致 |
| D3 | **A — dispatch table 模式（只改 `_apply_op`）** | 与 v2 推荐一致 |
| D4 | **A — 新建 `text_utils.py`** | 与 v2 推荐一致 |
| D5 | **B — 迁移到 `pydantic-settings`** | ⚠️ **翻转 v2 推荐**（v2 推荐 A 保留 dataclass） |
| D6 | **B — 改名 Stage 5** | 与 v2 推荐一致 |
| D7 | **A — pytest fixture** | 与 v2 推荐一致 |
| D8 | **B — 把 `python -m epubforge.editor.<cmd>` 迁成 `epubforge editor <cmd>` Typer 子命令**（⚠️ 用户追加指令后翻转） | ⚠️ **翻转 v2 推荐**（v2 推荐 A 保留 `python -m`） |
| R5 额外 | 用户不关心 break fixture（"i've finished all job on it"）→ 原"zxgb fixture 重建确认"不再阻塞 | — |
| R6 修正 | **只读 CLI `--config <path>` 指定的文件**；不再隐式扫描 `config.toml` / `config.local.toml` | ⚠️ **扩大 v2 R6 的范围**（v2 保留双文件 layered 读） |
| R6 定稿 | **B — 保留嵌套 TOML 结构 + 嵌套 `Config` 子模型** | ✅ 原待定项已收口 |
| 计划外新增 | **Book.version → op_log_version** 重命名（原在 v2 §3 "放弃项 #7"） | ⚠️ **新增 R15**（v2 标记"不做"） |

### 对执行的直接影响

- **D5 翻转**改写 R6 实现路径：原本小改 dataclass + 消除 CLI 硬编码 → 现在要迁到 `pydantic-settings.BaseSettings`。
- **R6 修正**进一步改写加载语义：`load_config()` 参数从"可选"变为"唯一来源"，无 TOML 指定时只用 defaults + env。命令行必须显式 `--config <path>`（或干脆不用 TOML，只 env）。
- **R6 定稿**把实现形态写死为：嵌套 TOML + 嵌套 `Config` 子模型 + 显式 env 映射表 + `resolved_vlm()` 单一归一化入口 + Typer 根 callback 产出 effective config。
- **D8 翻转** + R6 收益合流：13 个现有 editor 入口统一迁成 `epubforge editor <cmd>` Typer 子命令后，`--config` 只在 Typer 根 callback 接收一次，再通过 `ctx.obj` 下发到所有子命令。
- **新增 R15**把原本放弃的 `Book.version` 重命名列为正式工作项——涉及 IR schema 字段改名 + `edit_log.jsonl`/`book.json` 序列化字段随之 break，用户已授权。

---

## 1. Executive Summary

本轮重构聚焦 5 条主线：
1. **editor 子包内部整洁**（R1/R2/R10）——消除 4 份复制的 validator 与 4 个等价 `extra="forbid"` 基类，补漏 `SplitMergedTable` 导出。
2. **apply 分派重构**（R4/R5）——`_apply_op` 16 路 `isinstance` → dispatch 表；`_join_text("cjk")` 修正 + CJK helper 抽取到 `text_utils.py`。
3. **Config 现代化**（R6）——迁到 `pydantic-settings`；保留嵌套 TOML，并将 `Config` 定稿为嵌套子模型；`load_config()` 只认 CLI `--config <path>`，不隐式扫描；env 通过显式映射表覆盖嵌套路径；`resolved_vlm()` / root callback 统一有效配置入口。
4. **清除死代码 + 一致性**（R3/R11/R12/R13/R14）——`CLEAN_SYSTEM`、`TocRefineOutput`、`CleanOutput` 删除（保留共享 `_*_RULES` 片段）；pipeline console.print → logging；pillow 未使用依赖删；log.py 裸 `write_text` → `atomic_write_text`；audit HTML regex 去重。
5. **命名/文档/测试**（R7/R8/R9/R15）——13 个 editor 入口统一迁到 `epubforge editor <cmd>`（其中 9 个文件 kebab → snake_case）；AGENTS.md 与 `docs/usage.md` / `docs/agentic-editing-howto.md` 同步重写；测试 `_prov` → pytest fixture；`Book.version` → `op_log_version`。

预计 **15** 个工作项、~1100 行代码改动（多为删除 + 改名），覆盖约 13 个源文件 + 13 个 editor CLI 入口迁移（其中 9 个文件重命名）。**新增 1 项生产依赖：`pydantic-settings`**。

---

## 2. 重构工作项

按"高价值低风险优先"排序。仅对比 v2 的改动在标题后标 **[MOD]** / **[NEW]**。

---

### [R1] 抽取 editor 共享 validator 到 `editor/_validators.py`

**问题描述**：`_require_non_empty` 在 4 个文件各有一份独立实现（行为一致）：`editor/ops.py:35-38`、`editor/memory.py:36-39`、`editor/doctor.py:20-23`、`editor/leases.py:11-14`。`_validate_uuid4` 和 `_validate_utc_iso_timestamp` 在 `ops.py:41-63` 与 `memory.py:42-64` 双份。`leases.py:17-27` 另有 `_parse_utc_iso`——返回 `datetime` 而非 `str`，共享同样的正则与错误路径。

**建议方案**：新建 `src/epubforge/editor/_validators.py`，导出：
- `require_non_empty(value: str, *, field_name: str) -> str`
- `validate_uuid4(value: str, *, field_name: str) -> str`
- `validate_utc_iso_timestamp(value: str, *, field_name: str) -> str`
- `parse_utc_iso(value: str, *, field_name: str) -> datetime`（保留 leases 专用语义）

4 个现有模块改为 `from epubforge.editor._validators import ...`，删除各自本地定义（~80 行删）。命名去掉前导下划线——它们现在是包内公用，而非文件私有。

**影响面**：`ops.py`、`memory.py`、`doctor.py`、`leases.py`；新增 `_validators.py`（~60 行）。无外部接口变化。

**风险**：低。Pydantic `field_validator` 闭包内调用保持不变。

**依赖**：无。

---

### [R2] 统一 editor 基类为 `StrictModel`（D1=A 确认）

**问题描述**：四个等价基类：`EditorModel`（`ops.py:75`）、`MemoryModel`（`memory.py:32`）、`DoctorModel`（`doctor.py:16`）、`LeaseModel`（`leases.py:41`），实现全部都是 `class X(BaseModel): model_config = ConfigDict(extra="forbid")`。

**建议方案**：在 `editor/_validators.py`（或独立 `editor/_models.py`）中定义：
```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
```
4 个模块 import 并继承 `StrictModel`，删除本地 4 个基类定义。

**Commit 前强制检查**：`grep -rn -E 'EditorModel|MemoryModel|DoctorModel|LeaseModel' src/ tests/` 必须仅在被删行与本次新增 import 中出现。

**影响面**：4 个 editor 文件，每个 3 行增减。

**风险**：低。

**依赖**：可与 R1 合并。

---

### [R3] 删除死代码 `CLEAN_SYSTEM` / `TocRefineOutput` / `CleanOutput`（D2=A 确认）

**问题描述**：grep 验证结果——
- `llm/prompts.py:160-223` 的 `CLEAN_SYSTEM` 在 `src/` 中零 import。
- `ir/semantic.py:186-194`（`TocRefineItem`/`TocRefineOutput`）、`ir/semantic.py:199-209`（`CleanBlock`/`CleanOutput`），均无 `src/` 引用。
- `io.py:13` 的 `LEGACY_BOOK_FILENAMES` 含 `"07_footnote_verified.json"`、`"06_proofread.json"` 对应的 stage 早已删除。

**已确认事实**：`_LINE_BREAK_RULES` / `_PARAGRAPH_BOUNDARY_RULES` / `_POETRY_RULES` / `_CROSS_PAGE_CONT_RULES` 在 `prompts.py:182-188`（CLEAN_SYSTEM）**和** `prompts.py:273-279`（VLM_SYSTEM）两处同时引用；还被 `_FOOTNOTE_CORE_RULES`/`_SPACING_RULES`/`_PENDING_*` 相邻片段共用。**必须保留这些片段不动**。

**建议方案**：
1. 删除 `llm/prompts.py:160-223`（仅 `CLEAN_SYSTEM` 三引号字符串本身）。**保留** `prompts.py:9-90` 的 7 个 `_*_RULES` 片段。
2. 删除 `ir/semantic.py:184-209` 的 4 个类与上方 `# --- stage 5.5 / stage 3` 注释区。
3. 清理 `io.py:11-16` 的 `LEGACY_BOOK_FILENAMES`：保留 `05_semantic_raw.json`/`05_semantic.json`，删除 `06_proofread.json` 与 `07_footnote_verified.json`。

**影响面**：`llm/prompts.py`（~64 行删）、`ir/semantic.py`（~25 行删）、`io.py`（2 行删）。

**风险**：低。

**依赖**：无。

---

### [R4] `_apply_op` 改 dispatch 表（D3=A 确认）

**问题描述**：`editor/apply.py:478-688`，`_apply_op` 是 16 个独立 `if isinstance(op, X): ... return book` 分支。

**建议方案**：逐 op apply 逻辑提为小函数，handler 签名统一为宽类型 `(book: Book, op: EditOp, op_id: str) -> Book`，函数体内用 `assert isinstance(op, SetRole)` 做 narrow：
```python
def _apply_set_role(book: Book, op: EditOp, op_id: str) -> Book:
    assert isinstance(op, SetRole)
    ...
    return book

_APPLY_DISPATCH: dict[type[EditOp], Callable[[Book, EditOp, str], Book]] = {
    SetRole: _apply_set_role,
    SetStyleClass: _apply_set_style_class,
    # ... 共 13 个 text-mutating ops
}

def _apply_op(book: Book, op: EditOp, *, op_id: str) -> Book:
    if isinstance(op, NoopOp | CompactMarker | RevertOp):
        return book
    handler = _APPLY_DISPATCH.get(type(op))
    if handler is None:
        raise AssertionError(f"unsupported op type {type(op)!r}")
    return handler(book, op, op_id)
```

**D3 范围**：仅改 `_apply_op`。`_check_new_uid_collisions` / `_resolve_intra_chapter_uid` / `_target_effect_preconditions` 三处 isinstance 暂不改——每个函数的 side-concern 形式不同（返回 set/str 等），单分派无法干净覆盖。

**影响面**：`editor/apply.py`（重构 ~210 行为若干短函数 + 一个 dispatch dict）。纯代码重排，不改语义。

**风险**：中。重构面较大，需完整跑 `tests/test_editor_apply.py` 覆盖所有 op 类型。

**依赖**：无，但建议在 R1/R2 后进行。

---

### [R5] 修正 `_join_text("cjk")` 隐式 catch-all → concat + 共享 CJK-join（D4=A 确认）

**问题描述**：
- `editor/apply.py:196-201`：`_join_text` 的 `"cjk"` 分支走的是默认 fallthrough `return "".join(parts)`——既不是 CJK 逻辑，也不是显式 concat。`MergeBlocks` 用 `join="cjk"` 时行为与 `"concat"` 相同，CJK 语义丢失。
- `assembler.py:601` 有完整 `_cjk_join(prev: str, cont: str) -> str`，二元签名与 n-ary 需求不匹配。

**建议方案**：
1. 新建 `src/epubforge/text_utils.py`，迁入：
   - `is_no_space_char(c: str) -> bool`（来自 `assembler._is_no_space_char`）
   - `cjk_join_pair(prev: str, cont: str) -> str`（来自 `assembler._cjk_join`，改公开）
   - `cjk_join(parts: list[str]) -> str` —— n-ary 版：`reduce(cjk_join_pair, parts, "")`
   - **Note**: 保留原 `_cjk_join` 的 `prev.rstrip() / cont.lstrip()` 语义。不增加 `strip=True/False` 参数。
2. `assembler.py` 保留 `_cjk_join` 作为 thin alias 或直接改 import。
3. `apply._join_text` 改写为 exhaustive `match`：
```python
def _join_text(parts: list[str], join: Literal["concat", "cjk", "newline"]) -> str:
    match join:
        case "newline": return "\n".join(parts)
        case "cjk":     return cjk_join(parts)
        case "concat":  return "".join(parts)
        case _:         raise AssertionError(f"unreachable join kind {join!r}")
```

**影响面**：新增 `text_utils.py`（~30 行）；`assembler.py:577-640` 局部改 import；`apply.py:196-201` 4-7 行改。

**R5 用户补充**："i don't care breaking that file, i've finished all job on it." → 若 zxgb 的 `book.json` 因 `MergeBlocks(join="cjk")` replay 结果变化，**直接接受 book.json 差异或重建 edit_state**。

**风险**：低。行为变更范围已确认可接受。

**依赖**：无。

---

### [R6] Config 迁移到 pydantic-settings + 消除 CLI 硬编码 + CLI 显式指定 `--config`（D5=B 确认 ⚠️）[MOD]

**问题描述（v2 基础）**：
- `config.local.toml:32-44` 有 `[proofread]` 和 `[footnote_verify]` 节，`config.py:load_config` 完全不读取——死配置。
- `editor/tool_surface.py:337` 的 `--ttl default=1800`、`:378` 的 `--ttl default=300` 与 `config.py:37` 的 `editor_lease_ttl_seconds` 是独立硬编码。CLI 子进程从不读 `Config`，用户改 TOML 无效。
- `leases.py:106/146` 的 `ttl: int = 1800/300` 是方法级兜底，**保留**。
- `llm/client.py:122-123`：`if use_vlm and self.max_tokens is None: self.max_tokens = 16384` 对 Config 不可见。
- `cli.py:43`：`_log_level = log_level or os.environ.get("EPUBFORGE_LOG_LEVEL", "INFO")` 绕过 config.py。

**用户新增约束（human_input.md R6）**：
> don't infer config file to read, only read from the file specified in cli arg, don't make config.toml / config.local.toml as default config, force user to specify.

即：`config.py:65` 的 `toml_paths = (config_path,) if config_path else (Path("config.toml"), Path("config.local.toml"))` **隐式 fallback 必须消除**。没有 `--config` 时 → 仅用 defaults + env vars。

**建议方案（修订版，已按 β 定稿）**：

**Step 1: 迁到 `pydantic-settings`，并将 `Config` 定稿为嵌套子模型**
1. 新增依赖 `pydantic-settings>=2.7`（`pyproject.toml`）。
2. 改写 `src/epubforge/config.py`，不再使用扁平字段 + nested->flat 胶水，而是直接让 Python 内存结构与 TOML section 同构：
   ```python
   from pydantic import BaseModel, Field
   from pydantic_settings import BaseSettings, SettingsConfigDict

   class ProviderSettings(BaseModel):
       base_url: str = "https://openrouter.ai/api/v1"
       api_key: str | None = None
       model: str = "anthropic/claude-haiku-4.5"
       timeout_seconds: float = 300.0
       max_tokens: int | None = None
       prompt_caching: bool = True
       extra_body: dict[str, Any] = Field(default_factory=dict)

   class RuntimeSettings(BaseModel):
       concurrency: int = 4
       cache_dir: Path = Path("work/.cache")
       work_dir: Path = Path("work")
       out_dir: Path = Path("out")
       log_level: Literal["DEBUG", "INFO", "WARNING"] = "INFO"

   class EditorSettings(BaseModel):
       lease_ttl_seconds: int = 1800
       book_exclusive_ttl_seconds: int = 300
       compact_threshold: int = 50
       max_loops: int = 50

   class ExtractSettings(BaseModel):
       vlm_dpi: int = 200
       max_simple_batch_pages: int = 8
       max_complex_batch_pages: int = 12
       enable_book_memory: bool = True

   class Config(BaseSettings):
       model_config = SettingsConfigDict(extra="ignore")
       llm: ProviderSettings = Field(default_factory=ProviderSettings)
       vlm: ProviderSettings = Field(default_factory=lambda: ProviderSettings(model="google/gemini-flash-3", max_tokens=16384))
       runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
       editor: EditorSettings = Field(default_factory=EditorSettings)
       extract: ExtractSettings = Field(default_factory=ExtractSettings)
   ```
3. 顶层 5 个 nested 字段统一使用 `Field(default_factory=...)`，子模型叶子字段自带默认值；因此 `load_config(None)` 在“无 TOML、无 env”时也必须直接成功。
4. TOML 形态固定保留嵌套结构：`[llm]` / `[vlm]` / `[runtime]` / `[editor]` / `[extract]`。`config.example.toml` 保持这种结构，不扁平化。
5. TOML / init kwargs 对未知 section / key 一律 `extra="forbid"` fail fast；未知 env 则继续忽略，不报错。

**Step 2: source layering 收口为“defaults + explicit TOML + env”**
```python
def load_config(config_path: Path | None = None) -> Config:
    # config_path=None: defaults + env only
    # config_path=Path(...): defaults + that TOML + env
    ...
```
1. `config_path=None`：只使用 defaults + env，**绝不扫描** cwd 下任何 `config.toml` / `config.local.toml`。
2. `config_path=Path(...)`：只读取这一份 TOML；路径不存在直接报错。
3. 关闭自动 `.env` 扫描、默认 secrets source、`EPUBFORGE_CONFIG_PATH` 一类额外入口。
4. `load_config()` 只负责 settings source 合并；CLI runtime override（如 `--log-level` / `--log-file`）不属于 `load_config()` 层。

**Step 3: env 采用显式映射表，而不是自动 nested delimiter**
1. 不使用 `env_nested_delimiter` 之类的自动推导。
2. 维护固定白名单映射表，把 env 名显式映射到嵌套路径，例如：
   - `EPUBFORGE_LLM_BASE_URL` -> `llm.base_url`
   - `EPUBFORGE_LLM_API_KEY` -> `llm.api_key`
   - `EPUBFORGE_VLM_MODEL` -> `vlm.model`
   - `EPUBFORGE_RUNTIME_CONCURRENCY` -> `runtime.concurrency`
   - `EPUBFORGE_RUNTIME_LOG_LEVEL` -> `runtime.log_level`
   - `EPUBFORGE_EDITOR_LEASE_TTL_SECONDS` -> `editor.lease_ttl_seconds`
   - `EPUBFORGE_EDITOR_BOOK_EXCLUSIVE_TTL_SECONDS` -> `editor.book_exclusive_ttl_seconds`
   - `EPUBFORGE_EXTRACT_VLM_DPI` -> `extract.vlm_dpi`
3. env 只覆盖被点到的叶子字段，不得把整个 nested 子对象替换掉；同一 section 中 TOML / default 提供的 sibling 字段必须保留。
4. `extra_body` 保持 TOML-only，不为其增加 JSON-in-env 解析。

**Step 4: `resolved_vlm()` 作为唯一归一化入口**
1. `vlm.base_url` / `vlm.api_key` 原始字段允许为 `None`。
2. 在 `Config` 中提供唯一规范入口：`resolved_vlm() -> ProviderSettings`。
3. `require_vlm()`、`LLMClient(use_vlm=True)` 和其它调用方只使用 `resolved_vlm()`；不再并列保留 `resolved_vlm_base_url` / `resolved_vlm_api_key` 一类旁路 API。
4. `vlm.max_tokens` 直接在配置层给出默认值 `16384`，删除 `llm/client.py` 中的隐式 `None -> 16384` 兜底。

**Step 5: CLI 根 callback 产出 effective config**
1. 顶层 `src/epubforge/cli.py` 根 callback 是唯一配置装配点：
   - 解析 `--config`
   - 调用 `load_config(config_path=...)`
   - 在得到 `Config` 后再应用 CLI runtime override
2. `--log-level` 若出现，直接覆写到 effective `config.runtime.log_level`；不要再维护第二套平行 `_log_level` 状态。
3. `ctx.obj` 不直接塞裸 `Config`，而是塞最小 `AppContext`：
   ```python
   @dataclass
   class AppContext:
       config: Config
       log_file_override: Path | None
   ```
4. 子命令统一从 `ctx.find_root().obj.config` 取 effective config。

**Step 6: `tool_surface.py` 彻底退回业务层**
1. `tool_surface.py` 不得导入 `load_config`。
2. `tool_surface.py` 不得声明 CLI option default，不得读取 `typer.Context`。
3. 命令层参数统一写成 `ttl: int | None = None`，由命令层决定默认值：
   - chapter lease -> `cfg.editor.lease_ttl_seconds`
   - book lock -> `cfg.editor.book_exclusive_ttl_seconds`
4. `tool_surface.acquire_lease(...)` / `tool_surface.acquire_book_lock(...)` 只接收最终解析好的整数 TTL。`leases.py:106/146` 的默认值仅保留为最终兜底，不作为 CLI 默认值来源。

**Step 7: 删除死 section 并统一可见配置项**
1. `config.example.toml` 删除 `[proofread]` / `[footnote_verify]`。
2. `runtime.log_level` 成为正式配置字段，替代 `cli.py` 里直接读 `os.environ` 的逻辑。
3. `editor.book_exclusive_ttl_seconds` 成为正式配置字段，替代当前 book lock 的 `300` 硬编码。

**影响面**：
- `pyproject.toml`（+1 依赖）
- `src/epubforge/config.py`（整体重写为 nested settings + source hook + explicit env mapping）
- `src/epubforge/cli.py`（根 callback 产出 `AppContext`，`--log-level` 覆写 effective config）
- `src/epubforge/editor/app.py`（R9 新增；13 个 editor 入口共用根 callback 注入链）
- `src/epubforge/editor/tool_surface.py`（移除 config/CLI 感知，仅保留业务函数）
- `config.example.toml`（保持 nested TOML，死 section 删）
- `llm/client.py`（删除 VLM token 隐式默认）

**风险**：中-高。
1. `pydantic-settings` source hook 需要正确处理“单叶子 env override 不冲掉 sibling 字段”的 nested merge 语义。
2. 顶层 root callback + `AppContext` 一旦收口不严，13 个 editor 子命令很容易出现 `ctx.obj` / effective config 不一致。
3. `resolved_vlm()` 必须是唯一规范入口，否则 fallback 会再次散落到 config / client / CLI 多处。

**依赖**：必须与 R9 绑定（R9 把 13 个 editor 入口统一挂到 Typer 根注入链后，`--config` 与 effective config 才能真正单点下发）。

---

### [R7] AGENTS.md 重写

**问题描述**：
- `AGENTS.md:6` 声称"Seven-stage pipeline: parse → classify → extract → assemble → refine-toc → proofread → build"，但 `refine-toc` / `proofread` **命令不存在**（`cli.py:80-155` 只注册 6 个子命令）。
- `AGENTS.md:13-22` Pipeline 表格 5/6/7 行完全虚构。
- `AGENTS.md` 对 `editor/` 子系统、`audit/` 子系统、`BookMemory` 机制零描述。
- `AGENTS.md:50` 未提 `Table.multi_page`、`Table.merge_record`、`TableMergeRecord`。
- `AGENTS.md:51` 未提 `VLMPageOutput.updated_book_memory`。
- `AGENTS.md:55-64` Config env vars 清单漏掉 `EPUBFORGE_LLM_TIMEOUT`、`EPUBFORGE_VLM_TIMEOUT`、`EPUBFORGE_LLM_MAX_TOKENS`、`EPUBFORGE_VLM_MAX_TOKENS`、`EPUBFORGE_ENABLE_BOOK_MEMORY`、`EPUBFORGE_EDITOR_*` 全套；以及 `EPUBFORGE_EDITOR_NOW`、`EPUBFORGE_PROJECT_ROOT`、`EPUBFORGE_WORK_DIR`、`EPUBFORGE_EDIT_STATE_DIR`（test-only / scratch subprocess injection）。
- `AGENTS.md:41` 提"Gemini 2.5/3"模糊；默认 VLM 是 `google/gemini-flash-3`（`config.py:24`）。

**建议方案**：重写 `AGENTS.md`，结构：
1. **Project Overview**：改为"5-stage ingestion pipeline + editor subsystem"。
2. **Pipeline Stages**：表格只列 parse/classify/extract/assemble/build；stage 编号按 D6=B **改为 Stage 1-5**。
3. **Editor Subsystem**（全新节）：简述 `edit_state/` 目录结构、`OpEnvelope` / `apply_envelope` / `memory_patches` 语义、所有 `epubforge editor <cmd>` 命令清单及 JSON 契约要点。**补写 invariant**: "`apply_envelope` 的事务性依赖 `working = book.model_copy(deep=True)`（`apply.py:1065` 附近）——任何 op/memory_patches 失败都回滚到原 `book`。"
4. **Audit Subsystem**（全新节）：列举 `detect_structure_issues` / `detect_table_merge_issues` / `detect_footnote_issues` / `detect_dash_inventory` / `detect_table_issues` / `detect_invariant_issues`。
5. **Semantic IR**：补 `TableMergeRecord`、`Table.multi_page`、`Table.merge_record`；`VLMPageOutput.updated_book_memory`；`BookMemory` 用途。**补一句** "`Book.op_log_version: int` 是 op 日志版本号（每次 `apply_envelope` +1），不是 IR schema 版本"（R15 之后用新字段名；若 R15 先合入 AGENTS.md 也按新名写）。
6. **Config**：全量 env var 清单 + `EPUBFORGE_EDITOR_NOW` 等（标 "test-only / scratch subprocess injection"）。**新增一段**："TOML 配置文件路径必须通过 `--config <path>` CLI 参数显式指定；不再隐式读取 `config.toml` / `config.local.toml`。`Config` 为嵌套子模型，env 通过显式映射表覆盖嵌套路径；`resolved_vlm()` 是 VLM 实际生效配置的唯一归一化入口。"（R6 行为说明）。
7. **保留** Beads / shell commands 段落不动。

**影响面**：仅 `AGENTS.md`（~160 行改动）。

**依赖**：建议放在所有代码重构之后，反映最终状态。

---

### [R8] 测试 `_prov` fixture 统一（D7=A 确认 pytest fixture）

**问题描述**：`_prov` helper 在 **10 个**测试文件各自独立定义（`tests/test_audit_table_merge.py:9` 等 10 处）。无 `conftest.py`。

**建议方案**：新建 `tests/conftest.py`：
```python
@pytest.fixture
def prov():
    def _make(page: int = 1, source: Literal["llm", "vlm", "passthrough"] = "passthrough") -> Provenance:
        return Provenance(page=page, bbox=None, source=source)
    return _make
```
各测试文件的测试函数签名加入 `prov` 参数，调用点从 `_prov(...)` 改为 `prov(...)`。删除 10 处本地定义。

**影响面**：新建 `tests/conftest.py`（~15 行）；10 个测试文件各删 3-5 行，改所有 `_prov()` 调用点与相关函数签名。

**风险**：低。

**依赖**：无。

---

### [R9] 迁移 13 个现有 editor 入口为 `epubforge editor <cmd>` Typer 子命令（D8=B 确认 ⚠️）[MOD]

**用户追加指令（human_input.md 最新，覆盖原 D8=A）**：
> python -m 子命令都改成 epubforge editor xxx 去掉 -m editor.xx

**问题描述**：
- `src/epubforge/editor/__main__.py` 当前公开的稳定 editor 入口共有 **13 个**：`init`、`import-legacy`、`doctor`、`propose-op`、`apply-queue`、`acquire-lease`、`release-lease`、`acquire-book-lock`、`release-book-lock`、`run-script`、`compact`、`snapshot`、`render-prompt`。
- 其中只有 9 个命令对应 kebab-case 文件，需要做文件层重命名；`init`、`doctor`、`compact`、`snapshot` 已是 snake_case，但同样必须迁到 `epubforge editor <cmd>` 统一入口。
- 当前用户调用方式是 `python -m epubforge.editor.<name>`，绕过顶层 `epubforge` Typer app；测试 `tests/test_editor_tool_surface.py` 仍以 `subprocess + python -m` 为主；`docs/usage.md` 与 `docs/agentic-editing-howto.md` 也在描述旧入口。

**建议方案（D8=B 的实现路径）**：

**Step 1：将 13 个命令面与文件重命名范围拆开表述**
1. 命令面迁移范围是 **13 个 editor 入口**，这必须体现在正文、测试和验收口径中。
2. 文件重命名范围仍然是 **9 个 kebab-case 文件**：
   - `acquire-book-lock.py` -> `acquire_book_lock.py`
   - `acquire-lease.py` -> `acquire_lease.py`
   - `apply-queue.py` -> `apply_queue.py`
   - `import-legacy.py` -> `import_legacy.py`
   - `propose-op.py` -> `propose_op.py`
   - `release-book-lock.py` -> `release_book_lock.py`
   - `release-lease.py` -> `release_lease.py`
   - `render-prompt.py` -> `render_prompt.py`
   - `run-script.py` -> `run_script.py`
3. `init`、`doctor`、`compact`、`snapshot` 不做文件重命名，但要与其余 9 个命令一起挂到新的 `editor_app`。

**Step 2：建立 `editor` Typer 子 app，并让 13 个命令都走同一注入链**
```python
editor_app = typer.Typer(help="Editor subsystem commands", no_args_is_help=True)

@editor_app.command("propose-op")
def _propose_op_cmd(ctx: typer.Context, work: Path = typer.Argument(...)):
    app_ctx = ctx.find_root().obj
    raise typer.Exit(propose_op(work, sys.stdin.read(), app_ctx.config))

# ... 其余 12 个命令同理
```
- 子命令名保持 **kebab-case**，文件名改为 **snake_case**。
- 所有 13 个命令都从 `ctx.find_root().obj.config` 取 effective config；不允许某些命令继续自建 config 入口。

**Step 3：`cli.py` 挂载 editor 子 app**
```python
from epubforge.editor.app import editor_app
app.add_typer(editor_app, name="editor")
```
- 根 callback 负责 `--config` 与 `--log-level` 装配 `AppContext`；`editor_app` 不再拥有自己的 config callback。

**Step 4：每个命令模块收缩为“业务函数 + Typer 包装”**
- 13 个入口对应模块都应收敛成不依赖 argparse 的纯函数或等价业务函数。
- `tool_surface.py` 继续下沉为纯业务层，不承担 parser/default/config 读取。
- 旧的 `argparse` 块全部删除或缩成最薄兼容包装；由于本轮已明确废除 `python -m epubforge.editor.<cmd>`，可直接删掉入口包装而不是双轨维护。

**Step 5：删除或瘦身 `editor/__main__.py`**
- `python -m epubforge.editor.<cmd>` 不再是支持入口。
- `python -m epubforge.editor` 若仍需保留，可改为调用 `editor_app()`；若无收益，直接删除 `editor/__main__.py` 的命令分发表。

**Step 6：测试口径改为“顶层 app 覆盖 + 少量真实入口 smoke”**
1. 主体命令测试迁到：
   ```python
   CliRunner().invoke(app, ["--config", str(config_path), "editor", "<cmd>", ...])
   ```
   重点是覆盖顶层 root callback 的 config 注入链，而不是只直调 `editor_app`。
2. 可保留极少量 `editor_app` 级别的轻量测试，但**不得**把配置注入验证建立在 `editor_app` 直调上。
3. 保留至少 1 条真实入口 smoke：
   - `uv run epubforge editor <cmd> ...`
   - 用来验证 console script 挂载与“未传 `--config` 时不会误读 ambient TOML”。
4. 若测试涉及 `EPUBFORGE_EDITOR_NOW` / `EPUBFORGE_PROJECT_ROOT` / `EPUBFORGE_WORK_DIR` / `EPUBFORGE_EDIT_STATE_DIR` 这类 subprocess 语义，可保留少量 subprocess 测试；其余迁为 `CliRunner + monkeypatch`。

**Step 7：同步更新调用站点（文档 + prompt）**
- `src/epubforge/editor/prompts.py` 中所有 `python -m epubforge.editor.run-script` 改为 `epubforge editor run-script`。
- `docs/usage.md` 的 editor 命令示例全部改为 `epubforge editor <cmd>`，并与 `--config <path>` 新约定一致。
- `docs/agentic-editing-howto.md` 全文改写为新入口形式；原“当前稳定 surface 只有 `python -m epubforge.editor.*`”的表述全部删除。
- `docs/footnote-audit-process.md` / `docs/finer-proofread.md` / `docs/fix-plan-v3.md` 若有旧引用，一并更新。
- `AGENTS.md` 在 R7 中同步反映 13 个 editor 入口的新形态。

**Step 8：执行扫尾检查**
- `grep -rn 'python -m epubforge\.editor\.' .` 返回空。
- `grep -rn '_run_module\|python.*-m.*epubforge\.editor' tests/` 返回空，或仅剩显式保留的真实入口 smoke。
- `grep -rn 'epubforge\.editor\.[a-z]*-[a-z]*' .` 返回空。
- `grep -rn 'load_config\(' src/epubforge/editor` 返回空。

**影响面**：
- 新增 `src/epubforge/editor/app.py`（13 个 command 注册 + import wiring）。
- 9 个文件重命名；13 个命令模块的 argparse 包装删除或重构。
- `src/epubforge/cli.py`（挂载 `editor_app`，统一 root callback 注入）。
- `src/epubforge/editor/__main__.py`（删除或收缩为 `editor_app()`）。
- `tests/test_editor_tool_surface.py`（主体迁到顶层 `app` 路径；保留少量真实入口 smoke）。
- `docs/usage.md`、`docs/agentic-editing-howto.md` 及其他引用文件。
- `editor/prompts.py`。

**风险**：中-高。
1. **13 个命令统一注入链**：任何一个命令漏从 `ctx.find_root().obj.config` 取值，都会留下双入口。
2. **顶层 app 测试迁移**：若仍用 `CliRunner(editor_app)` 验证 config 注入，会产生“测试绿了但真实入口坏了”的假阳性。
3. **真实入口 smoke 缺失**：若完全取消 subprocess/console script 覆盖，`uv run epubforge editor ...` 挂载错误不易暴露。
4. **R9 与 R6 深度耦合**：必须同一 commit 完成；中间态会让 `--config` / `AppContext` / TTL 默认值同时失真。

**依赖**：与 R6 绑定同 commit（根 callback 同时处理 `--config` 与挂载 `editor_app`）；R7 文档同步必须反映新命令形式。

---

### [R10] 补漏 `SplitMergedTable` 到 `editor/__init__.py` 的 `__all__`

**问题描述**：`editor/ops.py:869` 的 `__all__` 已含 `SplitMergedTable`，但 `editor/__init__.py:28-51` 的 re-export 与 `__all__` **遗漏**。

**建议方案**：`editor/__init__.py:28-51` 的 import 语句加 `SplitMergedTable,`，`__all__` 列表加 `"SplitMergedTable",`。

**影响面**：2 行。**风险**：极低。**依赖**：无。

---

### [R11] `console.print` → `logging` 在 pipeline.py 中统一

**问题描述**：`pipeline.py` 对阶段边界使用 `console.print`（rich），其他所有模块用 `logging`。两套输出路径，stage banner 不会进 `work/<book>/logs/run-*.log`。

**建议方案**：删掉 `pipeline.py:8,13` 的 `Console` import 与实例；11 处 `console.print(...)` 改 `log.info(...)`。stderr 侧 RichHandler 自动上色，不损失视觉。

**影响面**：`pipeline.py` ~15 行。**风险**：低（CLI 输出格式轻微变化）。**依赖**：无。

---

### [R12] 清理 `pyproject.toml` 的 `pillow` 未使用依赖

**问题描述**：`pyproject.toml:19` 声明 `pillow>=10`，但 `src/` grep 无 `PIL`/`pillow` import。

**建议方案**：删除 `pyproject.toml:19`。

**影响面**：1 行；`uv.lock` 工具重算。**风险**：低（fitz/PyMuPDF 不依赖 PIL）。**依赖**：无。

---

### [R13] 统一 editor state 写入路径：`log.py` 裸 `write_text` → `atomic_write_text`

**问题描述**：一致性瑕疵——同包 `editor/state.py:230-234` 已提供 `atomic_write_text/json/model`（临时文件 + `os.replace`）且同包大量使用，唯独 `editor/log.py:190-191,217` 3 处对 `archive_path / CURRENT_LOG`、`archive_path / BOOK_FILE`、`paths.current` 使用裸 `write_text`。

**建议方案**：`log.py` import `from epubforge.editor.state import atomic_write_text`；3 处 `.write_text(...)` 改为 `atomic_write_text(path, content)`。

**影响面**：`editor/log.py` 3 行。**风险**：低。**依赖**：无。

---

### [R14] 抽取 audit HTML regex 到 `audit/_html.py`

**问题描述**：`audit/tables.py:13-17` 与 `audit/table_merge.py:20-23` 存在 HTML 解析 regex 重复：
- **完全重复**：`TBODY_RE`、`COLSPAN_RE` 两个完全一致。
- **等价重复**：`ROW_RE` pattern 稍差异但语义等价。
- **语义不同保留**：`CELL_RE` 两处差异实质，**不合并**。
- `ROWSPAN_RE` 只在 tables.py 有，保留。

**建议方案**：新建 `src/epubforge/audit/_html.py`，导出 `TBODY_RE`、`ROW_RE`（使用 tables.py 捕获式版本）、`COLSPAN_RE`。两文件改为 import，删本地定义。`table_merge.py` 改用 `.group(1)` 或 `.group(0)` 对应新 ROW_RE。

**影响面**：新增 `audit/_html.py`（~8 行）；`audit/tables.py` 删 3 行 + import；`audit/table_merge.py` 删 3 行 + import + 1 处 group 调整。

**风险**：低。需跑 `tests/test_audit_detectors.py` + `tests/test_audit_table_merge.py`。

**依赖**：无，与 R3/R12 同 commit（死/重复清理）。

---

### [R15] `Book.version` → `Book.op_log_version` 重命名（用户新增 ⚠️）[NEW]

**问题描述**：`ir/semantic.py:174` 的 `Book.version: int = 0` 名称含糊——实际语义是"op 日志版本号"（每次 `apply_envelope` +1，与 `OpEnvelope.base_version` / `applied_version` 对齐；不是 IR schema 版本）。用户明确要求改名。

**v2 原放弃理由**："牵扯序列化格式 break，且 `version` 名称虽宽泛但约定俗成"——现用户授权 break fixture，该理由消失。

**Scope 界定（重要）**：仅重命名 `Book.version` 及其所有引用点；**不改** `OpEnvelope.base_version` / `applied_version`（这些名称已清晰，且含义不同——"the Book version that this op is based on / applied at"）。

**grep 范围**（需核对的文件，每处需识别是否指 `Book.version`）：
- **确认涉及 Book.version**：`src/epubforge/ir/semantic.py:174`（定义）；`src/epubforge/editor/apply.py`、`editor/tool_surface.py`、`editor/prompts.py`、`editor/log.py`、`editor/state.py`、`editor/ops.py`、`editor/memory.py`（通过 grep `\bversion\b` 确认上下文）。
- **确认不涉及**：`src/epubforge/epub_builder.py`、`src/epubforge/io.py` 的 `version` 可能指其他字段——逐一核对。
- **测试文件**：`tests/test_editor_apply.py`、`test_foundations_helpers.py`、`test_editor_tool_surface.py`、`test_editor_log.py`、`test_ir_semantic.py`——大量引用需改。

**建议方案**：
1. `ir/semantic.py:174`：`version: int = 0` → `op_log_version: int = 0`。
2. 全项目 grep + 批量替换 `book.version` → `book.op_log_version`；`self.version`（在 `Book` 类方法上下文）→ `self.op_log_version`。
3. **不改**：`OpEnvelope.base_version` / `applied_version`（字段名不同）、`epub_builder.py` 的 `version` 若指 EPUB metadata 版本（应不是 `Book.version`，grep 确认）、`io.py` 的 `LEGACY_BOOK_FILENAMES` 无 version 字段。
4. **JSON 序列化 break**：`book.json` 中 `"version": N` 字段 → `"op_log_version": N`；`edit_log.jsonl` 若直接序列化 Book（检查），随之 break。**用户已授权**，zxgb `edit_state/` 若需重建则重建。
5. AGENTS.md 相关一句在 R7 中使用新字段名即可（R7 依赖 R15 的字段名变更，执行顺序应 R15 先于 R7）。

**影响面**：IR schema + editor 8 个文件 + 测试 5 个文件 + 1 条 AGENTS.md 文案。总计 ~40 行改动（多为单 token 替换）。

**人决策点**：无（用户已明确"i prefer to use op_log_version"）。

**风险**：中-高。
1. 序列化字段名 break——zxgb `edit_state/book.json` replay 失败（用户授权）。
2. 若 `Book` 以外还有子对象（如 `Chapter`）有 `version` 字段，不能误伤——执行时 grep 细核。
3. pyrefly/mypy 会捕捉所有错漏，但 JSON 字符串 key 不会——必须全项目 grep `"version"` 在 book 上下文中的出现。

**依赖**：放在 R7 之前（文档需引用新名）；与 R6/R9 同 commit 或独立 commit 均可。

---

## 3. 计划外明确放弃的事项

以下问题**不在**本轮范围：

1. **Config 迁移到 pydantic-settings BaseSettings** — ~~不做~~ **已改为做**（R6 新方案，D5=B）。
2. **prompt caching 多轮 `cache_control` 扩展**（Report 2 P6）：目前 VLM 调用只有一条 system message，功能性足够。
3. **`_call_parsed` / `_call_json_object_fallback` usage 解包/budget 翻倍共享**（Report 2 P2）：降级路径偶发，抽共享反而增加间接层。
4. **`use_vlm: bool` → `ClientProfile` dataclass**（Report 2 P4）：当前项目只有一个 client 调用点，profile 抽象收益极低。
5. **`staging.jsonl` 的 apply-queue batch atomicity**（Report 3 P5）：设计选择，在 AGENTS.md 新节中文档化。
6. **`TableMergeRecord.constituent_block_uids` 增补**（Report 3 P7）：uid 在 assembler 阶段不稳定，是设计选择。
7. ~~**`Book.version` 重命名**~~ → **已改为做**（R15，用户明确要求）。
8. **`editor/prompts.py` 的 f-string 静态检查**（Report 2 P7）：prompt 模板变更不频繁。
9. **pipeline/extract 绕过 io.py**（Report 4 #3）：extract.py 的中间 artifact 是 pipeline 内部 cache，不在同一致性对比域；R13 只修 log.py（核心 state 写入）。
10. **`audit` 的 `CELL_RE` 合并**（Report 4 #2 的子项）：两处 CELL_RE 语义不同（见 R14）。

---

## 4. 整体顺序与分组（commit 粒度）

建议 **6 个 commit**（按执行顺序；比 v2 多一个因 R15 新增 + R6 scope 扩大）：

**Commit 1 — editor 内部去重**（~1 天）
- R1（validators 抽取）
- R2（StrictModel 统一）
- R10（`SplitMergedTable` 导出补漏）

**Commit 2 — 死代码清理 + audit 整合**（~半天）
- R3（CLEAN_SYSTEM 字符串删除 + `Toc*`/`Clean*` model 删除）
- R12（pillow 依赖删除）
- R14（audit HTML regex 抽取）

**Commit 3 — apply 重构 + CJK**（~1 天）
- R4（`_apply_op` dispatch 表）
- R5（`_join_text("cjk")` 修正 + text_utils 抽取）

**Commit 4 — IR schema rename**（~半天）
- R15（`Book.version` → `op_log_version`）

*单独 commit 方便 revert 与定位序列化 break 的冲击面。*

**Commit 5 — Config 现代化 + editor CLI 迁 Typer**（~2 天，最大 commit）
- R6（pydantic-settings 迁移 + 嵌套 `Config` 子模型 + 显式 env 映射表 + `resolved_vlm()` + 只读 `--config` + effective config 注入链）
- R9（13 个 editor 入口统一迁 `epubforge editor <cmd>` Typer 子命令；其中 9 个文件重命名 + 测试迁顶层 `app` 路径 + 少量真实入口 smoke + 文档全局替换）
- R11（console.print → logging）
- R13（log.py atomic_write）

*R6 与 R9 **必须**同一 commit：Typer 根 callback 处理 `--config`，所有子命令通过 `ctx.obj` 拿 Config——分开提交会产生无法运行的中间态。*

**Commit 6 — 文档 + 测试重整**（~1 天）
- R7（AGENTS.md 重写，含 invariant、`op_log_version` 语义说明、`--config` 新约定）
- R8（conftest.py，pytest fixture 风格）

### 为什么分 6 个
- **Commit 1** 纯机械重构，风险最低，先做让后续项基于新基类 / 共享 validator。
- **Commit 2** 删除 + 新抽取 util，走得越早暴露问题越快。
- **Commit 3** dispatch 改动较大，独立 commit 便于 review 与回滚。
- **Commit 4** 序列化 break 独立——任何 fixture 问题一眼可定位到此 commit。
- **Commit 5** pydantic-settings 迁移 + 13 个 editor 入口统一接入顶层配置注入链深度耦合，合并可避免 `load_config()` / `AppContext` / TTL 默认值 / Typer 挂载出现半旧半新的中间态。
- **Commit 6** 最后做，反映终态。

---

## 5. 验证策略

**每 commit 必跑**：
```bash
uv run pytest -x
uv run pyrefly
uv run pre-commit run --all-files   # 若配置
```

**端到端冒烟**（Commit 3/4/5 后重点）：
```bash
uv run epubforge --config config.example.toml run fixtures/zxgb.pdf --force-rerun --from 1
```
若 zxgb 的 `edit_state/` 因 R5/R15 不可复用，允许重建。

**editor CLI 冒烟**（Commit 5 后，**新形式**）：
```bash
uv run epubforge --config config.example.toml editor init work/zxgb
uv run epubforge --config config.example.toml editor doctor work/zxgb
uv run epubforge --config config.example.toml editor propose-op work/zxgb < sample_ops.json
uv run epubforge --config config.example.toml editor apply-queue work/zxgb
```
（子命令名用 kebab-case 遵循 Typer/Click 惯例；`python -m epubforge.editor.<cmd>` 入口已废除。）

### 关键检查清单
- [ ] `grep -r _require_non_empty src/` 只剩 `_validators.py` 一处。
- [ ] `grep -rn -E 'EditorModel|MemoryModel|DoctorModel|LeaseModel' src/ tests/` 只剩 R2 过程的 import 行。
- [ ] `grep -r CLEAN_SYSTEM src/` 返回空。
- [ ] `grep -r _LINE_BREAK_RULES src/epubforge/llm/prompts.py` 返回定义 + `VLM_SYSTEM` 引用共 2 类。
- [ ] `grep -r 16384 src/` 返回空（或仅 `config.py` 一处）。
- [ ] `grep -rn 'EPUBFORGE_CONFIG_PATH' src/ docs/ tests/ AGENTS.md` 返回空。
- [ ] `grep -rn 'python -m epubforge\.editor\.' .` 返回空（D8=B 废除该入口形式）。
- [ ] `grep -rn 'epubforge\.editor\.[a-z]*-[a-z]*' .` 返回空（kebab-case import path 消除）。
- [ ] `grep -rn 'load_config\(' src/epubforge/editor` 返回空。
- [ ] `grep -rn '_run_module\|subprocess.*epubforge\.editor' tests/` 返回空，或仅剩显式保留的真实入口 smoke。
- [ ] `uv run epubforge --config config.example.toml editor doctor work/zxgb` 正常退出。
- [ ] `pytest` 覆盖 `load_config(None)` 在存在 ambient `config.toml` / `config.local.toml` 时仍完全忽略它们。
- [ ] `pytest` 覆盖 nested source merge：同一 section 中 env 只覆写一个叶子字段时，不会冲掉 sibling 字段。
- [ ] `pytest` 覆盖 `load_config(None)` 时 5 个 nested 子模型都能由 `default_factory` 成功构造。
- [ ] `pytest` 覆盖顶层 `app` 路径下 `--log-level` 已覆写到 `ctx.obj.config.runtime.log_level`。
- [ ] `pytest` 覆盖 `resolved_vlm()` 是唯一被断言的生效入口：未显式设置时继承 `llm.*`，显式设置后不再继承。
- [ ] `pytest` 覆盖 `acquire-lease` / `acquire-book-lock` 未传 `--ttl` 时分别取 `cfg.editor.lease_ttl_seconds` / `cfg.editor.book_exclusive_ttl_seconds`。
- [ ] `grep -rn '\.version\b' src/epubforge/ | grep -Ev 'base_version|applied_version|op_log_version|__version__|epub_version|package_version'` 返回空（确认 `Book.version` 全部改名）。
- [ ] `grep -r '"version"' src/ tests/` 在 book 序列化上下文中的出现已改为 `"op_log_version"`。
- [ ] `grep -r 'config\.toml\|config\.local\.toml' src/ docs/ AGENTS.md` 只允许出现在“**不再隐式读取**”的否定说明中，不得作为现行入口出现。
- [ ] `grep -rn 'Stage 8' src/` 返回空。
- [ ] `uv run epubforge --config config.example.toml run fixtures/zxgb.pdf` 不报错。
- [ ] 根据 D6=B：AGENTS.md Pipeline 表格与 `cli.py` `--from max=4` 一致；`pipeline.py:120` stage_timer 改 `"5 build"`。

---

## 6. 决策记录（已封版）

| 决策 | 用户选择 | 执行后果 |
|---|---|---|
| **D1** 基类命名 | **A. `StrictModel`** | R2 使用该名 |
| **D2** CLEAN_SYSTEM 处理 | **A. 彻底删除** | R3 按 "仅删字符串、保留 `_*_RULES`" 执行 |
| **D3** dispatch 范围 | **A. 只改 `_apply_op`** | R4 不动其它三处 isinstance |
| **D4** text_utils 位置 | **A. 新建 `text_utils.py`** | R5 新增该模块 |
| **D5** Config 框架 | **B. 迁到 pydantic-settings** | R6 重写，新增依赖 |
| **D6** Build stage 编号 | **B. Stage 5** | 同步 `cli.py:151` / `pipeline.py:119,120` 三处 |
| **D7** conftest 形式 | **A. pytest fixture** | R8 使用 fixture 方案 |
| **D8** editor 顶层子命令 | **B. 迁 Typer `epubforge editor <cmd>`** ⚠️ 翻转 | R9 范围扩大：13 个入口统一迁移；其中 9 个文件重命名；测试主路径迁顶层 `app`；`python -m` 入口废除 |
| **R5 额外** | 不在意 break zxgb fixture | zxgb fixture break 不阻塞执行 |
| **R6 扩展** | 只读 CLI `--config`，不隐式扫描 | R6 Step 2：删 `config.py:65` fallback 分支；`load_config(None)` 只走 defaults + env |
| **R6 定稿** | 保留嵌套 TOML + 嵌套 `Config` 子模型 | `Config` / TOML / 文档三者同构；env 显式映射到嵌套路径；`resolved_vlm()` 与 `AppContext` 收口 |
| **R15 新增** | `Book.version` → `op_log_version` | 新增独立 commit，带 schema break |

---

## 7. R6 设计定稿（已收口）

### 7.1 R6 定稿：保留嵌套 TOML + 嵌套 `Config` 子模型

本项已经定稿，不再保留待定分支。

**为什么不选扁平 `Config`**
- TOML 结构、Python 内存结构、文档结构三者同构，可显著降低理解与维护成本。
- 若保留嵌套 TOML、内部却维持扁平字段，只是把复杂度从用户接口挪进 source mapping，没有真正减少复杂度。

**目标形态**
- 顶层 `Config(BaseSettings)` 只持有 5 个 nested 子模型：`llm`、`vlm`、`runtime`、`editor`、`extract`。
- 顶层 5 个字段均使用 `Field(default_factory=...)`；子模型叶子字段自带默认值，因此 `load_config(None)` 在无 TOML、无 env 时也能直接成功。
- `config.example.toml` 保持：
  - `[llm]`
  - `[vlm]`
  - `[runtime]`
  - `[editor]`
  - `[extract]`

**source layering**
- `load_config(config_path=None)`：defaults + env only。
- `load_config(config_path=Path(...))`：defaults + that TOML + env。
- 不再隐式读取 `config.toml` / `config.local.toml`。
- 不再提供 `EPUBFORGE_CONFIG_PATH`、默认 `.env` 扫描或其它隐式配置入口。

**env 映射规则**
- 使用显式白名单映射表，把 env 名映射到嵌套路径。
- 不使用自动 nested delimiter 推导。
- 同一 section 中，env 只覆盖被点到的叶子字段，不得冲掉 sibling 字段。

**归一化与注入链**
- `resolved_vlm()` 是 VLM 实际生效配置的唯一归一化入口。
- 顶层 Typer root callback 先 `load_config()`，再把 `--log-level` 覆写到 effective `config.runtime.log_level`。
- `ctx.obj` 承载最小 `AppContext(config, log_file_override)`。
- 13 个 `epubforge editor <cmd>` 子命令统一从 `ctx.find_root().obj.config` 取 effective config。

**边界约束**
- `tool_surface.py` 不得导入 `load_config`。
- `tool_surface.py` 不得声明 CLI option default，也不得读取 `typer.Context`。
- CLI 默认值决议留在命令层：`ttl: int | None = None`，再由命令层解析成最终整数。

至此，R6 不再有剩余人类决策项。

---

## 附录 A. original v2 → human-reviewed v2 差异摘要

| 变化 | 内容 |
|---|---|
| 工作项数 | 14 → **15**（新增 R15 `Book.version` rename） |
| Commit 数 | 5 → **6**（R15 独立 commit） |
| 新依赖 | **+ `pydantic-settings`** |
| R6 | 从"保留 dataclass + 消除 CLI 硬编码" → "**迁 pydantic-settings + 只认 `--config` + 保留嵌套 TOML + 嵌套 `Config` 子模型 + 显式 env 映射表 + `resolved_vlm()` / `AppContext` 定稿**" |
| R9 | 从"仅重命名 9 个文件"扩大为 "**迁 13 个 editor 入口到 `epubforge editor <cmd>`** + 9 文件重命名 + 测试覆盖顶层 `app` 路径 + 少量真实入口 smoke + 文档全局替换" |
| R7 | 新增 `--config` 终态说明段落；`Config` / env / `resolved_vlm()` 定稿说明；`Book.version` 语义句改用 `op_log_version`；所有 `python -m epubforge.editor.*` 表述改为 `epubforge editor <cmd>` |
| §3 放弃项 | #1（config 迁移）与 #7（Book.version rename）从放弃转为正式项 |
| §7 待调研 | 从 2 点缩减到 **0 点**（子进程 config 分发因 D8=B 消除；R6 嵌套 TOML 方案已定稿） |
| §6 决策汇总 | 从"待决定"转为"已封版"表格；D5/D8 翻转明确，新增 R6 定稿行 |
| Scope | 明确补充：按新世界设计执行，不考虑兼容性、迁移路径、旧状态保留 |

---

## 附录 B. `human_input.md` 与后续对话原文存档

```
D1: A, StrictModel
D2: delete
D3: use that dispatch table pattern
D4: A, ok
D5: B, migrate
D6: B, rename
D7: A
D8: A, currently keep agentic helpers in editor submodule

R5: i don't care breaking that file, i've finished all job on it.
R6: why there are two toml files? we should only have one config toml?
R6 decision: don't infer config file to read, only read from the file specified in cli arg,
don't make config.toml / config.local.toml as default config, force user to specify.

discarded items but i think it should be done:
item 7: i prefer to use op_log_version
```

**追加指令（用户对话内，2026-04-23）**：

> python -m 子命令都改成 epubforge editor xxx 去掉 -m editor.xx

> 这项目就我一个人用 目前也没什么数据 完全无需考虑兼容性

> 需要嵌套结构。扁平太丑了

这些追加指令分别：
- 将 D8 从 A 翻转为 B，并扩大 R9 范围；
- 将兼容性 / 迁移路径 / 旧状态保留排除出本轮范围；
- 将 R6 的 TOML 形态定稿为“保留嵌套结构”。
