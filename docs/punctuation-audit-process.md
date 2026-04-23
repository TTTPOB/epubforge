# 标点审校 SOP 已迁移

旧版标点清洗流程已改写为原则化规则文档：

- [rules/punctuation.md](./rules/punctuation.md)

如何在 agentic 编辑层中触发 scanner / fixer / reviewer、如何使用 `doctor` 和 op queue，请看：

- [agentic-editing-howto.md](./agentic-editing-howto.md)

当前项目不再以 `06_proofread.json` 或 `07_footnote_verified.json` 为主工作面，也不再推荐按旧人工 SOP 大段运行一次性脚本。新的做法是先形成全书 convention，再对确定性改动按 chapter 逐步提交。
