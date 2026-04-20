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
)

log = logging.getLogger(__name__)


def assemble(work_dir: Path, out_path: Path) -> None:
    """Read stages 2–4 from *work_dir* and write Semantic IR JSON to *out_path*."""
    pages_data: list[dict[str, Any]] = json.loads(
        (work_dir / "02_pages.json").read_text(encoding="utf-8")
    )["pages"]

    simple_dir = work_dir / "03_simple"
    complex_dir = work_dir / "04_complex"

    # Build page → group file mapping for simple pages
    page_to_group: dict[int, Path] = {}
    for gf in sorted(simple_dir.glob("group_*.json")):
        gdata = json.loads(gf.read_text(encoding="utf-8"))
        for pno in gdata.get("pages", []):
            page_to_group[pno] = gf

    # Walk pages in reading order, collect all blocks
    all_blocks: list[Block] = []
    consumed_groups: set[Path] = set()

    for page_info in pages_data:
        pno: int = page_info["page"]
        kind: str = page_info["kind"]

        if kind == "simple":
            gf = page_to_group.get(pno)
            if gf is None or gf in consumed_groups:
                continue
            consumed_groups.add(gf)
            gdata = json.loads(gf.read_text(encoding="utf-8"))
            for raw_block in gdata.get("blocks", []):
                block = _parse_block(raw_block, pno, "llm")
                if block is not None:
                    all_blocks.append(block)

        else:  # complex
            cf = complex_dir / f"p{pno:04d}.json"
            if not cf.exists():
                log.warning("Missing VLM output for page %d, skipping", pno)
                continue
            cdata = json.loads(cf.read_text(encoding="utf-8"))
            for raw_block in cdata.get("blocks", []):
                block = _parse_block(raw_block, pno, "vlm")
                if block is not None:
                    all_blocks.append(block)

    # Merge cross-page table continuations, absorb adjacent title/caption paragraphs, pair footnotes
    all_blocks = _merge_continued_tables(all_blocks)
    all_blocks = _absorb_table_text(all_blocks)
    all_blocks = _pair_footnotes(all_blocks)

    # Group blocks into chapters at heading-level-1 boundaries
    book = _build_book(all_blocks, work_dir)
    out_path.write_text(book.model_dump_json(indent=2), encoding="utf-8")


def _parse_block(raw: dict[str, Any], page: int, source: str) -> Block | None:
    kind = raw.get("kind", "")
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
                result[prev_idx] = prev_tbl.model_copy(update={
                    "html": _splice_table_html(prev_tbl.html, block.html),
                    "table_title": prev_tbl.table_title or block.table_title,
                    "caption": prev_tbl.caption or block.caption,
                })
                log.debug("Merged continuation table (page %d) into table (page %d)", block.provenance.page, prev_tbl.provenance.page)
                continue
        result.append(block)
    return result


def _splice_table_html(base: str, cont: str) -> str:
    """Strip outer <table> wrapper from cont and append its rows before </table> in base."""
    inner = re.sub(r"^\s*<table[^>]*>", "", cont, count=1, flags=re.IGNORECASE)
    inner = re.sub(r"</table>\s*$", "", inner, count=1, flags=re.IGNORECASE)
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
    """Match footnote callout markers in paragraphs to footnote body blocks (best-effort)."""
    # For MVP: footnotes are already parsed as Footnote blocks by VLM; no extra pairing needed.
    return blocks


def _detect_language(blocks: list[Block]) -> str:
    sample = "".join(b.text for b in blocks if isinstance(b, Paragraph))[:3000]
    if not sample:
        return "en"
    cjk = sum(1 for c in sample if "\u4e00" <= c <= "\u9fff")
    return "zh" if cjk / len(sample) > 0.1 else "en"


def _build_book(blocks: list[Block], work_dir: Path) -> Book:
    """Aggregate blocks into chapters at every level-1 Heading boundary."""
    raw_path = work_dir / "01_raw.json"
    title = "Untitled"
    if raw_path.exists():
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        meta = raw.get("metadata") or {}
        title = meta.get("title") or (work_dir.name.replace("_", " ").title())

    language = _detect_language(blocks)

    chapters: list[Chapter] = []
    current_title = "Front Matter"
    current_blocks: list[Block] = []

    def flush() -> None:
        if current_blocks or chapters == []:
            chapters.append(Chapter(title=current_title, blocks=list(current_blocks)))

    for block in blocks:
        if isinstance(block, Heading) and block.level == 1:
            flush()
            current_title = block.text
            current_blocks = []
        else:
            current_blocks.append(block)

    flush()

    return Book(title=title, language=language, chapters=chapters)
