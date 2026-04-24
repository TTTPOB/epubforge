## xhigh Round 1 Inputs

### 综合定稿 agent 的主张

1. 计划范围必须改写为：
   - Docling 已经是常驻 Stage 1。
   - 真实变更是给 Stage 3 增加显式参数以跳过 VLM extract。

2. Blocker：
   - 原计划“Docling-Direct Extraction Plan”命名和叙事过大，`extract_mode=docling/vlm` 也应收缩为“默认仍跑现有 VLM extract，仅在显式参数下跳过它”。
   - “不做任何启发式推断”与“不修改 assembler/extract/ir”不能并存；必须要么补齐 Stage 3 合约，要么明确为降级路径并同步下游与验收。
   - 必须新增 `03_extract/` 产物隔离或清理策略，避免 VLM / skip-VLM 切换时旧 `unit_*.json` 混入。

3. Major：
   - `config.py` / env 级 `extract_mode` 应从主计划删除或降级，优先实现 runtime 参数。
   - 不要把 `_derive_image_ref()` 一致性当成图像正确性的主论证，当前 build 不按 `Figure.image_ref` 绑定图片。
   - `TableItem.export_to_html(doc, add_caption=False)` 应从“既定实现”降为“实现前验证项”。
   - 计划应明确 CLI 支持面、provider 校验、日志标识、附属产物行为。

### 红队 agent 的校正

1. 必须保留为主问题：
   - `03_extract/` 产物必须清理或隔离。
   - 跨页续接信号缺失必须保留为主问题。
   - 普通脚注不能输出成 `callout=""`，这条要单列，不要被埋进宽泛的“语义不兼容”。
   - 表格 continuation / `multi_page` / `merge_record` 语义缺失必须保留。

2. 应降级：
   - `unit.kind="vlm_group"` 不应单独作为 blocker，更多是 provenance 债务。
   - heading level / chapter splitting 风险降为样本验证项。
   - agentic workflow 恢复力不足删除或降级。
   - config/env vs runtime flag blast radius 只是 scope 选择，不应压过 correctness 主项。
   - `iterate_items(page_no=...)` 的测试逼真度只保留为实现注记。

3. 应补回：
   - 表题/题注/来源的结构化字段不能丢，不能只塞进 `html`。
   - 图片正确性的验收不能只测 `_derive_image_ref()` 一致。
   - `TableItem.export_to_html(doc, add_caption=False)` 必须写成实现时验证，不要写成已解决。
   - 若保留测试/验收段，至少覆盖：
     - 陈旧 `03_extract` 混入
     - 普通脚注 callout 提取
     - 跨页表 continuation 合并
     - 同页多图顺序绑定

### 最终裁决要求

请在 5-8 条内形成最终稳定的修订意见，要求：
- 只保留最硬、最能落到计划修订上的项；
- 删除重复、偏推测、偏产品口味的条目；
- 明确哪些原计划表述要删、哪些要改写、哪些要降级为实现验证或验收测试。
