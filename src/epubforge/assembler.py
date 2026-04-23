"""Stage 5 — merge cleaned + VLM outputs into Semantic IR."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from epubforge.ir.semantic import (
    Block,
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Provenance,
    Table,
    TableMergeRecord,
)
from epubforge.markers import (
    has_raw_callout as _has_raw_callout,
    replace_all_raw as _replace_all_raw,
    replace_first_raw as _replace_first_raw,
)

log = logging.getLogger(__name__)


def assemble(work_dir: Path, out_path: Path) -> None:
    """Read stage 3 extract units from *work_dir* and write Semantic IR JSON to *out_path*."""
    extract_dir = work_dir / "03_extract"
    unit_files = sorted(extract_dir.glob("unit_*.json"))
    log.info("assemble: reading %d unit files from %s", len(unit_files), extract_dir.name)

    all_blocks: list[Block] = []

    for uf in unit_files:
        data = json.loads(uf.read_text(encoding="utf-8"))
        unit_kind = data["unit"]["kind"]
        source = "llm" if unit_kind == "llm_group" else "vlm"
        default_page = data["unit"]["pages"][0]
        flag = data.get("first_block_continues_prev_tail", False)
        fn_flag = data.get("first_footnote_continues_prev_footnote", False)

        raw_blocks = data.get("blocks", [])
        parsed = [_parse_block(b, default_page, source) for b in raw_blocks]
        parsed = [b for b in parsed if b is not None]

        if fn_flag:
            first_fn_idx = next((i for i, b in enumerate(parsed) if isinstance(b, Footnote)), None)
            if first_fn_idx is not None:
                fn_cont = parsed[first_fn_idx]
                assert isinstance(fn_cont, Footnote)
                prev_fn = _find_last_footnote(all_blocks)
                if _is_continuation_plausible(prev_fn, fn_cont):
                    _append_to_last_footnote(all_blocks, fn_cont.text)
                    parsed = [b for i, b in enumerate(parsed) if i != first_fn_idx]
                else:
                    log.warning(
                        "refusing first_footnote_continues_prev_footnote for %s: "
                        "prev callout=%r cont callout=%r (callout mismatch)",
                        uf.name,
                        prev_fn.callout if prev_fn else None,
                        fn_cont.callout,
                    )
            else:
                log.warning(
                    "first_footnote_continues_prev_footnote=True but no Footnote in unit %s",
                    uf.name,
                )

        if flag and parsed:
            cont = parsed[0]
            if isinstance(cont, Paragraph):
                if not _append_to_last_paragraph(all_blocks, cont.text):
                    # Tail of previous unit is a non-Paragraph (e.g. Heading) — keep
                    # the continuation as a new cross-page paragraph rather than drop it.
                    log.warning(
                        "first_block_continues_prev_tail=True but tail is not Paragraph "
                        "(unit=%s) — keeping as new paragraph", uf.name
                    )
                    all_blocks.append(cont.model_copy(update={"cross_page": True}))
                all_blocks.extend(parsed[1:])
            else:
                log.warning(
                    "first_block_continues_prev_tail=True but first block is not Paragraph "
                    "(kind=%s, unit=%s)", cont.kind, uf.name  # type: ignore[union-attr]
                )
                all_blocks.extend(parsed)
        else:
            all_blocks.extend(parsed)

    # Merge empty-callout footnotes (VLM continuation text misplaced or fn_flag not set)
    all_blocks = _merge_empty_callout_footnotes(all_blocks)
    # Merge cross-page table continuations, absorb adjacent title/caption paragraphs, pair footnotes
    all_blocks = _merge_continued_tables(all_blocks)
    all_blocks = _absorb_table_text(all_blocks)
    all_blocks = _pair_footnotes(all_blocks)

    # Group blocks into chapters at heading-level-1 boundaries
    book = _build_book(all_blocks, work_dir)
    out_path.write_text(book.model_dump_json(indent=2), encoding="utf-8")

    n_chapters = len(book.chapters)
    n_blocks = sum(len(ch.blocks) for ch in book.chapters)
    n_footnotes = sum(
        1 for ch in book.chapters for b in ch.blocks
        if isinstance(b, Footnote) and getattr(b, "paired", False)
    )
    n_tables = sum(1 for ch in book.chapters for b in ch.blocks if isinstance(b, Table))
    log.info(
        "assemble: chapters=%d blocks=%d footnotes_paired=%d tables=%d",
        n_chapters, n_blocks, n_footnotes, n_tables,
    )


def _parse_block(raw: dict[str, Any], default_page: int, source: str) -> Block | None:
    kind = raw.get("kind", "")
    page = raw.get("page", default_page)
    prov = Provenance(page=page, source=source)  # type: ignore[arg-type]
    try:
        if kind == "paragraph":
            return Paragraph(text=raw.get("text", ""), provenance=prov)
        if kind == "heading":
            return Heading(text=raw.get("text", ""), level=raw.get("level", 1), provenance=prov)
        if kind == "footnote":
            return Footnote(callout=str(raw.get("callout", "")), text=raw.get("text", ""), provenance=prov)
        if kind == "figure":
            return Figure(caption=raw.get("caption", ""), image_ref=raw.get("image_ref"), provenance=prov)
        if kind == "table":
            return Table(
                html=raw.get("html", ""),
                table_title=str(raw.get("table_title") or ""),
                caption=str(raw.get("caption") or ""),
                continuation=bool(raw.get("continuation", False)),
                provenance=prov,
            )
        if kind == "equation":
            return Equation(latex=raw.get("latex", ""), provenance=prov)
    except Exception as exc:
        log.warning("Skipping malformed block %s: %s", raw, exc)
    return None


def _merge_continued_tables(blocks: list[Block]) -> list[Block]:
    """Merge Table blocks marked continuation=True into the preceding Table block.

    Footnote blocks at the bottom of a page may sit between a table and its
    cross-page continuation; we look back past them to find the preceding Table.

    When a merge occurs the resulting Table gets a merge_record capturing:
      segment_html, segment_pages, segment_order, column_widths
    for each contributing segment in merge order.  constituent_block_uids is
    intentionally omitted because uids are not yet stable at this stage.
    """
    result: list[Block] = []
    for block in blocks:
        if isinstance(block, Table) and block.continuation:
            prev_tbl: Table | None = None
            prev_idx: int | None = None
            for j in range(len(result) - 1, -1, -1):
                candidate = result[j]
                if isinstance(candidate, Table):
                    prev_tbl = candidate
                    prev_idx = j
                    break
                if not isinstance(candidate, Footnote):
                    break  # hit a non-footnote, non-table block — stop
            if prev_tbl is not None and prev_idx is not None:
                new_html = _splice_table_html(prev_tbl.html, block.html)
                merge_record = _build_merge_record(prev_tbl, block)
                result[prev_idx] = prev_tbl.model_copy(update={
                    "html": new_html,
                    "table_title": prev_tbl.table_title or block.table_title,
                    "caption": prev_tbl.caption or block.caption,
                    "multi_page": True,
                    "merge_record": merge_record,
                })
                log.debug("Merged continuation table (page %d) into table (page %d)", block.provenance.page, prev_tbl.provenance.page)
                continue
        result.append(block)
    return result


def _extract_tbody_html(html: str) -> str:
    """Return the content of the first <tbody> block, or the full inner table HTML."""
    tbody_match = re.search(r"<tbody\b[^>]*>(.*?)</tbody>", html, flags=re.IGNORECASE | re.DOTALL)
    if tbody_match:
        return tbody_match.group(1)
    # Fall back to stripping the outer <table> wrapper
    inner = re.sub(r"^\s*<table[^>]*>", "", html, count=1, flags=re.IGNORECASE)
    inner = re.sub(r"</table>\s*$", "", inner, count=1, flags=re.IGNORECASE)
    return inner


def _count_row_logical_width(row_html: str) -> int:
    """Return the logical column width of a single <tr> row (respects colspan)."""
    width = 0
    for cell_match in re.finditer(r"<t[dh]\b([^>]*)>", row_html, flags=re.IGNORECASE):
        attrs = cell_match.group(1)
        colspan_match = re.search(r'colspan\s*=\s*["\']?(\d+)', attrs, flags=re.IGNORECASE)
        width += int(colspan_match.group(1)) if colspan_match else 1
    return max(width, 0)


def _modal_column_width(tbody_html: str) -> int:
    """Return the most common logical row width in a tbody block (0 if no rows)."""
    rows = re.findall(r"<tr\b[^>]*>.*?</tr>", tbody_html, flags=re.IGNORECASE | re.DOTALL)
    if not rows:
        return 0
    widths: dict[int, int] = {}
    for row in rows:
        w = _count_row_logical_width(row)
        if w > 0:
            widths[w] = widths.get(w, 0) + 1
    if not widths:
        return 0
    return max(widths, key=lambda w: widths[w])


def _build_merge_record(base: Table, continuation: Table) -> TableMergeRecord:
    """Build a TableMergeRecord from a base table and a single continuation segment.

    When the base already has a merge_record (chained merge), the new segment is
    appended to the existing lists.
    """
    cont_tbody = _extract_tbody_html(continuation.html)
    cont_page = continuation.provenance.page
    cont_width = _modal_column_width(cont_tbody)

    existing = base.merge_record
    if existing is not None:
        # Extend existing record with one more segment
        next_order = len(existing.segment_order)
        return TableMergeRecord(
            segment_html=existing.segment_html + [cont_tbody],
            segment_pages=existing.segment_pages + [cont_page],
            segment_order=existing.segment_order + [next_order],
            column_widths=existing.column_widths + [cont_width],
        )

    # First merge: initialise from base + continuation
    base_tbody = _extract_tbody_html(base.html)
    base_page = base.provenance.page
    base_width = _modal_column_width(base_tbody)
    return TableMergeRecord(
        segment_html=[base_tbody, cont_tbody],
        segment_pages=[base_page, cont_page],
        segment_order=[0, 1],
        column_widths=[base_width, cont_width],
    )


def _splice_table_html(base: str, cont: str) -> str:
    """Strip outer <table> wrapper and header rows from cont, append data rows into base."""
    inner = re.sub(r"^\s*<table[^>]*>", "", cont, count=1, flags=re.IGNORECASE)
    inner = re.sub(r"</table>\s*$", "", inner, count=1, flags=re.IGNORECASE)
    # Remove <thead>…</thead> from continuation to avoid duplicate column headers
    inner = re.sub(r"<thead\b[^>]*>.*?</thead>", "", inner, flags=re.IGNORECASE | re.DOTALL)
    # Remove leading <tr> that contains only <th> cells (header row not wrapped in <thead>)
    inner = re.sub(r"^\s*<tr\b[^>]*>(\s*<th\b[^>]*>.*?</th>\s*)+</tr>", "", inner,
                   count=1, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"</table>\s*$", inner + "</table>", base, count=1, flags=re.IGNORECASE)


_TABLE_TITLE_RE = re.compile(r"^表\s*[\d一二三四五六七八九十百]+", re.UNICODE)
_TABLE_SOURCE_RE = re.compile(r"^(资料来源|来源|注|数据来源)[:：]", re.UNICODE)


def _absorb_table_text(blocks: list[Block]) -> list[Block]:
    """Move adjacent paragraphs that are table titles or source notes into the Table block."""
    result: list[Block] = list(blocks)
    i = 0
    while i < len(result):
        block = result[i]
        if not isinstance(block, Table):
            i += 1
            continue
        # Paragraph immediately before → table title
        prev_block = result[i - 1] if i > 0 else None
        if isinstance(prev_block, Paragraph) and not block.table_title and _TABLE_TITLE_RE.match(prev_block.text):
            result[i] = block.model_copy(update={"table_title": prev_block.text})
            result.pop(i - 1)
            i -= 1
            continue
        # Paragraph immediately after → source/caption
        next_block = result[i + 1] if i + 1 < len(result) else None
        if isinstance(next_block, Paragraph) and not block.caption and _TABLE_SOURCE_RE.match(next_block.text):
            result[i] = block.model_copy(update={"caption": next_block.text})
            result.pop(i + 1)
            continue
        i += 1
    return result


def _pair_footnotes(blocks: list[Block]) -> list[Block]:
    """Find each Footnote's callout char in preceding paragraphs/tables and embed a marker.

    The marker \\x02fn-PAGE-CALLOUT\\x03 is later converted to a <sup><a href=...> link by
    epub_builder. Footnotes whose callout cannot be located are left unpaired (epub_builder
    falls back to a standalone superscript reference).

    Algorithm: two-pass, LIFO stack per callout symbol.  Safe to call multiple times on the
    same block stream (already-embedded markers are skipped; already-paired footnotes are
    not re-paired).

    Pass 1 collects all callout symbols from unpaired Footnote blocks.
    Pass 2 forward-scans:
      - Heading level=1: clear entries that are neither multi_page nor on the same physical
        page as the heading; preserves multi_page (cross-page spans) and same-page sources
        (cross-chapter callouts sharing a physical page with their FN body).
      - Paragraph/Table containing raw callout C: push (block_idx, eff_page, is_multi).
        For cross_page paragraphs: eff_page = src_page + 1 (callout is in the continuation
        portion, logically on the next page). For Tables: eff_page = src_page.
      - Footnote with callout C (unpaired): select best source by 4-level priority (LIFO
        within each priority tier):
          P3 — eff_page == fn_page, not is_multi (regular same-page source)
          P2 — eff_page == fn_page, is_multi (multi same-page source)
          P1 — eff_page < fn_page, is_multi (cross-page continuation)
          P0 — eff_page < fn_page, not is_multi (layout anomaly fallback)

    Priority P3 is the strongest: once found, search stops immediately (LIFO within P3
    selects the most recent same-page source). P0 is only reached when P3/P2/P1 all
    have no candidate, enabling cross-page pairing when the book has layout anomalies
    (e.g. callout on page N, footnote body on page N+1 with no cross-page flag).
    """
    from collections import defaultdict

    result = list(blocks)

    # Pass 1: collect callout symbols from UNPAIRED footnotes only.
    callout_symbols = sorted({
        b.callout for b in result
        if isinstance(b, Footnote) and b.callout and not b.paired
    })
    if not callout_symbols:
        return result

    # Pass 2: forward scan with per-callout LIFO stacks.
    # stack entry: (block_index, effective_page, is_multi)
    # For cross_page paragraphs: effective_page = src_page + 1 (callout is in the
    # continuation portion, i.e. the next page). This prevents a cross_page paragraph
    # from competing as a same-page source for FN(src_page) while still making it the
    # natural same-page candidate for FN(src_page + 1).
    stacks: dict[str, list[tuple[int, int, bool]]] = defaultdict(list)

    for i, block in enumerate(result):
        if isinstance(block, Heading) and block.level == 1:
            # At chapter boundaries clear only non-same-page, non-multi_page entries.
            # multi_page sources may pair with next-chapter FN bodies (cross-page spans).
            # Same-page entries survive too: a callout before this heading may share a
            # physical page with the heading and its FN body (cross-chapter same-page layout).
            h_page = block.provenance.page
            for c in list(stacks.keys()):
                stacks[c] = [(j, ep, mp) for j, ep, mp in stacks[c] if mp or ep == h_page]
                if not stacks[c]:
                    del stacks[c]

        elif isinstance(block, Paragraph):
            for c in callout_symbols:
                if _has_raw_callout(block.text, c):
                    src_page = block.provenance.page
                    eff_page = src_page + 1 if block.cross_page else src_page
                    stacks[c].append((i, eff_page, block.cross_page))

        elif isinstance(block, Table):
            for c in callout_symbols:
                if _has_raw_callout(block.html, c) or _has_raw_callout(block.table_title, c):
                    stacks[c].append((i, block.provenance.page, block.multi_page))

        elif isinstance(block, Footnote) and not block.paired:
            callout = block.callout
            fn_page = block.provenance.page
            fn_marker = f"\x02fn-{fn_page}-{callout}\x03"

            stack = stacks.get(callout)
            if not stack:
                continue

            # Priority-based LIFO: scan all entries, select the highest-priority source.
            # Within the same priority, LIFO gives the most-recent entry (scan newest first).
            best_k, best_priority = None, -1
            for k in range(len(stack) - 1, -1, -1):
                j, eff_page, is_multi = stack[k]
                source = result[j]

                # Only check sources with the callout still present
                if isinstance(source, Paragraph):
                    if not _has_raw_callout(source.text, callout):
                        continue
                elif isinstance(source, Table):
                    if not (_has_raw_callout(source.html, callout) or _has_raw_callout(source.table_title, callout)):
                        continue
                else:
                    continue

                if eff_page > fn_page:
                    continue  # effective page is after this FN — not a candidate
                elif eff_page == fn_page and not is_multi:
                    priority = 3  # P3: same effective-page, regular (non-multi) source
                elif eff_page == fn_page:
                    priority = 2  # P2: same effective-page, multi source
                elif is_multi:
                    priority = 1  # P1: earlier effective-page, multi (cross-page continuation)
                else:
                    if fn_page - eff_page > 1:
                        continue  # P0 only fires for adjacent pages (layout anomaly)
                    priority = 0  # P0: earlier effective-page, regular (layout anomaly fallback)

                if priority > best_priority:
                    best_priority, best_k = priority, k
                    if priority == 3:
                        break  # P3 is maximum; no need to scan further

            if best_k is None:
                # Second-chance scan: a cross_page paragraph's callout may be in its
                # SOURCE portion (page = provenance.page), not the continuation portion
                # (eff_page = provenance.page + 1).  When the first pass found nothing,
                # retry multi_page Paragraphs using their source page.
                for k in range(len(stack) - 1, -1, -1):
                    j2, _ep, is_m = stack[k]
                    if not is_m:
                        continue
                    src = result[j2]
                    if not isinstance(src, Paragraph):
                        continue  # Tables: eff_page == provenance.page, already tried
                    if src.provenance.page != fn_page:
                        continue
                    if not _has_raw_callout(src.text, callout):
                        continue
                    best_k = k
                    best_priority = 2  # treat as P2 (same-page, multi)
                    break

            if best_k is None:
                continue

            j, _, _ = stack[best_k]
            source = result[j]
            if isinstance(source, Paragraph):
                result[j] = source.model_copy(update={
                    "text": _replace_first_raw(source.text, callout, fn_marker)
                })
                log.debug(
                    "Paired footnote callout %r (page %d) into paragraph block %d (P%d)",
                    callout, fn_page, j, best_priority,
                )
            elif isinstance(source, Table):
                # Callout may be in html body or in table_title — replace in whichever has it
                new_html = source.html
                new_title = source.table_title
                if _has_raw_callout(source.html, callout):
                    new_html = _replace_all_raw(source.html, callout, fn_marker)
                elif _has_raw_callout(source.table_title, callout):
                    new_title = _replace_first_raw(source.table_title, callout, fn_marker)
                result[j] = source.model_copy(update={"html": new_html, "table_title": new_title})
                log.debug(
                    "Paired footnote callout %r (page %d) into table block %d page %d (P%d)",
                    callout, fn_page, j, source.provenance.page, best_priority,
                )
            result[i] = block.model_copy(update={"paired": True})
            stack.pop(best_k)

            # After a P3 win (non-multi, same-page), retire any remaining same-page multi
            # entries (P2 candidates). Without this they linger and get grabbed by a distant
            # FN via P1, preventing the salvage pass from re-linking them correctly.
            if best_priority == 3 and callout in stacks:
                stacks[callout] = [(j2, ep2, mp2) for j2, ep2, mp2 in stacks[callout] if ep2 != fn_page]
                if not stacks[callout]:
                    del stacks[callout]

    # Salvage pass: same-page raw callouts that match an already-paired FN get the same
    # marker. Handles book typos where a page has multiple identical callouts but only one
    # FN body (e.g. p34 has ① in both a para and a table header, but FN body is unique).
    paired_markers: dict[tuple[str, int], str] = {}
    for blk in result:
        if isinstance(blk, Footnote) and blk.paired and blk.callout:
            key = (blk.callout, blk.provenance.page)
            paired_markers[key] = f"\x02fn-{blk.provenance.page}-{blk.callout}\x03"

    if paired_markers:
        for i, blk in enumerate(result):
            if isinstance(blk, Paragraph):
                page = blk.provenance.page
                eff_pages = {page, page + 1} if blk.cross_page else {page}
                new_text = blk.text
                for (c, fn_page), marker in paired_markers.items():
                    if fn_page in eff_pages and _has_raw_callout(new_text, c):
                        new_text = _replace_all_raw(new_text, c, marker)
                if new_text != blk.text:
                    result[i] = blk.model_copy(update={"text": new_text})
            elif isinstance(blk, Table):
                page = blk.provenance.page
                new_html = blk.html
                new_title = blk.table_title
                for (c, fn_page), marker in paired_markers.items():
                    if fn_page != page:
                        continue
                    if _has_raw_callout(new_html, c):
                        new_html = _replace_all_raw(new_html, c, marker)
                    if _has_raw_callout(new_title, c):
                        new_title = _replace_all_raw(new_title, c, marker)
                if new_html != blk.html or new_title != blk.table_title:
                    result[i] = blk.model_copy(update={"html": new_html, "table_title": new_title})

    return result


_TERMINAL_PUNCT = frozenset('。！？…；.!?')


def _merge_empty_callout_footnotes(blocks: list[Block]) -> list[Block]:
    """Merge Footnote blocks with callout='' into the nearest preceding incomplete Footnote.

    VLM sometimes extracts cross-page footnote continuations as callout='' blocks but
    places them in a non-first position (so fn_flag cannot handle them) or omits fn_flag
    entirely. A callout='' FN is always a continuation by VLM contract; we merge it with
    the most recent preceding FN whose text does not end with terminal punctuation
    (indicating it is incomplete/truncated).
    """
    result: list[Block] = []
    for block in blocks:
        if isinstance(block, Footnote) and not block.callout:
            # Find most recent preceding FN that is not yet complete
            target_idx: int | None = None
            for j in range(len(result) - 1, -1, -1):
                b = result[j]
                if isinstance(b, Footnote):
                    tail = b.text.rstrip()
                    if tail and tail[-1] not in _TERMINAL_PUNCT:
                        target_idx = j
                        break
                    # Complete-looking FN: skip and keep looking
            if target_idx is not None:
                prev = result[target_idx]
                assert isinstance(prev, Footnote)
                result[target_idx] = prev.model_copy(update={"text": _cjk_join(prev.text, block.text)})
                log.debug(
                    "Merged empty-callout footnote (page %d) into preceding FN at block %d",
                    block.provenance.page, target_idx,
                )
                continue
            log.warning(
                "Empty-callout footnote (page %d) has no incomplete preceding FN to merge into; keeping",
                block.provenance.page,
            )
        result.append(block)
    return result


def _find_last_footnote(blocks: list[Block]) -> Footnote | None:
    """Return the most recent Footnote in blocks, skipping non-Footnote blocks."""
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        if isinstance(b, Footnote):
            return b
    return None


def _is_continuation_plausible(prev_fn: Footnote | None, cont_fn: Footnote) -> bool:
    """Hard filter: reject continuation only when callouts explicitly conflict.

    Per VLM prompt contract, a true continuation footnote must have callout="".
    If both prev and cont have non-empty callouts that differ, the VLM is
    self-contradicting — reject. All other cases (including semantic completeness)
    are left to the VLM prompt rather than code heuristics.
    """
    if prev_fn is None:
        return False
    if cont_fn.callout and prev_fn.callout and cont_fn.callout != prev_fn.callout:
        return False
    return True


def _append_to_last_footnote(blocks: list[Block], cont_text: str) -> None:
    """Append cont_text to the most recent Footnote in blocks."""
    for i in range(len(blocks) - 1, -1, -1):
        candidate = blocks[i]
        if isinstance(candidate, Footnote):
            blocks[i] = candidate.model_copy(update={"text": _cjk_join(candidate.text, cont_text)})
            return
    log.warning("first_footnote_continues_prev_footnote=True but no preceding Footnote found; dropping continuation")


def _append_to_last_paragraph(blocks: list[Block], cont_text: str) -> bool:
    """Append cont_text to the last Paragraph in blocks, skipping trailing Footnotes.

    Returns True on success.  Returns False when the tail is a non-Paragraph
    block (e.g. a Heading that immediately precedes the page break), so the
    caller can fall back to keeping the continuation as a new paragraph.
    """
    for i in range(len(blocks) - 1, -1, -1):
        candidate = blocks[i]
        if isinstance(candidate, Footnote):
            continue
        if isinstance(candidate, Paragraph):
            blocks[i] = candidate.model_copy(update={"text": _cjk_join(candidate.text, cont_text), "cross_page": True})
            return True
        break
    return False


def _is_no_space_char(c: str) -> bool:
    """Return True if *c* belongs to a script that needs no inter-word space.

    Covered ranges:
      U+3040-U+309F  Hiragana
      U+30A0-U+30FF  Katakana
      U+3400-U+4DBF  CJK Extension A
      U+4E00-U+9FFF  CJK Unified Ideographs (BMP)
      U+AC00-U+D7AF  Hangul Syllables
      U+FF00-U+FFEF  Fullwidth / halfwidth forms
      U+20000-U+2FFFF  CJK Extensions B-F (supplementary plane)
    """
    cp = ord(c)
    return (
        0x3040 <= cp <= 0x309F  # Hiragana
        or 0x30A0 <= cp <= 0x30FF  # Katakana
        or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
        or 0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0xAC00 <= cp <= 0xD7AF  # Hangul Syllables
        or 0xFF00 <= cp <= 0xFFEF  # Fullwidth / halfwidth forms
        or 0x20000 <= cp <= 0x2FFFF  # CJK Extensions B-F (supplementary)
    )


def _cjk_join(prev: str, cont: str) -> str:
    """Join two text segments: no space between CJK/kana/hangul chars, one space between Latin/digit chars.

    Special cases:
    - If prev ends with '-' (ASCII hyphen) and cont starts with a Latin letter,
      drop the hyphen and join without a space (soft-hyphen line-break continuation).
    - If either boundary character belongs to a no-space script, join directly.
    - Otherwise insert a single space.
    """
    prev = prev.rstrip()
    cont = cont.lstrip()
    if not prev or not cont:
        return prev + cont
    a, b = prev[-1], cont[0]
    # Latin hyphen continuation: drop trailing hyphen when next fragment starts with a letter
    if a == "-" and b.isalpha() and b.isascii():
        return prev[:-1] + cont
    if _is_no_space_char(a) or _is_no_space_char(b):
        return prev + cont
    return prev + " " + cont


def _detect_language(blocks: list[Block]) -> str:
    sample = "".join(b.text for b in blocks if isinstance(b, Paragraph))[:3000]
    if not sample:
        return "en"
    cjk = sum(1 for c in sample if "\u4e00" <= c <= "\u9fff")
    return "zh" if cjk / len(sample) > 0.1 else "en"


class _BookMeta:
    def __init__(self, title: str, language: str, authors: list[str] | None = None, source_pdf: str = "") -> None:
        self.title = title
        self.language = language
        self.authors: list[str] = authors or []
        self.source_pdf = source_pdf


def _build_book_from_stream(blocks: list[Block], meta: _BookMeta) -> Book:
    """Aggregate blocks into chapters at every level-1 Heading boundary."""
    chapters: list[Chapter] = []
    current_title = "Front Matter"
    current_blocks: list[Block] = []

    def flush(at_heading_boundary: bool = False) -> None:
        # Always flush at heading boundaries to preserve heading-only pages (e.g. dedication pages).
        # At end of stream, skip empty chapters unless no chapters exist yet.
        if current_blocks or not chapters or at_heading_boundary:
            chapters.append(Chapter(title=current_title, blocks=list(current_blocks)))

    for block in blocks:
        if isinstance(block, Heading) and block.level == 1:
            flush(at_heading_boundary=True)
            current_title = block.text
            current_blocks = []
        else:
            current_blocks.append(block)

    flush()

    return Book(
        title=meta.title,
        language=meta.language,
        authors=meta.authors,
        source_pdf=meta.source_pdf,
        chapters=chapters,
    )


def _build_book(blocks: list[Block], work_dir: Path) -> Book:
    """Aggregate blocks into chapters at every level-1 Heading boundary."""
    from docling_core.types.doc import DoclingDocument

    raw_path = work_dir / "01_raw.json"
    title = work_dir.name.replace("_", " ").title()
    if raw_path.exists():
        doc = DoclingDocument.load_from_json(raw_path)
        origin = getattr(doc, "origin", None)
        if origin and getattr(origin, "filename", None):
            title = Path(origin.filename).stem
        elif doc.name:
            title = doc.name

    language = _detect_language(blocks)
    meta = _BookMeta(title=title, language=language)
    return _build_book_from_stream(blocks, meta)
