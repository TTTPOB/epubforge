# Stage 3 Skip-VLM Plan Review - Round 1

## Verdict: NEEDS_REVISION

## Actionable gaps

1. CLI override 需要三态语义，否则会破坏计划声明的配置优先级。

   计划要求优先级为 CLI > env > TOML > default，但 `--skip-vlm / --no-skip-vlm` 如果实现成普通 `bool = False`，未传 CLI 参数时也会把 env/TOML 中的 `skip_vlm=true` 覆盖成 false。应明确在 `run` 和 `extract` 中使用 `bool | None` 的三态 option，仅当值不是 `None` 时才 `model_copy(update=...)`；同时补测试覆盖“未传 CLI 时保留 env/TOML”、“`--skip-vlm` 覆盖 false”、“`--no-skip-vlm` 覆盖 true”。

2. provider gating 与 `run --from 4` 的真实执行路径还没闭合。

   计划说 `run --from 4` 不再要求 provider key，但当前 `run_all()` 仍会调用 `run_extract()`，而现有 VLM `extract()` 即使复用 unit 也会先构造 `LLMClient`/打开 PDF。计划必须明确 `pipeline.run_extract()` 在 `force=False` 且 active manifest 与当前 desired artifact 匹配、unit/sidecar 完整时，应在调用任何 extractor 或构造 VLM client 之前直接 skip；如果不匹配且需要 VLM，则才要求 provider key。补测试应断言 `run --from 4` 在无 key 时不会实例化 `LLMClient`，并且缺少可复用 Stage 3 artifact 时会给出清晰错误。

3. Stage 3 artifact 的复用、失败原子性和返回合同还不够可实现。

   计划让 pipeline 写 manifest，但现有 VLM `extract()` 返回 `None`，skip-VLM 返回 `list[Path]`；实现者仍不知道统一从哪里拿 `unit_files`、sidecars、selected pages、warnings。应定义一个共享 `Stage3ExtractionResult` 或等价返回结构，并让 VLM/skip-VLM 两条路径都返回它。还应规定 artifact 只有在 `artifact_dir/manifest.json` 存在且所有 listed files 校验通过时才可复用；失败的半成品 artifact 不能在下次 `force=False` 时被按 unit 文件存在而复用。`active_manifest.json` 只能在 artifact manifest 写入并校验后原子替换。

4. `book_memory.json` sidecar 在 VLM `enable_book_memory=false` 时的 manifest 语义未定义。

   manifest 示例固定列出 `sidecars.book_memory`，skip-VLM 也要求写空 `BookMemory`，但当前 VLM 路径在 `enable_book_memory=false` 时不会写 `book_memory.json`。计划需要二选一：要么所有模式都始终写一个明确的 empty/current `BookMemory` sidecar，要么把 manifest sidecar 字段定义为可空/可省略，并让 assemble/editor 只在存在时读取。相应补 artifact manifest schema 与测试。

5. editor 的多模态承接面仍不满足目标场景。

   计划把 `page_images_dir` 指向 `work/images`，但当前 parser 只保存 figure crops，`generate_page_images=False`，`work/images` 不是可检查整页布局的页面图目录。若目标是让后续 editor/agent 在复杂页上选择调用 VLM 或使用自身多模态能力，manifest/meta/prompt 必须暴露可用的整页视觉来源：例如原始 PDF 路径 + 页码渲染指令、预渲染 page images 目录，或一个明确的 editor render-page 工具。`render_prompt()` 中也应给出当前 chapter 的复杂页及对应 PDF/page-image 路径，而不是只给 figure crop 目录。

6. table caption 中脚注 marker 的全链路处理仍有遗漏。

   计划要求 `_pair_footnotes()` 扫描 `Table.caption`，并让 `_render_chapter()` 对 caption 应用 marker，但 build 端还有 cross-chapter borrowed-footnote 预扫描，目前只扫描 `Table.html` 和 `Table.table_title`。应把 `Table.caption` 加入所有 marker 发现、替换、borrowed-footnote 归属与 salvage pass 的路径，并补测试覆盖 caption 中的同章脚注和跨章/跨页 borrowed marker。

7. strict skip-VLM contract 的错误模型需要具体化。

   计划列出 fail-fast 条件，但没有规定异常类型、错误 payload、日志字段和 artifact 激活行为。应新增一个 Stage 3 contract error 类型或等价结构，至少包含 `page`、`label/self_ref`、`condition`、`hint`；pipeline 捕获后必须保留旧 active manifest、不写新的 active manifest，并向 CLI 显示“rerun without --skip-vlm”的建议。测试要覆盖 ordinary footnote 无 callout、空 table HTML、manifest unit 缺失这三类错误的用户可见信息。

8. artifact id 的 hash 输入需要规范化，否则实现间可能不稳定。

   计划列出了 hash 内容，但没有规定 canonical serialization。应明确使用稳定 JSON（`sort_keys=True`、固定 separators）、排序后的 `page_filter`、相对路径/字符串归一化，以及 settings 中哪些字段可以为 null。否则 active artifact 命中、测试 snapshot 和跨平台行为会不稳定。

## Human design decisions

无新增必须由人拍板的设计问题。计划里的 strict-only v1、是否后续增加 editor selective VLM command 都可以按当前推荐默认值继续推进，不阻塞工程实现。

## Non-blocking notes

- `TableItem.export_to_html(doc, add_caption=False)` 已在资料中确认会忽略 `add_caption`，当前计划改为显式 HTML caption normalization 是正确方向；实现时仍应有单测锁住避免重复渲染。
- `Provenance.source="docling"` 是值得现在一起做的 schema 修正；继续伪装成 `"vlm"` 会让 editor/audit 排障更困难。
- `run --pages` 产生局部 semantic book 是现有行为延续，但文档最好明确这是调试/抽样输出，不是完整书籍输出。
