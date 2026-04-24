## High-Round Convergence Summary

### Strong consensus across high agents

1. `03_extract/` artifacts need cleanup or isolation. Reusing `unit_*.json` in place while `assemble()` scans the whole directory makes mode switches (`vlm` vs skip-VLM), `--pages`, or page-set changes unsafe.

2. The proposed docling-direct units do not preserve the existing Stage 3 semantic contract expected by `assembler.py`. The strongest evidence is:
   - `first_block_continues_prev_tail = false`
   - `first_footnote_continues_prev_footnote = false`
   - footnote `callout = ""`
   - no table continuation / merge semantics
   Current assembler/audit/render flows depend on those signals.

3. The plan overstates compatibility by using `unit.kind = "vlm_group"`. Some agents treat this as part of item 2, others as a separate provenance/semantic debt issue.

### Issues with narrower but direct evidence

4. In locked `docling-core 2.74.0`, `TableItem.export_to_html(doc, add_caption=False)` appears to ignore `add_caption`, so the plan's caption-handling assumption is unsound.

5. Build currently does not bind images by `Figure.image_ref`; it binds by per-page figure order against sorted PNG filenames. Therefore testing `_derive_image_ref()` parity alone does not prove image correctness.

6. Table title/caption/source loss may be a distinct structural issue, though some agents subsume it under the broader Stage-3 semantic-contract break.

### Issues that some high agents dropped or downgraded

7. Heading-level / chapter-splitting risk.
8. Agentic workflow currently not being a strong enough recovery surface.
9. Config/env vs runtime flag blast radius.
10. Test/docs/rollout/observability gaps.
11. `iterate_items(page_no=...)` test realism because current helpers may not populate `body.children`.

### Synthesis task for xhigh

Produce a final, concise revision memo for `/tmp/docling-direct-plan-revision.md` that:
- focuses on the strongest stable findings first;
- converts them into actionable plan revisions rather than generic criticism;
- explicitly incorporates the user's clarification: docling always runs, and the real feature is a parameter to skip VLM extract;
- avoids weak or overly speculative items unless framed as implementation-time validation notes.
