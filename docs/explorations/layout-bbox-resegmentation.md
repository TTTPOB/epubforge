# PDF 与扫描页 bbox 重切方案调研

日期：2026-08-01

## 目标

为 PDF 到 EPUB 流水线提供快速、确定、可复现的页面布局重切能力。程序负责生成 bbox 或 polygon，VLM 只负责选择候选、判断合并与拆分、标注语义类型。VLM 不直接填写坐标。

本文覆盖以下页面：

- 带可用文本层的数字 PDF
- 干净的印刷扫描页
- 背景退化、倾斜或光照不均的扫描页
- 包含多栏、表格、图片、公式的复杂页面
- 带弯曲基线、旋转文字或特殊书写方向的历史材料

## 结论

推荐组合：

```text
PDF 字符框或 DB 概率图
    -> 确定性候选生成
    -> PP-DocLayout-S 提供语义先验
    -> VLM 选择候选
    -> 硬约束校验
    -> 稳定排序和坐标归一化
```

按页面类型选择几何来源：

| 页面类型 | 几何基础 | 推荐算法 | 主要用途 |
| --- | --- | --- | --- |
| 数字 PDF | glyph/word bbox | Docstrum 或约束文本行聚类 | 重组文本行、段落和栏目 |
| 干净扫描书页 | 二值图 | Leptonica morphology/pageseg | 快速生成文本行和文本块 |
| 退化扫描页 | 灰度图 | PaddleOCR DB/DB++ | 从概率图生成可调文本 polygon |
| 复杂杂志、论文 | 页面图像 | PP-DocLayout-S | 提供标题、正文、表格、图片等语义先验 |
| 弯曲、旋转、历史材料 | 页面图像 | Kraken baseline segmenter | 提取 baseline、polygon 和阅读顺序 |

数字 PDF 应当优先使用原始字符坐标。扫描页可以优先测试 DB 概率图方案，再使用 Leptonica 生成低成本补充候选。PP-DocLayout-S 适合参与语义判断，不适合独自承担最终几何边界。

## 数字 PDF

### 原子数据

从 PDF 文本层提取以下数据：

- 字符 bbox、Unicode 字符和字体信息
- word bbox
- 页面尺寸、旋转角和坐标变换
- 图像、矢量线和填充矩形的位置

字符框适合作为不可分割的原子。后续算法只能组合字符框，不能让最终边界穿过字符。

`pdfminer.six` 的 `LAParams` 提供以下布局参数：

- `line_overlap`：两个字符进入同一文本行所需的垂直重叠
- `char_margin`：同一文本行内允许的字符间距
- `word_margin`：插入词间空格的阈值
- `line_margin`：文本行组成同一文本框的距离阈值
- `boxes_flow`：排序时水平位置和垂直位置的权重

可以直接使用 pdfminer 的结果作为一组候选，也可以提取字符框后运行自己的聚类。自定义聚类更容易保存字符、行、块之间的完整归属关系。

### 推荐聚类

1. 根据字符中心、垂直重叠和字体高度建立邻接图。
2. 使用局部中位字符高度归一化全部距离。
3. 先组成文本行，再按垂直间距、水平重叠和缩进组成文本块。
4. 使用大面积垂直空白、矢量分隔线和栏目中心约束跨栏合并。
5. 将页眉、页脚、页码和边注留作独立候选，交给语义层处理。

Docstrum 使用连通组件的近邻距离和角度估计倾斜、行内间距及行间距。它比全局阈值更能适应同页内的局部变化。约束文本行方法也适合正文密集的书页。

## 扫描页

### 预处理

扫描页至少执行以下步骤：

1. 固定渲染 DPI 和颜色空间。
2. 检测并校正页面倾斜。
3. 估计正文字符高度或文本行高度。
4. 生成原始灰度图、Otsu 二值图和局部自适应二值图。

所有 morphology kernel、间距阈值和最小组件面积都应当根据字符高度计算。固定像素参数会随 DPI 改变行为。

### Leptonica

Leptonica 的 `pixGetRegionsBinary()` 从二值页生成三类 mask：

- halftone mask
- textline mask
- textblock mask

官方实现面向 300 至 400 PPI 的输入，并在内部缩小到约 150 至 200 PPI。调用前需要校正倾斜。该实现会使用 morphology、垂直空白和连通域组成文本块。

适用场景：

- 正文方向规则
- 页面背景干净
- 栏目之间存在清晰空白
- 需要低延迟 CPU 处理

风险：

- 固定 morphology 序列可能合并紧邻的脚注或边注
- 小字号与大标题混排时可能产生碎块
- 表格线和装饰线可能连接不相关文本

### PaddleOCR DB/DB++

DB 模型输出文本概率图。缓存该概率图后，可以重复运行廉价的后处理参数扫描：

- `det_db_thresh`：将概率图二值化为文字像素
- `det_db_box_thresh`：过滤低置信候选区域
- `det_db_unclip_ratio`：控制 polygon 向外扩展的距离
- `use_dilation`：连接断裂的前景区域
- `det_db_score_mode`：选择快速矩形评分或精细 polygon 评分

DB 主要生成文本行 polygon。系统仍需用以下特征把行聚成区域：

- 行间距与正文中位行距的比值
- 水平重叠率
- 左右缩进差
- 字号或行高差
- 栏目分隔线
- PP-DocLayout-S 的区域类型先验

DB 适合作为扫描页的首选几何模型，因为参数扫描只修改后处理。模型推理结果保持不变。

### Kraken

Kraken 提供两套页面分割器：

- 传统 segmenter 接收二值图并输出按阅读顺序排列的矩形行框
- 可训练 baseline segmenter 对像素标注 line 和 region 类型，再向量化为 baseline 与 polygon

Kraken 适合历史印刷、手写、弯曲基线、旋转文字和非标准书写方向。普通现代书页可以先测试更轻的 Leptonica 或 DB 方案。

## 复杂版式语义先验

PP-DocLayout 系列识别 23 种页面区域。论文作者报告 PP-DocLayout-S 包含 121 万参数，在 Intel Xeon Gold 6271C、8 线程、FP16 条件下耗时约 14.49 ms/page，`mAP@0.5` 为 70.9%。

PP-DocLayout-S 可以提供以下信息：

- 哪些文本行可能属于标题、正文、脚注或图片说明
- 哪些区域对应表格、图片或公式
- 哪些文本框不应跨语义区域合并
- 哪些传统 CV 结果可能漏掉非文本内容

70.9% 的 `mAP@0.5` 不足以支撑单一几何来源。系统应当保留 PDF 字符框或 DB/Leptonica 的独立覆盖路径。

## 传统算法的角色

### X-Y Cut

X-Y Cut 递归检查水平和垂直投影中的空白谷，并沿空白谷拆分矩形区域。

适合：

- Manhattan layout
- 栏目和段落之间存在宽空白
- 已完成倾斜校正

限制：

- 全局阈值容易错误拆分标题、页码和脚注
- 算法难以处理弯曲边界或互相嵌套的非矩形区域

建议将 X-Y Cut 用作 split 候选生成器。

### RLSA

RLSA 在水平和垂直方向填充短空白游程，再对结果执行连通域分析。它实现简单，参数会直接控制文字连接程度。

RLSA 对多字号和弯曲文本较敏感。它适合生成不同粒度的 merge 候选。

### Docstrum 与 Voronoi

Docstrum 根据连通组件的近邻距离和角度建立文本行与文本块。Voronoi 方法根据连通组件边界和区域邻接关系处理不规则布局。

DAS 2006 的六算法对比研究覆盖 X-Y Cut、smearing、whitespace、约束文本行、Docstrum 和 Voronoi。作者发现约束文本行、Docstrum 和 Voronoi 整体表现较好，同时指出没有一个算法在全部页面上占优。组合候选符合该研究结论。

## 候选生成协议

程序针对存在争议的局部区域生成少量候选：

```json
{
  "region_id": "r17",
  "candidates": [
    {"id": "c0", "operation": "keep"},
    {"id": "c1", "operation": "merge", "boxes": ["b12", "b13"]},
    {"id": "c2", "operation": "split", "separator": "ws_4"}
  ]
}
```

候选来源可以包括：

- 不同 `line_margin` 或归一化行距阈值产生的 merge 结果
- 不同 DB threshold 和 unclip ratio 产生的 polygon
- X-Y Cut 或 maximal whitespace 产生的 split 位置
- Leptonica textblock mask 的连通域
- PP-DocLayout-S 检测框与底层文本行的交集

程序需要给候选分配稳定 ID。相同输入、模型、参数和排序规则必须产生相同 ID。

## VLM 接口

VLM 接收页面图、候选叠加图和候选摘要。它只返回：

```json
{
  "region_id": "r17",
  "candidate_id": "c2",
  "role": "footnote",
  "confidence": "high",
  "reason": "The horizontal whitespace separates the body from footnotes."
}
```

VLM 不返回 `x0`、`y0`、`x1`、`y1`。如果 VLM 要求拆分，程序必须提前生成合法 separator 候选。这样可以避免坐标漂移，并允许缓存与审计。

## 硬约束和评分

程序在接受 VLM 选择前检查以下约束：

- 每个字符或前景连通域最多归属一个最终文本区域
- 最终区域不能切穿字符或已确认文本行
- 正文字符覆盖率不能低于候选生成前的覆盖率下限
- bbox 不能跨越已确认的栏目分隔线
- 合并结果的行距、缩进和行高必须落在页面统计范围内
- 表格、图片和正文区域不能因 bbox 合并产生大面积重叠
- 坐标必须裁剪到页面范围，并统一转换到约定坐标系

候选评分可以组合以下指标：

```text
score =
    text_coverage
  - empty_area_penalty
  - overlap_penalty
  - crossed_separator_penalty
  - line_fragment_penalty
  - reading_order_penalty
  + semantic_agreement
```

程序需要固定权重、浮点精度和 tie-break 规则。多个候选同分时，可以按参数元组和候选 ID 的字典序选择。

## 逐页参数选择

扫描质量差时，可以扫描 Otsu、Sauvola 和其他自适应二值化参数。Recognition Driven Thresholding 研究使用少量代表文本行评估不同二值化参数，再把最佳参数应用到整页。作者在 Google 1000 Books 的 740 页子集上报告约 6 个百分点的词典词比例提升。

本项目可以使用以下目标函数：

- OCR 平均字符置信度
- 可识别字符比例
- 语言模型困惑度
- 字符连通域尺寸分布的异常比例
- 下游文本行覆盖率和碎片率

参数搜索需要保存输入图像 hash、算法版本、参数和评分结果。

## 建议实验顺序

### 实验 1：数字 PDF 字符框重聚类

选择包含正文、标题、脚注和双栏的页面。提取字符框，比较 pdfminer 默认分组、局部尺度聚类和 Docstrum 风格聚类。

记录：

- 漏字符数
- 跨栏合并数
- 段落错误拆分数
- 每页耗时

### 实验 2：DB 概率图后处理扫描

固定一次 DB 推理，扫描 `thresh`、`box_thresh` 和 `unclip_ratio`。确认后处理阶段是否可以在不重复模型推理的情况下稳定产生候选。

记录：

- 文本行召回率
- polygon 互相重叠率
- 字符裁切率
- 参数扫描耗时

### 实验 3：Leptonica 与 DB 候选互补性

在普通书页、脚注密集页和图片混排页上比较两者。统计一个算法漏检而另一个算法覆盖的区域。

### 实验 4：语义模型与 VLM 选择

将 PP-DocLayout-S 检测结果映射到底层文本行。只把几何候选和叠加图交给 VLM，要求 VLM 返回候选 ID。验证相同提示和固定模型设置下的选择稳定性。

### 实验 5：端到端约束

让系统自动拒绝切穿字符、跨栏合并和文本重复归属。确认任何 VLM 输出都无法绕过这些约束。

## 来源

- [pdfminer.six Layout analysis algorithm](https://pdfminersix.readthedocs.io/en/latest/topic/converting_pdf_to_text.html)
- [pdfminer.six LAParams reference](https://pdfminersix.readthedocs.io/en/latest/reference/composable.html)
- [Leptonica pageseg.c reference](https://tpgit.github.io/Leptonica/pageseg_8c.html)
- [PaddleOCR inference parameter explanation](https://www.paddleocr.ai/v2.9/en/ppocr/blog/inference_args.html)
- [PaddleOCR DB post-processing implementation](https://github.com/PaddlePaddle/PaddleOCR/blob/main/ppocr/postprocess/db_postprocess.py)
- [PP-DocLayout paper](https://arxiv.org/html/2503.17213v1)
- [Kraken page segmentation](https://kraken.re/6.0.0/advanced/segmentation.html)
- [Performance Comparison of Six Algorithms for Page Segmentation](https://link.springer.com/chapter/10.1007/11669487_33)
- [The document spectrum for page layout analysis](https://doi.org/10.1109/34.244677)
- [OCR Based Thresholding](https://www.mva-org.jp/Proceedings/2009CD/papers/03-18.pdf)
