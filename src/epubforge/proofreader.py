"""Stage 6 — book-level structural proofreader."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from epubforge.config import Config
from epubforge.ir.semantic import Block, Book, Footnote, Heading, Paragraph, Figure, Table, Equation
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
# Helpers
# ---------------------------------------------------------------------------

def _load_or_seed_registry(registry_path: Path, book_title: str) -> StyleRegistry:
    if registry_path.exists():
        reg = StyleRegistry.model_validate_json(registry_path.read_text(encoding="utf-8"))
    else:
        reg = StyleRegistry(book=book_title)
    seed_defaults(reg)
    return reg


def _build_descriptors(chapter, ch_idx: int) -> list[dict]:
    descriptors = []
    for b_idx, block in enumerate(chapter.blocks):
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


def _build_messages(chapter, descriptors: list[dict], registry: StyleRegistry) -> list[ChatCompletionMessageParam]:
    from epubforge.llm.prompts import PROOFREAD_SYSTEM

    registry_json = json.dumps(
        [s.model_dump(exclude={"exemplar_block_ids"}) for s in registry.styles],
        ensure_ascii=False,
        indent=2,
    )
    chapter_json = json.dumps(descriptors, ensure_ascii=False, indent=2)
    user_content = (
        f"# Style registry\n{registry_json}\n\n"
        f'# Chapter "{chapter.title}"\n{chapter_json}'
    )
    return [
        {"role": "system", "content": PROOFREAD_SYSTEM},
        {"role": "user", "content": user_content},
    ]


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


def _split_paragraph(block: Paragraph, split_after_line_indices: list[int]) -> list[Paragraph]:
    """Split paragraph text at the given line indices (0-based, splitting after that line)."""
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
    # Drop low-confidence proposals
    proposals = [p for p in proposals if p.confidence >= 0.6]
    # Sort by block index descending so split/merge don't shift earlier indices
    proposals.sort(key=lambda p: int(p.block_id.split("_")[1]), reverse=True)

    valid_style_ids: set[str] = set()  # will be filled by caller context; skip validation here

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
                log.warning(
                    "proofreader: set_lines for %r has different text content — skip",
                    p.block_id,
                )
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
# Main entry point
# ---------------------------------------------------------------------------

def _chapter_pages(chapter) -> set[int]:
    return {b.provenance.page for b in chapter.blocks if hasattr(b, "provenance")}


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

    for ch_idx, chapter in enumerate(book.chapters):
        if pages is not None and not (_chapter_pages(chapter) & pages):
            log.debug("proofreader: chapter %d %r outside page filter — skip", ch_idx, chapter.title)
            continue

        descriptors = _build_descriptors(chapter, ch_idx)
        has_paragraphs = any(d["kind"] == "paragraph" for d in descriptors)
        if not has_paragraphs:
            log.debug("proofreader: chapter %d %r has no paragraphs — skip LLM", ch_idx, chapter.title)
            continue

        messages = _build_messages(chapter, descriptors, registry)
        try:
            result: ChapterProofreadOutput = client.chat_parsed(
                messages=messages,
                response_format=ChapterProofreadOutput,
            )
        except Exception:
            log.exception("proofreader: LLM call failed for chapter %d %r — skip", ch_idx, chapter.title)
            continue

        _merge_new_styles(registry, result.new_styles)
        chapter.blocks = _apply_proposals(list(chapter.blocks), result.proposals, ch_idx)

    out_path.write_text(book.model_dump_json(indent=2), encoding="utf-8")
    registry_path.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
