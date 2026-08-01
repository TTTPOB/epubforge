# 面向 Agent 的简化书稿编辑工作流

## 目标

epubforge 需要把 MinerU 返回的 PDF 解析结果交给视觉模型检查，并在必要时使用本地 Paddle 候选修复 bbox。模型随后修订 OCR、标点、标题、表格、脚注和阅读顺序，最终产出可用于 EPUB 的章节内容。

这条工作流优先控制模型面对的概念数量。模型只读取少量 HTML、Markdown 和页面图像，通过普通文件编辑完成工作。程序在内部保存坐标、缓存和执行记录，不要求模型填写复杂 JSON。

## 当前基础

项目已经提供以下能力：

- MinerU API 客户端可以提交 PDF、轮询任务并保存原始 ZIP。
- Stage 1 会保存稳定的源 PDF。
- Paddle 调参脚本可以生成稳定候选 ID、原始证据和 bbox 覆盖图。
- Editor projection 已验证按章节生成模型可读文本的可行性。
- EPUB builder、页面渲染、日志和 LLM 缓存可以支撑后续实现。
- 本机 OpenCode CLI 支持指定 agent、模型、variant、工作目录、附件和 JSON 事件输出。

当前 MinerU 下游仍未连通。Stage 1 输出 `01_raw.zip`，Stage 2 和 Stage 3 仍读取旧的 `01_raw.json` Docling 边界。

现有 editor 要求模型理解 `AgentOutput`、`BookPatch`、`PatchCommand`、UUID、doctor task 和结构化 memory patch。新工作流不把这些对象暴露给模型。现有 editor 可以在迁移期间保留，但不参与新的 agent 工作面。

## 总体流程

```text
MinerU ZIP
  -> 展开 content_list / middle / images
  -> 每约 10 页生成 batch.html、原页拼图、MinerU bbox 拼图
  -> Luna 检查布局
       -> 正常：保留 MinerU bbox 引用
       -> 有问题：按需生成 Paddle 候选，修改引用和 DOM 顺序
  -> 对换过 bbox、低置信文本和可疑表格执行定向 OCR
  -> Luna 按章节修订文字、标点、标题、表格和脚注
  -> 合并为 chapter HTML
  -> 校验并生成 EPUB
```

布局阶段默认每批 10 页。程序遇到明显章节边界时可以提前结束批次，不需要建立页面分类器。每个布局批次相互独立，可以并行处理。

文字修订阶段按章节组织。一个章节内的批次按页序处理，不同章节可以并行。

## 模型工作目录

每个批次使用独立目录。OpenCode 从该目录启动，模型不会看到 epubforge 源码和仓库级开发指令。

布局阶段只提供：

```text
batch.html
source-contact.jpg
overlay-contact.jpg
pages/
```

文字修订阶段增加：

```text
BOOK.md
CHAPTER.md
```

`source-contact.jpg` 展示十页原图。`overlay-contact.jpg` 展示 MinerU 当前元素、候选 ID 和阅读顺序。模型需要细看某一页时再读取 `pages/` 中的单页图像，避免一次加载过多视觉上下文。

程序可以在模型不可见的内部目录保存候选坐标、模型分数、原始 MinerU 数据和 Paddle 缓存。

## HTML IR

HTML 直接承担模型可编辑 IR。模型只需要理解四项规则：

- HTML 标签表达语义类型。
- DOM 顺序表达阅读顺序。
- `data-box` 引用 bbox 候选。
- 元素内部 HTML 保存书稿内容。

```html
<section data-page="104">
  <h2 id="p104-01" data-box="M017">第二章 方法</h2>

  <p id="p104-02" data-box="P006 P007">
    研究者收集了三类样本。<a href="#fn-104-1">1</a>
  </p>

  <figure id="p104-03" data-box="M021">
    <img src="images/figure-104-1.jpg" alt="">
    <figcaption>图 2-1 样本分布</figcaption>
  </figure>

  <aside id="fn-104-1" data-box="M023">
    1. 样本编号由采集日期组成。
  </aside>
</section>
```

候选 ID 的前缀表示几何来源：

- `M017` 指向 MinerU bbox。
- `P006` 指向 Paddle bbox。

模型不填写数值坐标。内部候选表负责把稳定 ID 映射到 bbox、页面、来源和分数。多个 `data-box` 引用表示一个语义元素覆盖多个候选区域。

模型通过普通 HTML 编辑完成所有变化：

- 调整节点顺序修复 reading order。
- 修改标签修复元素类型。
- 修改 `data-box` 更换 bbox。
- 拆分或合并节点。
- 直接修订正文和 `<table>`。

构建器最终消费章节 HTML。程序如需 Python 对象，可以在内存中解析为 `id`、页面、bbox 引用和 HTML 内容，无需持久化复杂 Book JSON。

## Bbox 策略

程序默认保留 MinerU 的 bbox、内容和 reading order。Luna 检查全部批次，但只为存在布局问题的页面请求 Paddle 候选。

处理规则保持简单：

- MinerU bbox 正确时，模型不修改 `data-box`，程序继续复用 MinerU 内容。
- Reading order 错误时，模型只调整 DOM 顺序。
- 元素类型错误时，模型只修改 HTML 标签。
- MinerU bbox 错误时，模型调用候选工具并把 `data-box` 改为 Paddle ID。
- 一个元素需要合并多个区域时，模型在 `data-box` 中列出多个候选 ID。
- 一个元素需要拆分时，模型创建多个 HTML 节点并分别引用候选 ID。

Paddle 推理和参数扫描留在工具内部。常用路径向模型展示少量候选和覆盖图。底层调参只能作为候选不足时的逃生口。

## 定向 OCR

布局通过后，程序只 OCR 以下区域：

- 模型更换过 bbox 的元素。
- MinerU 没有文字或置信度较低的元素。
- 模型或校验器标记的表格、公式、图注和脚注。
- 文字修订过程中主动要求复查的元素。

程序继续复用其余 MinerU 文本。OCR 后端可以使用 Luna 或其他视觉模型，工具把结果转换为纯文本、LaTeX 或表格 HTML。后续文字修订 agent 对照页面图像修正识别和排版错误。

## Agent 工具

OpenCode 已经提供文件读取和 `apply_patch`。epubforge 只增加三个领域命令。

### `epubforge agent inspect PAGE [--paddle]`

该命令返回原页、当前覆盖图和候选 ID。`--paddle` 为当前坏页生成或复用本地 Paddle 候选。默认调用不会运行 Paddle。

### `epubforge agent ocr FILE [REF...]`

该命令默认识别新换 bbox 和低置信区域。模型也可以指定候选引用，重新识别单个段落、表格、公式或脚注。命令返回可直接写入 HTML 的内容。

### `epubforge agent check FILE`

该命令使用 HTML parser 检查：

- HTML 是否可解析。
- 元素 ID 是否唯一。
- bbox 引用是否存在并属于正确页面。
- reading order 是否覆盖全部保留元素。
- 脚注链接是否配对。
- 表格结构是否有效。

检查成功后，命令生成最新覆盖图。失败时只返回短错误、元素 ID 和相关页面。

监督器根据通过校验的文件 diff 接受修改。普通 Git 可以保存每轮变更和回退点，不需要单独的 patch schema 或审计日志。

## 书级与章节级记忆

模型读取两份短 Markdown 文件。

`BOOK.md` 只保存跨章节可复用的信息：

- 书名、作者和语言。
- 固定译名、人名和术语。
- 全书标点和引号习惯。
- 标题层级与编号格式。
- 页眉、页脚和页码规律。
- 反复出现的 OCR 混淆。

`CHAPTER.md` 保存当前章节信息：

- 本章局部人物、术语和缩写。
- 本章标题与列表形式。
- 跨批次段落、续表和脚注衔接。
- 当前待确认问题。

布局检查不加载记忆。文字修订加载两份文件，并只更新 `CHAPTER.md`。章节完成后，监督器发起一次短模型调用，把跨章节仍有价值的事实整理进 `BOOK.md`，随后归档章节记忆。

这套方案不需要向量数据库、Pydantic memory schema 或并发 memory merge。模型通过文件名和 Markdown 标题按需获取上下文。

## OpenCode 调用方式

项目只需要定义一个短 `book-editor` agent。Prompt 应说明当前批次范围、HTML 约定、三个领域工具和完成条件，不应重复底层数据契约。

布局阶段可以这样启动 Luna：

```bash
opencode run \
  --model <luna-model-id> \
  --variant medium \
  --agent book-editor \
  --dir <batch-workspace> \
  --format json \
  -f batch.html \
  -f source-contact.jpg \
  -f overlay-contact.jpg \
  "检查并修订当前十页，最后运行 epubforge agent check batch.html"
```

文字修订阶段再附加 `BOOK.md` 和 `CHAPTER.md`。Python 监督器启动 subprocess，读取 JSON 事件、session ID、退出状态和文件 diff。第一版不需要 OpenCode SDK 或自建 agent runtime。

Agent prompt 可以控制在以下范围：

```text
处理指定页面，直接编辑 batch.html。
优先保留正确的 MinerU bbox。
发现坏 bbox 时调用 inspect --paddle 并选择候选 ID。
HTML 标签表示类型，DOM 顺序表示 reading order。
对可疑内容按需调用 ocr。
完成后运行 check，并修正全部错误。
文字修订阶段把本章新事实写入 CHAPTER.md。
```

模型不需要返回结构化 JSON。监督器只关心文件修改和 `check` 结果。

## 第一阶段验证

第一轮实现只验证一条纵向链路：

1. 从一个真实 MinerU ZIP 中提取两至十页。
2. 为这些页面生成 `batch.html`、原页图和 MinerU 覆盖图。
3. 实现 `inspect` 和 `check`。
4. 在独立工作目录中通过 `opencode run` 调用 Luna Medium。
5. 检查 Luna 是否能通过修改 HTML 标签、DOM 顺序和候选引用完成布局修复。

这轮暂不迁移现有 editor，不实现完整记忆合并，也不重写 EPUB builder。验证通过后再增加定向 OCR、章节修订和最终 HTML 构建链路。

## 设计依据

- [MinerU 输出格式](https://opendatalab.github.io/MinerU/reference/output_files/) 已提供 reading-order `content_list.json`、详细 `middle.json` 和布局可视化。
- [OpenCode Agents](https://opencode.ai/v2/docs/agents) 支持 Markdown agent、指定模型和独立权限。
- [SWE-agent ACI](https://swe-agent.com/0.7/background/aci/) 的实验支持小工具集、短反馈和编辑后立即校验。
- [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) 推荐按需读取、渐进披露和文件式短记忆。
- [Paddle PP-StructureV3](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/PP-StructureV3.html) 支持独立调用布局、OCR 和表格模块。
