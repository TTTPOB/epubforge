# 旧 proofread 备注已迁移

这里曾记录 proofread-era 的脚注配对问题样式。当前稳定架构里，这些知识已经吸收到：

- [rules/footnotes.md](./rules/footnotes.md)
- [agentic-editing-howto.md](./agentic-editing-howto.md)

请不要再把 `refine-toc -> proofread` 当成运行时阶段，也不要再以 `06_proofread.json` 作为现行工作基线。新的编辑循环以 `edit_state/`、`doctor`、`render-prompt`、`propose-op`、`apply-queue` 为中心。
