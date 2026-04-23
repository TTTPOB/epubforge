# Review of refactor-plan-v2.md — Round 2

**Verdict**: APPROVED

## Overall
收敛度高。v2 对 round 1 的全部 Blocker/Major 都做了实质性改写（不是口头接受，源码行号与正文叙述均已修正），新增 R14 填补遗漏。仅剩若干文档数字/行号精度问题，不阻塞执行。

## v2 改动合规性检查

- **B1**（CLEAN_SYSTEM/_\*_RULES 片段保留）改得对。R3 正文第 1 步显式 "保留 `prompts.py:9-90` 的 7 个片段"，§7.1 已从"待调研"降为"已确认"。
- **B2**（R9 动机纠错）改得对。§R9 "动机修正" 明确承认前版叙述错误，删 §7.2。
- **M1**（audit HTML regex）改得对。R14 新增，归 Commit 2，范围正确（`CELL_RE` 不合并、`ROWSPAN_RE` 保留）。
- **M2**（R4 类型方案）改得对。R4 直接给出宽签名 + `assert isinstance` narrow 的示例代码。
- **M3**（R6 leases ttl）改得对。R6 第 2 步明确 "**不改** `leases.py:106/146`"。
- **M4**（R5 cjk 叙述）改得对。改为 "'cjk' 走隐式 catch-all"，方案用 `match` + `raise AssertionError`。
- **m1/m2/m3** 全部改得对。
- **D7 翻转** 合理。
- **新事实 1/2/3** 全部改得对。
- **遗漏 #3** 改得对。R2 风险节新增 commit 前 grep 检查。

## 新发现问题（全部 Minor，不阻塞）

### [Minor] n1 — R9 对 `docs/agentic-editing-howto.md` 行号偏窄
v2 写 "10-21 共 10 行"，实测全文约 29 处 kebab-case 命令引用。v2 后文 "若含引用，一并更新" 已兜底，但头部数字会误导。建议改为 "全文约 29 处" 或删具体数字。

### [Minor] n2 — R9 对 `__main__.py` "9 个字符串" 可能让人只改 9 行
实测 `__main__.py:13-25` 列 13 个 commands（含 `init`/`doctor`/`compact`/`snapshot` 4 个本就是 snake-case）。数字正确（只算需改的）但对照文件时会困惑。建议改为 "13 个字符串中的 9 个 kebab-case 条目"。

### [Minor] n3 — Executive Summary §1 字段名与 R6 不一致
§1 写 `book_exclusive_ttl`，R6 新增字段是 `book_exclusive_ttl_seconds`。小瑕疵。

## 决策点再审（v2）
8 个决策点全部有明确推荐、理由与 reviewer 共识，无弱推荐或遗漏选项。D7 翻转为 pytest fixture 合理。

## §3 放弃事项
新增第 9/10 条理由清晰合理。未见漏项。

## Final Directive
**APPROVED**

v2 已解决 round 1 全部 Blocker/Major/Minor，新增 R14 填补 M1 遗漏，§3 说明理由合理。n1/n2/n3 都是文档数字/措辞精度问题——可在执行时顺手修正或忽略，不必 v3。

**执行建议**：Commit 4 前，把 R9 的"同步清单"当白名单而非穷尽列表，用 `grep -rn 'epubforge\.editor\.[a-z]*-[a-z]*' .`（v2 §5 已列在检查清单）做最终扫尾——这比数清单上的数字更可靠。
