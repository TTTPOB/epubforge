## Candidate Findings For Convergence

1. `03_extract/` 目录的 `unit_*.json`、`book_memory.json`、`audit_notes.json` 等产物没有清理策略；在 `vlm`/`skip-vlm` 间切换、改变 `--pages`、或 TOC 过滤变化时，可能把旧产物混入新的 assemble。

2. 计划用 docling 直出 unit，但把 `first_block_continues_prev_tail=false`、`first_footnote_continues_prev_footnote=false`、footnote `callout=""`、无 table continuation/merge_record 作为默认，这与 assembler 现有语义契约冲突，可能导致跨页段落、脚注、表格静默退化。

3. 空 footnote `callout` 不是轻微降级，而是会系统性破坏脚注配对、audit、EPUB 渲染和后续 agent 修复，因为现有链路按 `(page, callout)` 建索引。

4. `unit.kind="vlm_group"` 只保住 JSON 形状，没有保住 VLM 语义；同时会把 provenance 伪装成 `source="vlm"`。

5. 表格相关信息可能丢失或失真：跨页 merge 线索、`merge_record`、table title/caption/source 处理、以及 `_pair_footnotes()` 在表格标题上的扫描行为。

6. heading level 直接沿用 docling `item.level` 可能破坏 chapter 切分和 TOC，且当前 doctor / audit 没有稳定兜底这个问题。

7. “把判断推迟给 agentic workflow” 在当前 editor/tooling 下缺少真正承接面；现有 doctor/scanner/fixer/reviewer 不消费 page image/raw anchor，也没有一等 VLM 调用位。

8. 若真实需求只是“增加一个跳过 VLM extract 的参数”，那么把它做成持久化 config/env 而不是 run-time flag，会扩大 blast radius，并增加误配置和排障成本。

9. 测试计划不足，缺少 config/env/CLI 分发、API key gating、模式切换回归、assemble/build 端到端、以及 artifact cleanup 的覆盖。

10. 文档/配置示例/观测与 rollout 计划不足：`docs/usage.md`、`config.example.toml`、日志与 run artifact 中都缺少“本次是否跳过 VLM”的明确表达，也缺少验收口径与失败回退标准。

11. docling API 假设有未证实或已被现状反驳之处：`TableItem.export_to_html(..., add_caption=False)` 实际可能不会去掉 caption；`_derive_image_ref(page_no)` 未必与现有 parser 的 `prov[0].page_no` 命名一致；`captions` 可直接 `resolve(doc)`；`iterate_items(page_no=...)` 依赖 body tree，现有测试 helper 未覆盖真实形态。

12. 现有 build 端不按 `Figure.image_ref` 取图，而是按同页 figure 顺序与排序后的 PNG 绑定；所以即便 `image_ref` 命名正确，也未必能保证多图页绑定正确。
