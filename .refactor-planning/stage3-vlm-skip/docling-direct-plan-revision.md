## Skip-VLM 计划修订稿

这份计划应从“Docling-Direct Extraction Plan”改写为“Skip-VLM Extract Plan”。前提要先收口：

- Docling 仍然是既有 Stage 1 parse，不是这次新增能力。
- 本次新增的是 Stage 3 的显式 skip-VLM 路径。
- 默认行为保持现有 VLM extract 不变；skip-VLM 先作为显式运行参数落地。
- agentic workflow 不能再被写成 Stage 3 正确性的兜底理由，它只能是后续修复工具，不是主合同的一部分。

### 1. 必须写进主计划的主约束

#### 1.1 Stage 3 contract 必须显式化

计划正文需要新增一节“skip-VLM 的 Stage 3 contract”，至少写清下面这些点：

- skip-VLM 路径仍然依赖 `02_pages.json` 做 TOC 过滤和页序输入。
- v1 先固定为“一页一个 unit”，不要同时引入新的 unit 聚合策略。
- `page_filter` 只允许在被选中的连续页之间产生 continuation；一旦页间有 gap，就视为 continuation 断开。
- `unit.kind="vlm_group"` 如果暂时保留，只能写成 compatibility shim，不能再写成“兼容已解决”；它会留下 provenance debt。
- `audit_notes.json`、`book_memory.json` 等 sidecar 产物必须和 active units 使用同一套隔离机制，不能继续共用根目录状态。
- 日志必须显式标出当前是 VLM 还是 skip-VLM，避免之后排障时无法区分运行路径。

#### 1.2 脚注 contract 不能降格成“尽量提取”

计划正文必须把脚注写成明确合同，而不是宽泛描述：

- 普通脚注必须提取非空 `callout`。
- 空 `callout` 只保留给“续脚注 continuation”语义，不能把所有 Docling 脚注都写成空串。
- 不只脚注正文要保留 raw callout，正文段落、表格 HTML、`table_title` 里的 inline callout 也必须原样保留，否则现有配对链路会失效。
- 需要新增“页内脚注归并规则”：当 Docling 把同一逻辑脚注拆成多条连续 `footnote` item 时，计划必须说明是否归并、归并依据是什么、最终是否产出单条逻辑 Footnote block。

#### 1.3 continuation 不能只写“不要恒为 false”

原计划里“不能把 continuation flag 恒写成 false”还不够，需要改成可执行设计：

- 写清 `first_block_continues_prev_tail` 和 `first_footnote_continues_prev_footnote` 的信号来源。
- 写清这些信号的检测范围：是否只看相邻页、是否允许在非连续 `page_filter` 下产生 continuation。
- 写清信号检测失败时的行为：是降级为 lossy mode、补 deterministic heuristics，还是直接判定 skip-VLM 路径不成立。
- 删除“不做任何启发式推断”与“不改下游”并存的写法。只有当 skip-VLM 路径真的满足现有 Stage 3 合约时，下游才可能不动；否则下游改动本来就在范围内。

#### 1.4 表格要拆成 3 个子问题来写

原计划把表格问题混成一条，无法指导实现。应拆成：

1. `continuation` 推导

- Stage 3 负责给出是否续表的信号。
- Stage 4 在看到 `continuation` 后才生成 `multi_page` 和 `merge_record`。
- 计划不能再把 Stage 3 与 Stage 4 的职责混在一起。

2. `table_title` / `caption` / 表下注释来源

- 计划要明确 `table_title` 的来源。
- 计划要明确 `caption` 的来源。
- 对“资料来源”这类表下注释，若 Docling 不能直接提供结构化信号，就必须在计划里写清采用什么规则，不要留白。
- `caption` 丢失会导致渲染与文本/审计覆盖退化；`table_title` 丢失还会影响脚注 marker 配对。

3. HTML 规范化

- 计划要新增“表格 HTML 规范化”小节。
- 由于锁定版本里 `TableItem.export_to_html(doc, add_caption=False)` 不能被直接当作已解决方案，计划必须写清：若 HTML 中仍带 `<caption>`，后续如何避免与 `table_title` / `caption` 的额外渲染重复。

#### 1.5 产物隔离不只是原则，要写机制

你已经决定走“产物隔离”，所以这部分要从原则升级成机制设计：

- skip-VLM 产物必须与常规 VLM 产物隔离，避免 `03_extract` 串读旧 `unit_*.json`。
- `unit_*.json`、`book_memory.json`、`audit_notes.json` 等 sidecar 都必须一起隔离，不能只隔离主 unit。
- 计划要明确 Stage 4 assemble 如何定位“当前这一轮”的 active Stage 3 产物，而不是继续全目录扫描。
- 计划要明确在 mode、`--pages`、unit 数量变化时，如何识别并拒绝混用旧产物。
- 建议把 active artifact set 做成显式机制，不再依赖“目录里有什么就读什么”。

#### 1.6 label 覆盖表必须补上

原计划现在只对一小部分 label 给了映射。修订稿应增加“label 覆盖表”，把所有 relevant label 分为四类：

- 明确保留
- 明确丢弃
- 明确降级为 `paragraph`
- 必须样本验证后再决定

这样可以避免 `MARKER`、`FORM`、`CHECKBOX_*`、`FIELD_*`、`HANDWRITTEN_TEXT` 等项被默认漏掉或错误塞进正文。

### 2. 实现前必须补的预检与 fail-fast

计划里需要新增一个“能力预检 / fail-fast”小节。不要等代码写完再看结果，而是先用真实 `01_raw.json` 样本验证 skip-VLM 是否能稳定拿到以下关键信号：

- 普通脚注 `callout`
- 跨页 paragraph / footnote continuation
- 表格 `continuation`
- `table_title` / `caption` / 表下注释信号
- 同页多图顺序
- `Heading.level == 1` 的章分割稳定性

同时，计划必须写清预检失败后的分支：

- 补 deterministic heuristics
- 降级为 lossy 模式并要求人工复核
- 直接停止 skip-VLM 方案

如果不把失败分支写进计划，实施时就会重新掉回“先做出来再说”的状态。

### 3. 用户可见行为要写成清晰合同

计划需要明确写出：

- 哪些命令支持 skip-VLM 参数
- 打开 skip-VLM 后是否绕过 `require_llm()` / `require_vlm()` 检查
- 日志如何标识当前运行模式
- 运行产物里如何看出这次走的是哪条 Stage 3 路径
- 默认行为仍是现有 VLM 路径，skip-VLM 只在显式开启时生效

关于 `config.py` / env 级持久化配置，建议不要在 v1 主计划里先展开。更稳妥的写法是：

- 先把 runtime / CLI contract 定稳
- 默认 VLM 路径保持不变
- 之后再决定是否沉淀到 config/env

### 4. 验收口径要从“几个单测点”升级为真实样本验证

最低验收不应只剩映射单测。修订稿应至少要求：

1. VLM 与 skip-VLM 反复切换后，`03_extract` 不会串读旧产物。
2. 普通脚注能提取非空 `callout`，页内拆分脚注能按既定规则归并，续脚注才允许空 `callout`。
3. 正文段落、表格 HTML、`table_title` 中的 raw callout 保真。
4. 跨页表在 Stage 3 给出 `continuation` 后，Stage 4 能产出 `multi_page` / `merge_record`。
5. 同页多图时，最终 EPUB 绑定顺序正确，而不只是文件名公式正确。
6. 至少拿一个“同页多图 + 文本/表格混排”样本验证 `iterate_items(page_no=...)` 的读序稳定性。
7. 至少拿一个真实样本验证 `Heading.level == 1` 的章切分是否稳定；这项可降级为样本验证，但不能完全从验证清单里消失。

### 5. 从主计划正文降级出去的内容

下面这些内容不要继续占据计划主干，应移到“实现前验证项 / 实现备注”：

- `TableItem.export_to_html(doc, add_caption=False)` 是否真能去掉 caption
- `RefItem` 用 `cref` 还是 `$ref`
- `iterate_items(page_no=...)` 的测试构造细节
- `unit.kind="vlm_group"` 的 provenance debt 细节

这些内容依然要保留，但应该作为“实现前必须验证的注记”，而不是主计划的一级结构。

### 6. 原计划里应直接删除或改写的句子

下面这些原表述应直接删掉或重写：

- “Docling-Direct Extraction Plan”
- “VLM 从 pipeline 必经阶段变为 agentic workflow 的工具”
- “不做任何启发式推断”
- “不修改 `assembler.py`, `extract.py`, `ir/semantic.py`”
- “`unit.kind = "vlm_group"` 兼容”
- “`_derive_image_ref()` 与 `docling_parser.py` 一致即可证明图像正确”
- 把 `DocItemLabel.FOOTNOTE` 直接映射成 `{"callout": ""}`
- 把 table 只写成 `{"html": ...}` 而不交代 `continuation` / `table_title` / `caption`

修订后的计划应该是：先定义 skip-VLM 的合同、产物隔离和 fail-fast，再进入代码实现，而不是先假设“Docling 大体够用，剩下靠后处理修”。
