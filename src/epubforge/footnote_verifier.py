"""Stage 7 — LLM footnote pairing verification.

Reads 06_proofread.json, calls LLM per-chapter to verify callout↔FN-body pairings,
applies corrections (pair/unpair/relink/mark_orphan), writes 07_footnote_verified.json.
"""

from __future__ import annotations

import json
import logging
import typing
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal, Union

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, create_model

from epubforge.config import Config
from epubforge.editor.apply import ApplyError, BlockRef, FootnoteMutation, apply_footnote_mutation
from epubforge.fields import iter_block_text_fields
from epubforge.ir.semantic import Book, Footnote, Paragraph, Table
from epubforge.markers import (
    FN_MARKER_FULL_RE as _FN_MARKER_FULL_RE,
    has_raw_callout as _has_raw_callout,
    make_fn_marker,
    replace_first_raw as _replace_first_raw,
    replace_nth_raw as _replace_nth_raw,
    strip_markers as _shared_strip_markers,
)
from epubforge.query import find_marker_source as _query_find_marker_source

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM output schema
# ---------------------------------------------------------------------------

class FootnoteEditOp(BaseModel):
    op: Literal["pair", "unpair", "relink", "mark_orphan"]
    fn_block_id: str
    source_block_id: str | None = None
    new_source_block_id: str | None = None
    occurrence_index: int = 0
    callout: str | None = None
    reason: str
    confidence: float


class FootnoteVerifyOutput(BaseModel):
    ops: list[FootnoteEditOp]


# ---------------------------------------------------------------------------
# Block-id helpers
# ---------------------------------------------------------------------------

def _bid(ch_idx: int, b_idx: int) -> str:
    return f"{ch_idx}_{b_idx}"


def _parse_bid(bid: str) -> tuple[int, int] | None:
    parts = bid.split("_", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _is_valid_bid_loc(book: Book, loc: tuple[int, int] | None) -> bool:
    if loc is None:
        return False
    ch_idx, b_idx = loc
    if ch_idx < 0 or b_idx < 0 or ch_idx >= len(book.chapters):
        return False
    return b_idx < len(book.chapters[ch_idx].blocks)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _strip_markers(text: str) -> str:
    return _shared_strip_markers(text)


def _count_raw_callout(text: str, callout: str) -> int:
    return _strip_markers(text).count(callout)


def _block_texts(block) -> list[str]:
    """Return text fields to scan for callouts/markers."""
    if isinstance(block, Paragraph):
        return [block.text]
    if isinstance(block, Table):
        return [value for field, value in iter_block_text_fields(block) if field in {"html", "table_title"}]
    return []


def _update_block_text(block, old_text: str, new_text: str):
    """Return a copy of block with old_text replaced by new_text."""
    if isinstance(block, Paragraph):
        if block.text == old_text:
            return block.model_copy(update={"text": new_text})
    if isinstance(block, Table):
        if block.html == old_text:
            return block.model_copy(update={"html": new_text})
        if block.table_title == old_text:
            return block.model_copy(update={"table_title": new_text})
    return block


# ---------------------------------------------------------------------------
# Find which source block currently holds a FN marker
# ---------------------------------------------------------------------------

def _find_marker_source(book: Book, fn: Footnote) -> tuple[int, int] | None:
    """Return (ch_idx, b_idx) of block containing this FN's marker, or None."""
    match = _query_find_marker_source(book, fn)
    if match is None:
        return None
    return match.chapter_idx, match.block_idx


# ---------------------------------------------------------------------------
# Descriptor builders
# ---------------------------------------------------------------------------

def _build_fn_descriptor(fn: Footnote, ch_idx: int, b_idx: int, book: Book) -> dict:
    src = _find_marker_source(book, fn)
    d: dict[str, Any] = {
        "block_id": _bid(ch_idx, b_idx),
        "page": fn.provenance.page,
        "callout": fn.callout,
        "text_preview": fn.text[:120] + ("…" if len(fn.text) > 120 else ""),
        "paired": fn.paired,
        "orphan": fn.orphan,
    }
    if src:
        src_ch, src_b = src
        d["current_source_block_id"] = _bid(src_ch, src_b)
        # Include the marker's context so LLM can verify the pairing semantically
        src_block = book.chapters[src_ch].blocks[src_b]
        marker = make_fn_marker(fn.provenance.page, fn.callout)
        for text in _block_texts(src_block):
            if marker in text:
                idx = text.find(marker)
                # Strip ALL markers for readability, replace ours with [★callout]
                clean = _strip_markers(text[:idx]) + f"[★{fn.callout}]" + _strip_markers(text[idx + len(marker):])
                start = max(0, len(_strip_markers(text[:idx])) - 100)
                end = start + 100 + len(fn.callout) + 4 + 100
                d["current_source_context"] = clean[start:end]
                break
    return d


def _callout_context(text: str, callout: str, window: int = 100) -> str:
    """Return a short window of text around the first raw callout occurrence."""
    clean = _strip_markers(text)
    idx = clean.find(callout)
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(clean), idx + len(callout) + window)
    snippet = clean[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(clean):
        snippet = snippet + "…"
    return snippet


def _build_source_descriptor(block, ch_idx: int, b_idx: int, callouts: set[str]) -> dict | None:
    texts = _block_texts(block)
    if not texts:
        return None
    combined = " ".join(texts)
    raw_hits = [c for c in callouts if _has_raw_callout(combined, c)]
    marker_hits: list[str] = []
    for m in _FN_MARKER_FULL_RE.finditer(combined):
        marker_hits.append(m.group(2))

    if not raw_hits and not marker_hits:
        return None

    d: dict[str, Any] = {
        "block_id": _bid(ch_idx, b_idx),
        "kind": block.kind,
        "page": block.provenance.page,
    }
    if isinstance(block, Paragraph):
        preview = block.text
        if len(preview) > 300:
            preview = preview[:150] + " … " + preview[-80:]
        d["text_preview"] = preview
        d["cross_page"] = block.cross_page
    elif isinstance(block, Table):
        d["table_title"] = block.table_title[:80] if block.table_title else ""
        d["html"] = block.html  # full HTML — token limit enforced at chapter level

    if raw_hits:
        d["has_raw_callouts"] = raw_hits
        d["callout_contexts"] = {
            c: _callout_context(combined, c) for c in raw_hits
        }
    if marker_hits:
        d["has_markers"] = marker_hits
    return d


def _collect_chapter_descriptors(
    book: Book,
    ch_idx: int,
) -> dict:
    """Build descriptor payload for chapter ch_idx, including adjacent-chapter context.

    Also returns 'fn_bids' and 'all_bids' for schema enum constraints.
    """
    chapter = book.chapters[ch_idx]

    # Collect FN descriptors + all callout symbols in this chapter
    fn_descs: list[dict] = []
    fn_callouts: set[str] = set()
    fn_bids: list[str] = []
    for b_idx, block in enumerate(chapter.blocks):
        if isinstance(block, Footnote):
            fn_descs.append(_build_fn_descriptor(block, ch_idx, b_idx, book))
            fn_callouts.add(block.callout)
            fn_bids.append(_bid(ch_idx, b_idx))

    # Source block descriptors (paragraphs/tables with relevant callouts/markers)
    source_descs: list[dict] = []
    src_bids: list[str] = []
    for b_idx, block in enumerate(chapter.blocks):
        if isinstance(block, Footnote):
            continue
        d = _build_source_descriptor(block, ch_idx, b_idx, fn_callouts)  # type: ignore[assignment]
        if d:
            source_descs.append(d)
            src_bids.append(_bid(ch_idx, b_idx))

    # Adjacent chapter context: blocks from prev/next chapter sharing same physical pages
    ch_pages = {b.provenance.page for b in chapter.blocks if hasattr(b, "provenance")}
    adj_context: list[dict] = []
    adj_bids: list[str] = []
    for adj_idx in (ch_idx - 1, ch_idx + 1):
        if adj_idx < 0 or adj_idx >= len(book.chapters):
            continue
        adj_ch = book.chapters[adj_idx]
        for b_idx, block in enumerate(adj_ch.blocks):
            if not hasattr(block, "provenance"):
                continue
            if block.provenance.page not in ch_pages:
                continue
            bid = _bid(adj_idx, b_idx)
            d: dict[str, Any] = {
                "block_id": bid,
                "kind": block.kind,
                "page": block.provenance.page,
                "adjacent_chapter": adj_idx,
            }
            if isinstance(block, Footnote):
                d["callout"] = block.callout
                d["text_preview"] = block.text[:80]
                d["paired"] = block.paired
                fn_bids.append(bid)
            elif isinstance(block, Paragraph):
                d["text_preview"] = block.text[:120]
                adj_bids.append(bid)
            adj_context.append(d)

    return {
        "chapter_idx": ch_idx,
        "chapter_title": chapter.title,
        "footnote_bodies": fn_descs,
        "source_blocks": source_descs,
        "adjacent_context": adj_context,
        # for schema constraint (not sent to LLM directly)
        "_fn_bids": fn_bids,
        "_all_bids": src_bids + adj_bids + fn_bids,
    }


def _estimate_tokens(descriptors: dict, chars_per_token: float) -> int:
    text = json.dumps(descriptors, ensure_ascii=False)
    return int(len(text) / chars_per_token)


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_verify_messages(descriptors: dict) -> list[ChatCompletionMessageParam]:
    from epubforge.llm.prompts import FOOTNOTE_VERIFY_SYSTEM

    user_content = (
        f"chapter_idx={descriptors['chapter_idx']} title={descriptors['chapter_title']!r}\n\n"
        f"# Footnote bodies\n{json.dumps(descriptors['footnote_bodies'], ensure_ascii=False, indent=2)}\n\n"
        f"# Source blocks (paragraphs/tables containing callouts or markers)\n"
        f"{json.dumps(descriptors['source_blocks'], ensure_ascii=False, indent=2)}\n\n"
    )
    if descriptors["adjacent_context"]:
        user_content += (
            f"# Adjacent-chapter context (same physical pages)\n"
            f"{json.dumps(descriptors['adjacent_context'], ensure_ascii=False, indent=2)}\n"
        )

    return [
        {"role": "system", "content": FOOTNOTE_VERIFY_SYSTEM},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Dynamic schema constraint
# ---------------------------------------------------------------------------

def _make_literal(values: list[str]) -> type:
    """Create Union[Literal[v1], Literal[v2], ...] from a list, usable in Pydantic."""
    if not values:
        return str  # type: ignore[return-value]
    if len(values) == 1:
        return typing.Literal[values[0]]  # type: ignore[return-value]
    return Union[tuple(typing.Literal[v] for v in values)]  # type: ignore[return-value]


def _make_constrained_verify_cls(fn_bids: list[str], all_bids: list[str]) -> type[FootnoteVerifyOutput]:
    """Build a FootnoteVerifyOutput subclass with block_id fields constrained to valid enums."""
    if not fn_bids:
        return FootnoteVerifyOutput

    FnBidType = _make_literal(fn_bids)
    all_unique = list(dict.fromkeys(all_bids))  # dedup, preserve order
    SrcBidOptType = _make_literal(all_unique) | None  # type: ignore[operator]

    DynEditOp = create_model(
        "FootnoteEditOp",
        op=(Literal["pair", "unpair", "relink", "mark_orphan"], ...),
        fn_block_id=(FnBidType, ...),
        source_block_id=(SrcBidOptType, None),
        new_source_block_id=(SrcBidOptType, None),
        occurrence_index=(int, 0),
        callout=(str | None, None),
        reason=(str, ...),
        confidence=(float, ...),
    )

    return create_model("FootnoteVerifyOutput", ops=(list[DynEditOp], ...))  # type: ignore[return-value]


def _apply_fn_ops(
    book: Book,
    ops: list[FootnoteEditOp],
    ch_idx: int,
    report: list[dict],
) -> int:
    applied = 0
    chapter = book.chapters[ch_idx]

    # Deduplicate: per fn_block_id keep highest confidence
    best: dict[str, FootnoteEditOp] = {}
    for op in ops:
        prev = best.get(op.fn_block_id)
        if prev is None or op.confidence > prev.confidence:
            best[op.fn_block_id] = op

    for fn_bid, op in best.items():
        # Confidence gate
        min_conf = 0.80 if op.op == "mark_orphan" else 0.70
        if op.confidence < min_conf:
            log.debug(
                "footnote-verify: skip %s %s confidence=%.2f < %.2f",
                op.op, fn_bid, op.confidence, min_conf,
            )
            continue

        fn_loc = _parse_bid(fn_bid)
        if fn_loc is None:
            log.warning("footnote-verify: invalid fn_block_id %r — skip", fn_bid)
            continue
        fn_ch, fn_b = fn_loc

        # Locate the Footnote in the book (may be in adjacent chapter)
        if fn_ch >= len(book.chapters) or fn_b >= len(book.chapters[fn_ch].blocks):
            log.warning("footnote-verify: fn_block_id %r out of range — skip", fn_bid)
            continue
        fn_block = book.chapters[fn_ch].blocks[fn_b]
        if not isinstance(fn_block, Footnote):
            log.warning("footnote-verify: block %r is not a Footnote — skip", fn_bid)
            continue

        # Callout sanity check
        if op.callout and op.callout != fn_block.callout:
            log.warning(
                "footnote-verify: callout mismatch for %r: op.callout=%r fn.callout=%r — skip",
                fn_bid, op.callout, fn_block.callout,
            )
            continue
        if op.op == "pair" and fn_block.paired:
            log.debug("footnote-verify: pair on already-paired %r — skip", fn_bid)
            continue
        if op.op == "unpair" and not fn_block.paired:
            log.debug("footnote-verify: unpair on already-unpaired %r — skip", fn_bid)
            continue

        source_loc = _parse_bid(op.source_block_id or "") if op.source_block_id else None
        new_source_loc = _parse_bid(op.new_source_block_id or "") if op.new_source_block_id else None
        if op.source_block_id is not None and not _is_valid_bid_loc(book, source_loc):
            log.warning("footnote-verify: source %r out of range", op.source_block_id)
            source_loc = None
        if op.new_source_block_id is not None and not _is_valid_bid_loc(book, new_source_loc):
            log.warning("footnote-verify: relink new_source %r out of range", op.new_source_block_id)
            new_source_loc = None

        if op.op == "pair" and source_loc is None:
            log.warning("footnote-verify: pair missing/invalid source_block_id for %r", fn_bid)
            continue
        if op.op == "relink" and (source_loc is None or new_source_loc is None):
            log.warning("footnote-verify: relink missing/invalid source/new_source for %r", fn_bid)
            continue

        if op.op == "relink" and source_loc == new_source_loc and source_loc is not None:
            log.debug("footnote-verify: relink same-src skip (confirmation) %r", fn_bid)
            continue

        try:
            apply_footnote_mutation(
                book,
                FootnoteMutation(
                    op_name=typing.cast(Literal["pair_footnote", "unpair_footnote", "relink_footnote", "mark_orphan"], {
                        "pair": "pair_footnote",
                        "unpair": "unpair_footnote",
                        "relink": "relink_footnote",
                        "mark_orphan": "mark_orphan",
                    }[op.op]),
                    fn_ref=BlockRef(fn_ch, fn_b),
                    source_ref=BlockRef(*source_loc) if source_loc is not None else None,
                    new_source_ref=BlockRef(*new_source_loc) if new_source_loc is not None else None,
                    occurrence_index=op.occurrence_index,
                ),
                op_id=f"footnote-verify:{fn_bid}",
            )
        except ApplyError as exc:
            log.warning("footnote-verify: %s for %r", exc.reason, fn_bid)
            continue

        applied += 1
        entry = {"op": op.op, "fn_block_id": fn_bid, "reason": op.reason, "confidence": op.confidence}
        if op.source_block_id is not None:
            entry["source_block_id"] = op.source_block_id
        if op.new_source_block_id is not None:
            entry["new_source_block_id"] = op.new_source_block_id
        report.append(entry)

    return applied


# ---------------------------------------------------------------------------
# Post-apply invariant check
# ---------------------------------------------------------------------------

def _validate_paired_invariants(book: Book) -> None:
    """Downgrade paired=True footnotes that have no marker anywhere in the book."""
    for chapter in book.chapters:
        for i, block in enumerate(chapter.blocks):
            if not isinstance(block, Footnote):
                continue
            if not block.paired:
                continue
            if _find_marker_source(book, block) is None:
                log.warning(
                    "footnote-verify: invariant violation — FN page=%d callout=%r marked "
                    "paired=True but no marker found; downgrading to paired=False",
                    block.provenance.page, block.callout,
                )
                chapter.blocks[i] = block.model_copy(update={"paired": False})


# ---------------------------------------------------------------------------
# LLM safe-call wrapper
# ---------------------------------------------------------------------------

def _safe_call(
    client,
    messages: list[ChatCompletionMessageParam],
    extra_body: dict | None,
    label: str,
    response_cls: type = FootnoteVerifyOutput,
) -> FootnoteVerifyOutput | None:
    try:
        result = client.chat_parsed(
            messages=messages,
            response_format=response_cls,
            extra_body=extra_body,
        )
        # Normalise dynamic subclass back to FootnoteVerifyOutput
        if type(result) is not FootnoteVerifyOutput:
            ops = [
                FootnoteEditOp(
                    op=op.op,
                    fn_block_id=op.fn_block_id,
                    source_block_id=op.source_block_id,
                    new_source_block_id=op.new_source_block_id,
                    occurrence_index=op.occurrence_index,
                    callout=op.callout,
                    reason=op.reason,
                    confidence=op.confidence,
                )
                for op in result.ops
            ]
            return FootnoteVerifyOutput(ops=ops)
        return result
    except Exception:
        log.exception("footnote-verify: LLM call failed for %s — skip", label)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def verify_footnotes(
    src: Path,
    out: Path,
    cfg: Config,
    *,
    pages: set[int] | None = None,
    report_path: Path | None = None,
) -> None:
    from epubforge.llm.client import LLMClient
    from epubforge.observability import stage_timer

    book = Book.model_validate_json(src.read_text(encoding="utf-8"))
    client = LLMClient(cfg, use_vlm=False)
    if cfg.footnote_verify_model:
        client.model = cfg.footnote_verify_model

    _eb: dict[str, Any] = {}
    if cfg.footnote_verify_thinking_budget_tokens > 0:
        _eb["reasoning"] = {"max_tokens": cfg.footnote_verify_thinking_budget_tokens}
    if cfg.footnote_verify_providers:
        _eb["provider"] = {"order": cfg.footnote_verify_providers, "allow_fallbacks": True}
    extra_body: dict[str, Any] | None = _eb or None

    chapters_in_scope = [
        ch_idx for ch_idx, ch in enumerate(book.chapters)
        if pages is None or any(
            hasattr(b, "provenance") and b.provenance.page in pages
            for b in ch.blocks
        )
    ]
    log.info(
        "footnote-verify: %d chapters in scope (pages_filter=%s)",
        len(chapters_in_scope),
        f"{sorted(pages)[:5]}{'...' if pages and len(pages) > 5 else ''}" if pages else "all",
    )

    all_ops_report: list[dict] = []
    total_applied = 0

    # --- Phase 1: collect descriptors and validate token ceiling (sequential, reads book) ---
    work_items: list[tuple[int, dict]] = []  # (ch_idx, descriptors)
    for ch_idx in chapters_in_scope:
        chapter = book.chapters[ch_idx]
        fn_blocks = [b for b in chapter.blocks if isinstance(b, Footnote)]
        if not fn_blocks:
            log.debug("footnote-verify: chapter %d %r no footnotes — skip", ch_idx, chapter.title)
            continue
        descriptors = _collect_chapter_descriptors(book, ch_idx)
        est = _estimate_tokens(descriptors, cfg.footnote_verify_chars_per_token)
        if est > cfg.footnote_verify_max_chapter_tokens:
            raise RuntimeError(
                f"footnote-verify: chapter {ch_idx} {chapter.title!r} estimated "
                f"{est} tokens > limit {cfg.footnote_verify_max_chapter_tokens}. "
                f"Raise footnote_verify_max_chapter_tokens in config or split the chapter."
            )
        work_items.append((ch_idx, descriptors))

    # --- Phase 2: LLM calls in parallel (no shared mutable state) ---
    def _call_chapter(ch_idx: int, descriptors: dict) -> tuple[int, FootnoteVerifyOutput | None]:
        label = f"chapter {ch_idx} {book.chapters[ch_idx].title!r}"
        verify_cls = _make_constrained_verify_cls(
            descriptors["_fn_bids"], descriptors["_all_bids"]
        )
        log.debug("footnote-verify: %s schema fn_bids=%d all_bids=%d",
                  label, len(descriptors["_fn_bids"]), len(descriptors["_all_bids"]))
        messages = _build_verify_messages(descriptors)
        with stage_timer(log, f"fn-verify ch{ch_idx}"):
            result = _safe_call(client, messages, extra_body, label, response_cls=verify_cls)
        return ch_idx, result

    llm_results: dict[int, FootnoteVerifyOutput | None] = {}
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {pool.submit(_call_chapter, ch_idx, desc): ch_idx
                   for ch_idx, desc in work_items}
        for fut in as_completed(futures):
            ch_idx, result = fut.result()
            llm_results[ch_idx] = result

    # --- Phase 3: apply ops in chapter order (mutates book, must be sequential) ---
    for ch_idx, _ in work_items:
        result = llm_results.get(ch_idx)
        label = f"chapter {ch_idx} {book.chapters[ch_idx].title!r}"
        if result is None or not result.ops:
            log.info("footnote-verify: %s — no ops", label)
            continue
        chapter_report: list[dict] = []
        applied = _apply_fn_ops(book, result.ops, ch_idx, chapter_report)
        all_ops_report.extend(chapter_report)
        total_applied += applied
        log.info("footnote-verify: %s — %d ops from LLM, %d applied", label, len(result.ops), applied)

    _validate_paired_invariants(book)

    # Stats
    all_fns = [b for ch in book.chapters for b in ch.blocks if isinstance(b, Footnote)]
    paired = sum(1 for f in all_fns if f.paired)
    orphans = sum(1 for f in all_fns if f.orphan)
    log.info(
        "footnote-verify done: chapters=%d fns=%d paired=%d orphans=%d ops_applied=%d",
        len(chapters_in_scope), len(all_fns), paired, orphans, total_applied,
    )

    out.write_text(book.model_dump_json(indent=2), encoding="utf-8")

    if report_path is not None:
        report_path.write_text(
            json.dumps(all_ops_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("footnote-verify: report written to %s", report_path)
