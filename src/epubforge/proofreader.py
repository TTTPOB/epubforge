"""Stage 6 — book-level structural proofreader (Phase 1 rolling + Phase 2 audit)."""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal, NamedTuple

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from epubforge.config import Config
from epubforge.ir.semantic import Block, Book, Equation, Figure, Footnote, Heading, Paragraph, Table
from epubforge.ir.style_registry import ALLOWED_ROLES, StyleDefinition, StyleRegistry, seed_defaults

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Edit op models
# ---------------------------------------------------------------------------

class EditOp(BaseModel):
    op: Literal["relabel", "set_lines", "set_style", "split", "merge_next"]
    block_id: str
    new_role: str | None = None
    lines: list[str] | None = None
    split_after_line_indices: list[int] | None = None
    style_id: str | None = None
    reason: str
    confidence: float


class ChapterProofreadOutput(BaseModel):
    proposals: list[EditOp]
    new_styles: list[StyleDefinition] = []


# ---------------------------------------------------------------------------
# Internal runtime types (not persisted)
# ---------------------------------------------------------------------------

class _AppliedEdit(NamedTuple):
    ch_idx: int
    block_id: str
    op: str
    variant_key: str
    reason: str


class _AuditChunk(NamedTuple):
    anchors: list[dict]
    items: list[dict]


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def _load_or_seed_registry(registry_path: Path, book_title: str) -> StyleRegistry:
    if registry_path.exists():
        reg = StyleRegistry.model_validate_json(registry_path.read_text(encoding="utf-8"))
    else:
        reg = StyleRegistry(book=book_title)
    seed_defaults(reg)
    return reg


def _chapter_pages(chapter) -> set[int]:
    return {b.provenance.page for b in chapter.blocks if hasattr(b, "provenance")}


def _has_paragraphs(chapter) -> bool:
    return any(isinstance(b, Paragraph) for b in chapter.blocks)


def _merge_new_styles(registry: StyleRegistry, new_styles: list[StyleDefinition]) -> None:
    existing_ids = {s.id for s in registry.styles}
    for style in new_styles:
        if style.id in existing_ids:
            continue
        if style.parent_role not in ALLOWED_ROLES:
            log.warning("proofreader: new style %r has unknown parent_role %r — skipping", style.id, style.parent_role)
            continue
        if style.confidence < 0.8:
            log.debug("proofreader: new style %r confidence %.2f < 0.8 — skipping", style.id, style.confidence)
            continue
        registry.styles.append(style)
        existing_ids.add(style.id)


def _cjk_join(prev: str, cont: str) -> str:
    from epubforge.assembler import _cjk_join as _join
    return _join(prev, cont)


# ---------------------------------------------------------------------------
# Block descriptor builders
# ---------------------------------------------------------------------------

def _build_descriptors_for_range(
    chapter, ch_idx: int, block_range: tuple[int, int]
) -> list[dict]:
    start, end = block_range
    descriptors = []
    for b_idx, block in enumerate(chapter.blocks[start:end], start=start):
        bid = f"{ch_idx}_{b_idx}"
        if isinstance(block, Paragraph):
            text = block.text
            if len(text) > 1000:
                text = text[:500] + " … " + text[-100:]
            descriptors.append({
                "id": bid,
                "kind": "paragraph",
                "page": block.provenance.page,
                "role": block.role,
                "text": text,
                "len": len(block.text),
            })
        elif isinstance(block, Heading):
            descriptors.append({
                "id": bid,
                "kind": "heading",
                "level": block.level,
                "page": block.provenance.page,
                "text": block.text,
            })
        elif isinstance(block, Footnote):
            descriptors.append({"id": bid, "kind": "footnote", "page": block.provenance.page})
        elif isinstance(block, Figure):
            descriptors.append({"id": bid, "kind": "figure", "page": block.provenance.page})
        elif isinstance(block, Table):
            cols = block.html.count("<th") or block.html.count("<td")
            descriptors.append({
                "id": bid,
                "kind": "table",
                "page": block.provenance.page,
                "summary": f"<table cols≈{cols}>",
            })
        elif isinstance(block, Equation):
            descriptors.append({"id": bid, "kind": "equation", "page": block.provenance.page})
    return descriptors


# ---------------------------------------------------------------------------
# Paragraph operations
# ---------------------------------------------------------------------------

def _split_paragraph(block: Paragraph, split_after_line_indices: list[int]) -> list[Paragraph]:
    lines = block.text.split("\n") if "\n" in block.text else [block.text]
    if not split_after_line_indices:
        return [block]
    indices = sorted(set(split_after_line_indices))
    segments: list[list[str]] = []
    prev = 0
    for idx in indices:
        cut = idx + 1
        if cut > len(lines) or cut <= prev:
            continue
        segments.append(lines[prev:cut])
        prev = cut
    segments.append(lines[prev:])
    segments = [s for s in segments if s]
    if len(segments) <= 1:
        return [block]
    result = []
    for seg in segments:
        text = "\n".join(seg) if len(seg) > 1 else seg[0]
        result.append(block.model_copy(update={"text": text, "display_lines": None, "style_class": None}))
    return result


def _has_footnote_marker(block: Paragraph) -> bool:
    import re
    return bool(re.search(r"\x02fn-", block.text))


def _apply_proposals(
    blocks: list[Block], proposals: list[EditOp], ch_idx: int
) -> list[Block]:
    proposals = [p for p in proposals if p.confidence >= 0.6]
    proposals.sort(key=lambda p: int(p.block_id.split("_")[1]), reverse=True)

    for p in proposals:
        parts = p.block_id.split("_")
        if len(parts) != 2:
            log.debug("proofreader: invalid block_id %r — skip", p.block_id)
            continue
        try:
            idx = int(parts[1])
        except ValueError:
            continue
        if idx >= len(blocks):
            log.debug("proofreader: block_id %r out of range (%d blocks) — skip", p.block_id, len(blocks))
            continue

        block = blocks[idx]

        if p.op == "relabel":
            if not isinstance(block, Paragraph):
                continue
            if p.new_role not in ALLOWED_ROLES:
                log.debug("proofreader: relabel %r to unknown role %r — skip", p.block_id, p.new_role)
                continue
            blocks[idx] = block.model_copy(update={"role": p.new_role})

        elif p.op == "set_lines":
            if not isinstance(block, Paragraph):
                continue
            if not p.lines:
                continue
            joined = "".join(p.lines)
            original_no_ws = "".join(block.text.split())
            joined_no_ws = "".join(joined.split())
            if joined_no_ws != original_no_ws:
                log.warning("proofreader: set_lines for %r has different text content — skip", p.block_id)
                continue
            blocks[idx] = block.model_copy(update={"display_lines": p.lines})

        elif p.op == "set_style":
            if not isinstance(block, Paragraph):
                continue
            if p.style_id:
                blocks[idx] = block.model_copy(update={"style_class": p.style_id})

        elif p.op == "split":
            if not isinstance(block, Paragraph):
                continue
            if _has_footnote_marker(block):
                log.debug("proofreader: skip split on %r — has footnote markers", p.block_id)
                continue
            new_blocks = _split_paragraph(block, p.split_after_line_indices or [])
            blocks[idx : idx + 1] = new_blocks

        elif p.op == "merge_next":
            if not isinstance(block, Paragraph):
                continue
            if idx + 1 >= len(blocks):
                continue
            nxt = blocks[idx + 1]
            if not isinstance(nxt, Paragraph):
                log.debug("proofreader: merge_next on %r would cross non-Paragraph — skip", p.block_id)
                continue
            merged_text = _cjk_join(block.text, nxt.text)
            blocks[idx : idx + 2] = [block.model_copy(update={"text": merged_text})]

    return blocks


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _estimate_descriptor_chars(block) -> int:
    if isinstance(block, Paragraph):
        text = block.text
        if len(text) > 1000:
            text = text[:500] + " … " + text[-100:]
        return len(text) + 80
    return 60


def _split_chapter_into_chunks(
    chapter, ch_idx: int, *, max_chunk_tokens: int, chars_per_token: float
) -> list[tuple[int, int]]:
    char_budget = int(max_chunk_tokens * chars_per_token) - 20_000
    if char_budget < 5_000:
        char_budget = 5_000

    chunks: list[tuple[int, int]] = []
    cur_start, cur_chars = 0, 0
    for b_idx, block in enumerate(chapter.blocks):
        desc_chars = _estimate_descriptor_chars(block)
        if cur_chars + desc_chars > char_budget and cur_start < b_idx:
            chunks.append((cur_start, b_idx))
            cur_start, cur_chars = b_idx, 0
        cur_chars += desc_chars
    chunks.append((cur_start, len(chapter.blocks)))
    return chunks


def _est_item_chars(item: dict) -> int:
    return len(item.get("text", "")) + 120


def _chunk_audit_items(
    items: list[dict], *, max_chunk_tokens: int, chars_per_token: float
) -> list[_AuditChunk]:
    char_budget = int(max_chunk_tokens * chars_per_token) - 20_000
    if char_budget < 5_000:
        char_budget = 5_000

    total_chars = sum(_est_item_chars(it) for it in items)
    if total_chars <= char_budget:
        return [_AuditChunk(anchors=[], items=items)]

    # Select anchors: up to 3 per role, longest text first (deterministic)
    by_role: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_role[it["role"]].append(it)
    anchors: list[dict] = []
    for role, group in by_role.items():
        sorted_group = sorted(group, key=lambda x: -len(x["text"]))
        anchors.extend(sorted_group[:3])
    anchor_ids = {a["id"] for a in anchors}

    # Role-rotation interleaving for remaining items
    remaining = [it for it in items if it["id"] not in anchor_ids]
    buckets: dict[str, deque] = defaultdict(deque)
    for it in remaining:
        buckets[it["role"]].append(it)
    ordered: list[dict] = []
    while any(buckets.values()):
        for role in list(buckets.keys()):
            if buckets[role]:
                ordered.append(buckets[role].popleft())

    anchor_chars = sum(_est_item_chars(a) for a in anchors)
    per_chunk_budget = max(char_budget - anchor_chars, 5_000)

    chunks: list[_AuditChunk] = []
    cur: list[dict] = []
    cur_chars = 0
    for it in ordered:
        c = _est_item_chars(it)
        if cur_chars + c > per_chunk_budget and cur:
            chunks.append(_AuditChunk(anchors=anchors, items=cur))
            cur, cur_chars = [], 0
        cur.append(it)
        cur_chars += c
    if cur:
        chunks.append(_AuditChunk(anchors=anchors, items=cur))
    return chunks or [_AuditChunk(anchors=[], items=[])]


# ---------------------------------------------------------------------------
# History & proposal routing
# ---------------------------------------------------------------------------

def _variant_key(p: EditOp) -> str:
    if p.op == "relabel":
        return f"relabel->{p.new_role}"
    if p.op == "set_style":
        return f"set_style->{p.style_id}"
    return p.op


def _summarize_history(
    history: list[_AppliedEdit], *, max_per_variant: int = 3
) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for edit in reversed(history):
        if len(groups[edit.variant_key]) < max_per_variant:
            groups[edit.variant_key].append({
                "block_id": edit.block_id,
                "reason": edit.reason,
            })
    return dict(groups)


def _apply_proposals_to_book(
    book: Book, proposals: list[EditOp], *, current_ch_idx: int
) -> list[_AppliedEdit]:
    by_chapter: dict[int, list[EditOp]] = defaultdict(list)
    for p in proposals:
        try:
            target_ch = int(p.block_id.split("_")[0])
        except (ValueError, IndexError):
            continue
        if target_ch > current_ch_idx or target_ch >= len(book.chapters):
            log.debug("proofreader: proposal targets future/invalid chapter %d — drop", target_ch)
            continue
        by_chapter[target_ch].append(p)

    applied: list[_AppliedEdit] = []
    for tgt_ch, props in by_chapter.items():
        new_blocks = _apply_proposals(list(book.chapters[tgt_ch].blocks), props, tgt_ch)
        book.chapters[tgt_ch].blocks = new_blocks
        for p in props:
            if p.confidence < 0.6:
                continue
            applied.append(_AppliedEdit(
                ch_idx=tgt_ch,
                block_id=p.block_id,
                op=p.op,
                variant_key=_variant_key(p),
                reason=p.reason,
            ))
    return applied


def _dedup_proposals_by_block_id(proposals: list[EditOp]) -> list[EditOp]:
    best: dict[str, EditOp] = {}
    conflicts: dict[str, set[str]] = defaultdict(set)
    for p in proposals:
        key = p.block_id
        if key not in best:
            best[key] = p
        else:
            existing = best[key]
            conflicts[key].add(f"{existing.op}:{existing.new_role}")
            conflicts[key].add(f"{p.op}:{p.new_role}")
            if p.confidence > existing.confidence:
                best[key] = p
    for bid, ops in conflicts.items():
        if len(ops) > 1:
            log.warning("proofreader phase2: conflicting proposals for %r %s — kept highest confidence", bid, ops)
    return list(best.values())


# ---------------------------------------------------------------------------
# Audit item collection
# ---------------------------------------------------------------------------

def _collect_audit_items(book: Book, pages: set[int] | None) -> list[dict]:
    items = []
    for ch_idx, ch in enumerate(book.chapters):
        if pages is not None and not (_chapter_pages(ch) & pages):
            continue
        for b_idx, b in enumerate(ch.blocks):
            if not isinstance(b, Paragraph):
                continue
            if b.role == "body" and not b.display_lines and not b.style_class:
                continue
            text = b.text
            if len(text) > 400:
                text = text[:300] + " … " + text[-80:]
            items.append({
                "id": f"{ch_idx}_{b_idx}",
                "page": b.provenance.page,
                "role": b.role,
                "style_class": b.style_class,
                "has_display_lines": bool(b.display_lines),
                "text": text,
            })
    return items


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _registry_json(registry: StyleRegistry) -> str:
    return json.dumps(
        [s.model_dump(exclude={"exemplar_block_ids"}) for s in registry.styles],
        ensure_ascii=False,
        indent=2,
    )


def _build_phase1_messages(
    chapter,
    ch_idx: int,
    chunk_idx: int,
    total_chunks: int,
    descriptors: list[dict],
    registry: StyleRegistry,
    summary: dict[str, list[dict]],
) -> list[ChatCompletionMessageParam]:
    from epubforge.llm.prompts import PROOFREAD_SYSTEM

    chunk_note = f" chunk {chunk_idx + 1}/{total_chunks}" if total_chunks > 1 else ""
    summary_json = json.dumps(summary, ensure_ascii=False, indent=2)
    chapter_json = json.dumps(descriptors, ensure_ascii=False, indent=2)
    user_content = (
        f"mode=label\n"
        f"# Style registry\n{_registry_json(registry)}\n\n"
        f"# Prior proposals summary (most-recent first, up to 3 per variant)\n{summary_json}\n\n"
        f'# Chapter "{chapter.title}"{chunk_note} (ch_idx={ch_idx})\n{chapter_json}'
    )
    return [
        {"role": "system", "content": PROOFREAD_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def _build_phase2_messages(
    audit_chunk: _AuditChunk,
    registry: StyleRegistry,
    *,
    chunk_idx: int,
    total: int,
) -> list[ChatCompletionMessageParam]:
    from epubforge.llm.prompts import PROOFREAD_SYSTEM

    chunk_note = f"chunk {chunk_idx + 1}/{total}" if total > 1 else "all edited paragraphs"
    if audit_chunk.anchors:
        anchors_json = json.dumps(audit_chunk.anchors, ensure_ascii=False, indent=2)
        items_json = json.dumps(audit_chunk.items, ensure_ascii=False, indent=2)
        user_content = (
            f"mode=audit\n"
            f"# Style registry\n{_registry_json(registry)}\n\n"
            f"# Anchors (shared across chunks — use for consistency comparison; "
            f"revisions on these require confidence ≥ 0.85 and must apply book-wide)\n{anchors_json}\n\n"
            f"# Audit {chunk_note} — remaining edited paragraphs under review\n{items_json}"
        )
    else:
        items_json = json.dumps(audit_chunk.items, ensure_ascii=False, indent=2)
        user_content = (
            f"mode=audit\n"
            f"# Style registry\n{_registry_json(registry)}\n\n"
            f"# Audit {chunk_note} — all edited paragraphs across the book\n{items_json}"
        )
    return [
        {"role": "system", "content": PROOFREAD_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def _build_thinking_extra(budget: int) -> dict[str, Any] | None:
    if budget <= 0:
        return None
    return {"reasoning": {"max_tokens": budget}}


def _safe_call(
    client,
    messages: list[ChatCompletionMessageParam],
    extra_body: dict[str, Any] | None,
    label: str,
) -> ChapterProofreadOutput | None:
    try:
        return client.chat_parsed(
            messages=messages,
            response_format=ChapterProofreadOutput,
            extra_body=extra_body,
        )
    except Exception:
        log.exception("proofreader: LLM call failed for %s — skip", label)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def proofread(
    semantic_path: Path,
    out_path: Path,
    registry_path: Path,
    cfg: Config,
    *,
    pages: set[int] | None = None,
) -> None:
    from epubforge.llm.client import LLMClient

    book = Book.model_validate_json(semantic_path.read_text(encoding="utf-8"))
    registry = _load_or_seed_registry(registry_path, book.title)
    client = LLMClient(cfg, use_vlm=False)

    phase1_extra = _build_thinking_extra(cfg.proofread_phase1_thinking_budget_tokens)
    phase2_extra = _build_thinking_extra(cfg.proofread_phase2_thinking_budget_tokens)
    history: list[_AppliedEdit] = []

    # ---------- Phase 1: rolling labeling ----------
    for ch_idx, chapter in enumerate(book.chapters):
        if pages is not None and not (_chapter_pages(chapter) & pages):
            log.debug("proofreader: chapter %d %r outside page filter — skip", ch_idx, chapter.title)
            continue
        if not _has_paragraphs(chapter):
            log.debug("proofreader: chapter %d %r has no paragraphs — skip", ch_idx, chapter.title)
            continue

        chunks = _split_chapter_into_chunks(
            chapter, ch_idx,
            max_chunk_tokens=cfg.proofread_max_chunk_tokens,
            chars_per_token=cfg.proofread_chars_per_token,
        )
        for chunk_idx, block_range in enumerate(chunks):
            descriptors = _build_descriptors_for_range(chapter, ch_idx, block_range)
            summary = _summarize_history(history, max_per_variant=3)
            messages = _build_phase1_messages(
                chapter, ch_idx, chunk_idx, len(chunks),
                descriptors, registry, summary,
            )
            label = f"chapter {ch_idx} {chapter.title!r} chunk {chunk_idx + 1}/{len(chunks)}"
            result = _safe_call(client, messages, phase1_extra, label)
            if result is None:
                continue
            _merge_new_styles(registry, result.new_styles)
            applied = _apply_proposals_to_book(book, result.proposals, current_ch_idx=ch_idx)
            history.extend(applied)
            log.info("proofreader phase1: %s — %d proposals applied", label, len(applied))

    # ---------- Phase 2: global consistency audit ----------
    audit_items = _collect_audit_items(book, pages)
    if not audit_items:
        log.info("proofreader phase2: no non-default paragraphs found — skip")
    else:
        audit_chunks = _chunk_audit_items(
            audit_items,
            max_chunk_tokens=cfg.proofread_max_chunk_tokens,
            chars_per_token=cfg.proofread_chars_per_token,
        )
        log.info("proofreader phase2: %d audit items → %d chunk(s)", len(audit_items), len(audit_chunks))

        all_proposals: list[EditOp] = []
        for audit_idx, audit_chunk in enumerate(audit_chunks):
            messages = _build_phase2_messages(
                audit_chunk, registry,
                chunk_idx=audit_idx, total=len(audit_chunks),
            )
            label = f"phase2 audit chunk {audit_idx + 1}/{len(audit_chunks)}"
            result = _safe_call(client, messages, phase2_extra, label)
            if result is None:
                continue
            _merge_new_styles(registry, result.new_styles)
            all_proposals.extend(result.proposals)
            log.info("proofreader: %s — %d proposals collected", label, len(result.proposals))

        if all_proposals:
            deduped = _dedup_proposals_by_block_id(all_proposals)
            _apply_proposals_to_book(book, deduped, current_ch_idx=len(book.chapters) - 1)
            log.info("proofreader phase2: applied %d deduped proposals", len(deduped))

    out_path.write_text(book.model_dump_json(indent=2), encoding="utf-8")
    registry_path.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
