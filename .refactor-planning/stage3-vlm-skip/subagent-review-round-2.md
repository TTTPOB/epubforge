# Stage 3 Skip-VLM Plan Review - Round 2

## Verdict: NEEDS_REVISION

## Actionable gaps

1. Stage 4 输出 freshness 仍未和 active Stage 3 artifact 绑定，mode/pages 切换后可能复用旧 `05_semantic_raw.json`。

   当前 `pipeline.run_assemble()` 只按 `05_semantic_raw.json` 是否存在决定跳过。round 2 计划虽然要求 assemble 读取 `active_manifest.json`，但没有规定当 Stage 3 active artifact 变化时如何强制重组。这样先跑 VLM、再跑 `run --skip-vlm` 或改变 `--pages` 时，Stage 3 可能已激活新 artifact，Stage 4 却跳过并留下旧 semantic book。计划应明确：`run_assemble()` 必须读取 active manifest，并把现有 `Book.extraction.artifact_id` / manifest sha 与 active manifest 对比；不匹配时即使 `force=False` 也重跑 assemble。补测试覆盖 VLM→skip-VLM、skip-VLM→VLM、`--pages` 改变且旧 `05_semantic_raw.json` 存在时不会复用旧 Book。

2. editor init 的默认输入路径没有闭合，skip-VLM e2e 可能无法进入 editor。

   当前 `editor init` 默认读取 `work/<book>/05_semantic.json`，而 pipeline Stage 4 写的是 `05_semantic_raw.json`。round 2 测试计划要求 “`editor init` 可初始化 skip-VLM draft”，但没有说明是让 assemble 同步写 `05_semantic.json`，还是让 `editor init` 在 curated `05_semantic.json` 不存在时读取 `05_semantic_raw.json`。计划应明确一个无手工步骤的路径，并更新 `default_init_source()` / docs / tests；否则用户跑完 `run --skip-vlm` 后仍不能直接进入后续 agentic editor workflow。

3. `source_pdf` / `render-page` 的可解析路径合同仍不够具体。

   hard constraint 要求 editor/prompt 暴露可用整页视觉来源。计划新增 `editor render-page`，但 manifest 示例里的 `"source_pdf": "book.pdf"` 没有说明该路径必须如何保证从 `work_dir` 可解析，尤其当原始 PDF 在 cwd 外、以后从另一个 cwd 调用 editor、或 workdir 被移动时。计划应规定 Stage 1 或 Stage 3 要么把原 PDF 复制/硬链接到 workdir 的稳定位置并在 manifest/meta 中记录相对路径，要么记录绝对 resolved path 并在 `render-page` 中校验存在；`render-prompt` 应输出同一可执行命令。补测试覆盖从非 PDF 所在 cwd 调用 `epubforge editor render-page <work> --page N`。

4. VLM 路径仍有未清点的 deterministic batching / context heuristics。

   round 2 删除了 assemble 的语义后处理，但现有 `extract.py::_build_units()` 仍用 `_page_trailing_element_label()`、bbox bottom-noise 过滤和 `TABLE`/`PICTURE` label 来决定复杂页是否跨页成组。这不是最终写入 `continuation=true`，但它仍是 pipeline 内置规则在决定哪些页被当作可能跨页上下文交给 VLM。为满足“pipeline 只暴露上下文、证据、候选，不用启发式决定页面续接/表格语义”的硬约束，计划应明确替换该逻辑：VLM batching 只能按相邻页和配置 batch size 机械分组，或每页独立并把相邻页作为 candidate context；不得用 label/bbox/底部噪声规则决定跨页语义上下文。相应补测试断言 table-like/bottom-noise 情况不会改变 deterministic semantic batching 决策。

5. 新 editor ops 的 apply/log/revert/lease 合同还不够 implementation-ready。

   计划列出 `replace_block`、`set_paragraph_cross_page`、`set_table_metadata`，但现有 editor op 体系还需要同步定义 Pydantic schema、`EditOp` union、`apply.py` dispatcher、lease scope、precondition fields、revert behavior 和 accepted-log effect preconditions。尤其 `replace_block` 会跨 `Block` union 改 kind；计划应规定它是 irreversible，还是必须携带 `original_block` snapshot 以支持 revert，并说明是否允许保留 uid、如何处理 `new_block_uid`、如何校验 target/current kind。`set_table_metadata` 也应明确 `merge_record` 与 `multi_page` 的一致性校验。补测试覆盖 op validate、apply、lease enforcement、transaction rollback 和 revert/irreversible 行为。

6. VLM 模式的 `evidence_index.json` 内容合同仍缺失。

   计划要求所有模式都写 `evidence_index.json`，并让 `editor vlm-page` 读取该页证据，但只具体定义了 skip-VLM 的 evidence item。VLM artifact 应该复用同一 Docling evidence schema，还是只索引 VLM output blocks，目前不明确。计划应规定 `evidence_index.json` 的统一 schema、page/ref lookup 规则、VLM 与 skip-VLM 两种模式的最低字段，以及 `render-prompt`/`vlm-page` 找不到某页 evidence 时的行为。补测试覆盖两种 artifact mode 下按 page/ref 查询 evidence。

## Human design decisions

无。上述问题都可以按当前产品方向由工程计划补齐；不需要用户额外拍板。

## Non-blocking notes

- round 2 已修正 round 1 的主要硬约束问题：不再伪装 `vlm_group`、不保留 legacy fallback、三态 CLI 覆盖、显式 `render-page` 工具和 skip-VLM candidate roles 都已纳入计划。
- `detect_candidate_issues()` 建议在计划中明确落到 doctor hints 而不是阻塞性 audit errors，避免 skip-VLM 初始 draft 在未扫描前被误判为结构失败。
