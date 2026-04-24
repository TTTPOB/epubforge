# Stage 3 Skip-VLM Plan Review - Round 3

## Verdict: CONVERGED

## Actionable gaps

无。

Round 3 计划已经补齐 round 2 的 actionable gaps，并且可直接作为实现依据：

- Stage 4 freshness 明确绑定 active Stage 3 manifest sha，覆盖 VLM/skip-VLM mode 切换、`--pages` 切换、raw/pages/source 变化以及旧 `05_semantic_raw.json` 复用风险。
- `editor init` 明确可在 matching `05_semantic.json` 不存在时使用 `05_semantic_raw.json`，并验证 active manifest，一条无手工复制的 skip-VLM 到 editor 路径已经闭合。
- `source/source.pdf` 与 `editor render-page` 合同明确，且 manifest、Book extraction、editor meta、render-prompt 都只依赖 workdir-relative source PDF；没有把 `work/images` 或 figure crops 当作整页图入口。
- VLM batching 改为按 selected adjacent pages 和 `max_vlm_batch_pages` 机械切块，删除 table-like label、bbox、bottom-noise、pending tail/footnote 等 deterministic semantic/context heuristics。
- Skip-VLM extractor 明确只产出 Docling evidence、candidate roles、mechanical draft blocks 和 adjacent-page candidate edges；不做段落续接、章节/标题、列表、脚注、续表、题注归属等语义判断。
- Assemble/build/audit/editor 的边界清楚：不再自动 footnote pairing、empty-callout merge、continued-table merge、H1 chapter split，candidate roles 不被自动升级，doctor 只发 hints。
- 新 editor ops 的 schema、apply、lease、precondition/effect-precondition、transaction rollback 和 revert 行为已经具体到实现层面。
- VLM 与 skip-VLM 统一 `evidence_index.json` 合同，且 `editor vlm-page` 的 page/ref lookup、空 evidence、未选中 page 行为都有定义。
- 不保留旧 root `03_extract/unit_*.json`、旧 unit kind、旧 sidecar、旧 config/env alias、旧 editor meta 或旧 IR migration，满足“不考虑向后兼容”的约束。

## Human design decisions

无。

## Non-blocking notes

- Force rerun 同一 desired Stage 3 artifact 时，建议实现者在代码中把 deterministic `artifact_id` 与 manifest 的 `created_at`/`manifest_sha256` 关系写清楚：可以重建同一 artifact dir，但 active freshness 应继续以 manifest sha 为准。这不影响当前计划收敛。
- `render-prompt` 已要求使用 absolute work path；实现时也建议让 evidence index 中的 `render_command` 使用同一 absolute work path，便于 subagent 直接复制执行。
