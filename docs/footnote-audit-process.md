# 脚注审校 SOP 已迁移

旧版脚注人工审校文档已由以下新文档替代：

- 规则知识：见 [rules/footnotes.md](./rules/footnotes.md)
- supervisor 工作流：见 [agentic-editing-howto.md](./agentic-editing-howto.md)

当前不再推荐围绕 `07_footnote_verified.json` 运行人工脚本并手改 JSON。脚注相关决策应当：

1. 先由 `doctor` 暴露硬规则问题和提示
2. 由 scanner / fixer / reviewer 形成 conventions、patterns、open questions 与 op envelopes
3. 经 `propose-op` / `apply-queue` 落到 `edit_state/book.json`

如果你只是想知道脚注什么情况下可以重连、什么时候应该标记 orphan、何时必须升级为 reviewer 决策，请直接读新规则文档。
