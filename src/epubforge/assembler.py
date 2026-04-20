"""Stage 5 — merge cleaned + VLM outputs into Semantic IR."""

from __future__ import annotations

import json
import logging
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

    # Pair footnote callouts with bodies
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
            return Table(html=raw.get("html", ""), caption=str(raw.get("caption") or ""), provenance=prov)
        if kind == "equation":
            return Equation(latex=raw.get("latex", ""), provenance=prov)
    except Exception as exc:
        log.warning("Skipping malformed block %s: %s", raw, exc)
    return None


def _pair_footnotes(blocks: list[Block]) -> list[Block]:
    """Match footnote callout markers in paragraphs to footnote body blocks (best-effort)."""
    # For MVP: footnotes are already parsed as Footnote blocks by VLM; no extra pairing needed.
    return blocks


def _build_book(blocks: list[Block], work_dir: Path) -> Book:
    """Aggregate blocks into chapters at every level-1 Heading boundary."""
    # Try to get title from raw metadata
    raw_path = work_dir / "01_raw.json"
    title = "Untitled"
    if raw_path.exists():
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        meta = raw.get("metadata") or {}
        title = meta.get("title") or (work_dir.name.replace("_", " ").title())

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

    return Book(title=title, chapters=chapters)
