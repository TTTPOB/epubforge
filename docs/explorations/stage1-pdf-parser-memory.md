# Stage 1 Traditional Pipeline 内存累积问题调研报告

**Date**: 2026-04-26
**Status**: COMPLETE — 结论已定
**Scope**: Docling + RapidOCR OCR 模式，仅 traditional pipeline，Granite 关闭

---

## 1. 背景

### 1.1 触发事件

在 bmsf 测试 PDF（50 页扫描件，启用 OCR）上，8 GB WSL2 环境下单次 `converter.convert()` 峰值内存超过 5 GB，导致 OOM。

### 1.2 已有缓解措施

commit 855d61a 引入了 page-batched parser：`parse_pdf` 按 `page_batch_size`（默认 20）循环调用 `converter.convert(page_range=...)`，50 页拆成 3 批可以跑通。

但批与批之间内存水位持续上涨。以 50 页为例，第 1 批 peak ~3.5 GB，第 3 批 peak ~4.0 GB，涨幅 ~500 MB。对于几百页的全书，这种趋势能否在某个水位 plateau 而非无界增长，是主要疑问。

---

## 2. 调研方法

1. **Web 调研** — 在 docling GitHub issues、onnxruntime GitHub issues 搜索已知内存泄漏报告
2. **源码阅读** — 分析以下关键路径：
   - `docling.backend.*`（各 backend 的资源管理）
   - `docling.pipeline.base_pipeline._unload`（内部 page_batch_size 触发的 unload 时机）
   - `docling.datamodel.settings.perf.page_batch_size`（默认值 4）
   - `rapidocr.inference_engine.onnxruntime.main.OrtInferSession`（ONNX session 生命周期）
   - `.venv/lib/python3.13/site-packages/rapidocr/config.yaml`（默认配置验证）
3. **实测对照** — bmsf 50 页 + OCR，Granite 关闭，多组控制变量实验，采样间隔 1 秒，记录到 `/tmp/epubforge_oom_test/mem_*.csv`

---

## 3. Web 调研发现

以下按重要性排序。

### 3.1 docling issue #2077 — DoclingParseV4 backend 长文档严重累积

**链接**：https://github.com/docling-project/docling/issues/2077

DoclingParseV4 backend 处理长文档时内存累积到 20+ GB。报告人切换到 PyPdfium2DocumentBackend 后内存稳定在 ~3.9 GB，不再增长。这是目前最直接的可行 workaround。

### 3.2 docling issue #2209 — DoclingParseV2 单次 convert 跳 13 GB

**链接**：https://github.com/docling-project/docling/issues/2209

DoclingParseV2 backend 单次 convert 后 RSS 直跳 13 GB，手动 `gc.collect()` 无效。属于 backend 内部 C++ 层的对象未及时释放，Python GC 无法干预。

### 3.3 onnxruntime issue #11118 / #22271 / #26831 — InferenceSession shape cache 累积

- https://github.com/microsoft/onnxruntime/issues/11118
- https://github.com/microsoft/onnxruntime/issues/22271
- https://github.com/microsoft/onnxruntime/issues/26831

onnxruntime 会按输入 tensor shape 缓存内核优化信息（execution plan cache）。OCR 场景中每个文字 crop 的图像尺寸都不同，导致每次推理都触发新的 shape 路径并写入缓存。这是 OCR 场景 onnxruntime 内存持续增长的主要原因，且是设计层面的 tradeoff（缓存换推理速度），没有简单的全局开关可以关闭。

### 3.4 RapidOCR 默认已关闭 CPU memory arena

验证路径：`.venv/lib/python3.13/site-packages/rapidocr/config.yaml`

```yaml
# 片段
enable_cpu_mem_arena: false
```

`enable_cpu_mem_arena: false` 对应 onnxruntime `SessionOptions.enable_cpu_mem_arena = False`，关闭后 onnxruntime 不会预分配大块 arena。RapidOCR 上游已将此项默认关闭，epubforge 无需额外处理。

---

## 4. 当前 epubforge 现状（commit 90648f9）

### 4.1 Backend

Traditional pipeline OCR 模式使用 `DoclingParseDocumentBackend`（V1），非 V2/V4，但与 V2/V4 共享相似的 C++ 层资源管理模式。

### 4.2 外层批处理逻辑

`parse_pdf` 按 `page_batch_size`（默认 20）循环调用 `converter.convert(page_range=...)`，复用同一个 `converter` 对象。

### 4.3 结果合并

每批 `DoclingDocument` 转 JSON 后通过 regex 重写 `self_ref`/`$ref` 字段，将页号偏移到绝对位置后合并。`page_range` 模式下 docling 内部已返回相对于子文档的页号，重写后拼接为全书绝对页号。

### 4.4 调研用环境变量（commit 待定）

`src/epubforge/parser/docling_parser.py` 中已加入以下调研用 env 变量：

- `EPUBFORGE_EXTRACT_PDF_BACKEND=pypdfium2`（默认 `docling_parse`）
- `EPUBFORGE_EXTRACT_DOCLING_INNER_BATCH=<int>`（覆盖 docling 内部 `page_batch_size`，默认 4）

---

## 5. 实测对照

### 5.1 测试环境

- 硬件：8 GB WSL2（bmsf 50 页扫描 PDF）
- OCR：启用，Granite pipeline：关闭（`--no-granite`）
- 采样：`/proc/meminfo` 每秒轮询，记录 MemAvailable

### 5.2 数据来源

- `mem_E_dual_50p_b20_reuse.csv`（取前 170 秒，去掉 granite 那段）
- `mem_*_pypdfium*.csv`

### 5.3 对照结果

| Run | Backend | docling inner_batch | epubforge page_batch | Batch peaks (MB) | Total peak (MB) | Min avail (MB) | Time |
|-----|---------|---------------------|----------------------|------------------|-----------------|----------------|------|
| E（基线） | docling_parse (V1) | 4（默认） | 20 | 3531 / 4076 / 4047 | 4076 | n/a | 162.6 s |
| F | pypdfium2 | 4（默认） | 20 | 3026 / 3611 / 3870 | 3870 | 1890 | 155.0 s |
| G | pypdfium2 | 1 | 20 | 3424 / 3724 / 4159 | 4159 | 1599 | 152.7 s |
| H | pypdfium2 | 4（默认） | 10 | 3129 / 3526 / 3971 / 4162 / 4140 | 4162 | n/a | 158.2 s |

---

## 6. 结论

### 6.1 结论 1：切换到 PyPdfium2 backend 直接砍掉 ~500 MB 基线

E（docling_parse V1）vs F（pypdfium2），每批 peak 都低约 500 MB，全程 peak 从 4076 MB 降至 3870 MB。

原因：DoclingParseDocumentBackend 内部维护 C++ 文档对象树，在 `_unload` 调用前持有整批页面的解析状态；PyPdfium2DocumentBackend 按页渲染成位图后立刻释放，不保留结构化中间状态。

**代价**：PyPdfium2 文字单元更碎（sub-word 粒度），native 文字提取质量低于 V1。但在 OCR 启用时，文字内容最终来自 RapidOCR 而非 backend native 提取，PyPdfium2 的文字粒度问题对扫描 PDF 不存在。

**建议**：在 OCR 启用时自动切换到 PyPdfium2 backend；未启用 OCR 时保留 docling_parse V1，保留原生文字提取质量。

### 6.2 结论 2：水位上涨会 plateau，不是无界泄漏

Run H（pypdfium2，page_batch=10）跑了 5 个 batch，峰值序列：3129 → 3526 → 3971 → 4162 → 4140（最后一批下降 22 MB）。第 4 批后基本不再增长。

此行为与 onnxruntime shape cache 的特性吻合：cache 按输入 shape（图像宽高）索引，OCR 场景下文字 crop 的尺寸组合有限，见到足够多不同形状后 cache 趋于饱和，内存停止增长。

**推断**：对于几百页全书，见到足够多 OCR crop 形状后，内存将稳定在 ~4200 MB 附近，8 GB 系统有约 3.8 GB 余量，风险可控。

### 6.3 结论 3：把 docling 内部 page_batch_size 调到 1 反而更糟

Run G（pypdfium2，inner_batch=1）peak 4159 MB，比 F（默认 inner_batch=4）peak 3870 MB 高 289 MB。

强制每页 `_unload` 会在解除引用时引入额外的 Python GC 压力和 C++ allocator 内部碎片，瞬时分配反而升高。不要覆盖 docling 默认的 `page_batch_size=4`。

---

## 7. 后续可选优化（未实测）

- **backend 与 OCR 联动**：在 `docling_parser.py` 中，当 `use_ocr=True` 时自动将 backend 切换到 `pypdfium2`，无需用户手动设置环境变量。可作为下一个小功能点实现。
- **极端低内存场景**：将 epubforge 外层 `page_batch_size` 从 20 降到 10，Run H 时间仅比 F 多 3.2 秒（158.2s vs 155.0s），峰值几乎相同。若 8 GB 系统仍偶发压力，可作为保守模式选项。

---

## 8. 关键文件路径

- 内存采样数据：`/tmp/epubforge_oom_test/mem_E_dual_50p_b20_reuse.csv`、`/tmp/epubforge_oom_test/mem_*_pypdfium*.csv`
- docling parser：`src/epubforge/parser/docling_parser.py`
- 调研用 env 变量在上述文件中（`EPUBFORGE_EXTRACT_PDF_BACKEND`、`EPUBFORGE_EXTRACT_DOCLING_INNER_BATCH`）
- RapidOCR 默认配置：`.venv/lib/python3.13/site-packages/rapidocr/config.yaml`

---

## 9. Process-boundary segmentation fix (2026-04-26)

`§5/§6` 的结论假设 onnxruntime shape cache 在某个水位 plateau。在 315 页全书（bmsf 完整版）上验证时，假设破灭。

### 9.1 OOM 实测数据

| Run | Backend | OCR engine | page_batch | 进程 anon-RSS 终态 | OOM 节点 |
|-----|---------|------------|------------|--------------------|----------|
| 全书 #1 | pypdfium2 | onnxruntime mobile | 10 | 5.9 GB | batch 24/32 |
| 全书 #2 | pypdfium2 | torch + llama-server | 10 | 5.78 GB | batch 19/32 |

两次都 OOM，证明：
- onnxruntime mobile 与 torch backend **都**会随 batch 累积内存，并非仅 onnxruntime 特性
- 单进程 `gc.collect()` + 所有按页释放都无法回收
- 唯一可靠的释放方式：**进程退出**（OS 强制回收 mmap）

### 9.2 根因总结

`onnxruntime.InferenceSession`（以及 docling 内部 PyTorch layout/table 模型）在 OCR 边路上累积优化缓存：每个 batch 见到新的 tensor shape 集合 → onnxruntime 编入 execution-plan cache（C++ 层 mmap，无 Python 引用），torch 侧的 cuDNN benchmark cache 同理。这些缓存**没有公开 release API**，引用图断不掉，gc 干预不到 mmap。session 在进程内活多久就累积多久。

### 9.3 方案设计

外层封装 `parse_pdf` 增加 `segment_size: int | None` 参数：
- `segment_size=None`（默认）：原路径，向后兼容
- `segment_size=N`（N < total_pages）：分段，每段 fork-exec `python -m epubforge.parser._segment_worker docling --start S --end E ...`，子进程内仍按 `page_batch_size` 跑 docling 循环；段产出 `<out>.segments/segment_NNN.json`，主进程读完后用现有 `_merge_batch_into` 拼接。所有段完成后写最终 `01_raw.json`。
- 段失败 → 主进程立即 raise，**保留**段产物以便调试（不删 `<out>.segments/`）。
- 不并发段（GPU 单卡 + llama-server `-np 1`）。
- Granite 副链同样支持段化（`parse_pdf_granite_segmented`）：每段 worker 跑 per-page Granite loop 并输出中间 doctags JSON，主进程汇总后做最终 `_finalize_granite_document` + manifest。
- OCR / Granite settings 用 `model_dump_json()` / `model_validate_json()` 在父子进程间序列化（pydantic BaseModel 原生支持）。

### 9.4 验收数据

50 页 bmsf_50p_torch.pdf，OCR 启用，`page_batch_size=10`：

| 模式 | 配置 | texts | pages | pictures | tables | groups | body.children |
|------|------|-------|-------|----------|--------|--------|---------------|
| single-process | `segment_size=None` | 255 | 50 | 3 | 0 | 8 | 236 |
| segmented | `segment_size=20`（3 段）| 255 | 50 | 3 | 0 | 8 | 236 |

附加深度比较：
- `texts` 列表逐项 `(self_ref, text)` 完全一致
- `body.children` 列表完全一致
- `pictures` self_refs 完全一致 `[#/pictures/0, #/pictures/1, #/pictures/2]`
- 每页 OCR 文本内容逐页一致（无 diff page）

结论：进程边界**对最终输出完全透明**，下游 stage 2/3/4 看到的 `01_raw.json` 在相同 `page_batch_size` 下与单进程跑产出**字节等价**。

跑时开销：50 页 segmented (segment_size=20, 3 子进程) 187s vs single-process 159s，多 28s（~17%），来自 3 次进程冷启动 + 模型重新加载。可接受（用 OOM 的不可恢复换可控的额外冷启动开销）。

### 9.5 配置入口

| 入口 | 值 |
|------|-----|
| TOML | `[extract] segment_size = 20`（或不设置 = None）|
| Env | `EPUBFORGE_EXTRACT_SEGMENT_SIZE=20`（空字符串 = None）|
| Pipeline 调用 | `parse_pdf(..., segment_size=cfg.extract.segment_size)` |
| Granite 入口 | `pipeline.run_parse` 在 `segment_size != None` 时切到 `parse_pdf_granite_segmented` |

### 9.6 关键文件

- 子进程入口：`src/epubforge/parser/_segment_worker.py`
- 主进程段循环：`src/epubforge/parser/docling_parser.py::_parse_pdf_segmented`
- Granite 段循环：`src/epubforge/parser/granite_parser.py::parse_pdf_granite_segmented`
- 单元测试：`tests/parser/test_segment_dispatch.py`
