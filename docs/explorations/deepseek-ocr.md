# DeepSeek-OCR-2: Investigation and Abandonment

**Status**: Abandoned. Do not retry on RTX 2070 or without vLLM access.

---

## Context

This investigation extended the OCR comparison workspace at `work/explore-more-approach/`, which already compared two pipelines: standard Docling + RapidOCR vs. Granite Docling VLM. The goal was to add a third pipeline — DeepSeek-OCR-2 — to compare output quality on Chinese trade paperback PDFs (the bmsf source material).

**Hardware constraint**: RTX 2070, 8 GB VRAM, Turing architecture (sm_75).

Consequences of sm_75:
- FlashAttention 2 requires sm_80+ (Ampere). Unavailable.
- FP8 compute requires sm_89+ (Ada Lovelace). Unavailable.
- vLLM's PagedAttention and custom CUDA kernels are effectively unusable at this compute capability.
- The only viable serving path is plain `transformers` with 4-bit bitsandbytes NF4 quantization and FP16 compute dtype to fit the ~7B parameter model into 8 GB.

Everything below was implemented on the `transformers` path with `BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True)`.

---

## Engineering Hurdles Solved (and How)

### `masked_scatter_` dtype mismatch (DeepSeek-OCR issue #101)

**Symptom**: `RuntimeError: masked_scatter_: expected self and source to have same dtypes but got Half and Float` raised inside `modeling_deepseekocr2.py` around line 495 during the first forward pass.

**Root cause**: The vision encoder (DeepEncoderV2, which combines a SAM backbone, a Qwen2-ViT, and a projector) returns FP32 tensors even when `torch_dtype=torch.float16` is passed to `AutoModelForCausalLM.from_pretrained`. The 4-bit quantized language model, however, expects FP16 `inputs_embeds`. The crash is a dtype collision at the point where the vision embeddings are scattered into the token embedding sequence.

**Fix approach**: runtime monkey-patching of `DeepseekOCR2Model.forward`. The implementation:

1. Called `inspect.getsource()` on the unbound method to get the exact source text.
2. Applied a single precise string substitution to insert a `.to(inputs_embeds.dtype)` cast and convert the mask to bool before the `masked_scatter_` call.
3. Compiled the modified source with `compile()` and executed it with `exec()` using the original module's `__dict__` as globals, then bound the resulting function as the new method.
4. Guarded with an `__dsocr2_patched__` marker attribute (idempotent on repeated calls).
5. Asserted `substitution_count == 1` — if upstream changes the source so the substring no longer exists, the patch aborts loudly rather than silently no-oping.

The dynamic model code module is not loaded by `AutoTokenizer.from_pretrained`; we used `transformers.dynamic_module_utils.get_class_from_dynamic_module` to force-load the class before patching.

Reference: https://github.com/deepseek-ai/DeepSeek-OCR/issues/101

### `model.infer()` hardcodes batch size 1

The official `infer()` method in `modeling_deepseekocr2.py` (approximately lines 690–940) only handles single-image input. The inputs it builds — `input_ids`, `images_seq_mask`, `images`, `attention_mask` — are all constructed for a single example.

We bypassed `infer()` entirely and called `model.generate()` directly with manually batched inputs:

- `input_ids`: left-padded to the longest sequence in the batch, stacked into `(B, L)`.
- `images_seq_mask`: stacked into `(B, L)`.
- `images`: a Python list of length B, each element being a list-of-tuples as expected by the model's `forward()`.
- `attention_mask`: constructed from the padding mask.

The model's `forward()` already iterates the batch dimension natively with `for image, crop_shape in zip(images, images_spatial_crop):` (around line 411), so batching works without any model code changes.

**Result**: ~1.4× wall-time speedup at `batch_size=2` vs. `batch_size=1`. Outputs were verified to be identical between batch sizes on clean pages. VRAM usage was ~4 GB / 8 GB at batch=2, suggesting headroom for batch=3.

### Other small fixes

- `eval_mode=True` is a required keyword argument when calling `model.infer()`. Without it the method returns `None` silently.
- The dynamic model module must be explicitly loaded before patching (see above); `AutoTokenizer` alone does not trigger the dynamic module loader.

---

## The Blocker: Hallucination and Repetition Loops

The config used for initial testing was DeepSeek-OCR v1's "Gundam" preset: `base_size=1024, image_size=640, crop_mode=True`. (This was a configuration error; see Section 5.)

Results on bmsf source pages:

**page_002 (CIP / copyright page)**: The model emitted 3602 characters of repeated `<table><tr><td>数据日期</td><td>2023.12.NN</td></tr>` for NN cycling from 15 to 100 — pure fabrication with no correspondence to the actual page content. This occurred at both `batch_size=1` and `batch_size=2`, ruling out a batching artifact.

**page_001 (cover page)**: Misread as a table structure with hallucinated text ("主标题=养者"), losing all actual cover content.

A prior experiment with a different config (`image_size=768, crop_mode=False`) had produced clean output for page_002: `"文景\n\nHorizon"` — 16 characters, correct.

The model's own generation config already sets `no_repeat_ngram_size=35`, which is aggressive. It was not enough to prevent the 3602-character repetition loop.

---

## Web Research: This Is a Known, Well-Documented Failure Mode

The repetition / hallucination loop is not an edge case. It is the central known failure mode of DeepSeek-OCR:

- **Issue #151** — A user reports a **9.2% catastrophic-failure rate** across 600 historical newspaper images. Standard generation guards (`no_repeat_ngram_size=5–7`, `repetition_penalty=1.15–1.25`, `max_new_tokens=3072`, `early_stopping=True`) **did not eliminate the failures**. https://github.com/deepseek-ai/DeepSeek-OCR/issues/151

- **Issue #250** — The model enters an infinite loop emitting dots on a single specific page, while surrounding pages are clean. https://github.com/deepseek-ai/DeepSeek-OCR/issues/250

- **Issue #31** — Repeating-token output pattern: "拖欠 的 的 的 的 ...". https://github.com/deepseek-ai/DeepSeek-OCR/issues/31

- **Maintainers ship `SKIP_REPEAT = True`** in their own vLLM config: https://github.com/deepseek-ai/DeepSeek-OCR/blob/main/DeepSeek-OCR-master/DeepSeek-OCR-vllm/config.py — the maintainers are aware and have baked in a skip mechanism at the vLLM serving layer.

- **Official mitigation** (vLLM path only): `NGramPerReqLogitsProcessor` with `ngram_size=30, window_size=90, whitelist_token_ids={128821, 128822}` (the `<td>` / `</td>` token IDs are whitelisted so legitimate table cells aren't penalized), combined with `temperature=0.0` and `skip_special_tokens=False`. See: https://docs.vllm.ai/projects/recipes/en/latest/DeepSeek/DeepSeek-OCR-2.html

- **4-bit bitsandbytes quantization amplifies the problem**: quantization noise flattens logit distributions, making degenerate repetition attractors more likely to dominate. This is an independently observed pattern for heavily quantized autoregressive models, and is consistent with our results.

---

## A Configuration Error We Made

DeepSeek-OCR-2's official default per the v2 README is:

```
base_size=1024, image_size=768, crop_mode=True
```

Note: **768**, not 640. The named presets (Tiny, Small, Base, Large, Gundam) are v1 nomenclature. DeepSeek-OCR-2 documents only a single dynamic-resolution mode with `image_size=768`.

We used Gundam (`image_size=640`) — the v1 default — rather than v2's 768. This likely contributed to the failures but is not the primary cause: the underlying repetition loop is a model-level problem that the maintainers acknowledge, and the built-in `no_repeat_ngram_size=35` was already insufficient.

---

## Decision: Abandon DeepSeek-OCR on This Hardware

1. **The repetition / hallucination failure mode is model-fundamental.** The maintainers acknowledge it, have shipped skip logic in their own tooling, and the community has documented failure rates approaching 10% on real-world document sets. It is not a misconfiguration we can tune away on the `transformers` path.

2. **The official mitigation requires vLLM.** `NGramPerReqLogitsProcessor` is a vLLM-internal component. vLLM is unusable on RTX 2070 (sm_75): no FlashAttention 2, no FP8, incompatible PagedAttention kernels.

3. **4-bit quantization (mandatory at 8 GB) makes the problem worse.** Quantization noise reduces logit sharpness and increases the probability of repetition attractors dominating. We cannot run unquantized FP16 in 8 GB.

4. **The existing pipelines are sufficient.** Standard Docling + RapidOCR and Granite Docling VLM both run reliably on the same hardware and produce acceptable quality for the comparison goals. Adding a third pipeline that produces hallucinated output 9%+ of the time would corrupt the comparison dataset.

---

## If You Want to Retry Later

Before starting, read the maintainer-acknowledged issues above. Then:

1. **Establish a baseline first.** Run the Granite Docling VLM pipeline (`compare_granite.py` in `work/explore-more-approach/scripts/`) and confirm you have a quality reference before spending time on DeepSeek-OCR.

2. **Get an Ampere or newer GPU (sm_80+).** This unlocks vLLM, which is the only path with a working mitigation (`NGramPerReqLogitsProcessor`). The RTX 3080 / A100 / H100 family all qualify. Cloud: an A10G or A100 instance will work.

3. **Use the correct v2 config**: `base_size=1024, image_size=768, crop_mode=True`. Do not use v1 presets.

4. **Prefer unquantized FP16 or BF16.** 4-bit bnb quantization amplifies the repetition problem. If VRAM allows (the full model is ~14 GB in FP16), skip quantization.

5. **Use the official vLLM mitigation**: `NGramPerReqLogitsProcessor(ngram_size=30, window_size=90, whitelist_token_ids={128821, 128822})`, `temperature=0.0`, `skip_special_tokens=False`.

6. **Prompt style**: plain `<image>\nFree OCR.` prompts reportedly outperform instruction-style prompts on this model.

7. **No git history to recover.** The runner code lived under `work/` (gitignored) and was never committed. Sections 2 and 5 of this document contain enough implementation detail to rebuild the dtype-mismatch monkey-patch and the manually-batched `generate()` call from scratch. The DeepSeek-OCR-2 modeling source is at `~/.cache/huggingface/modules/transformers_modules/deepseek-ai/DeepSeek-OCR-2/<commit>/modeling_deepseekocr2.py` after a single `AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-OCR-2", trust_remote_code=True)` call.
