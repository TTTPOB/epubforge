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
    """Read stage 3 extract units from *work_dir* and write Semantic IR JSON to *out_path*."""
    extract_dir = work_dir / "03_extract"
    unit_files = sorted(extract_dir.glob("unit_*.json"))

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
                _append_to_last_footnote(all_blocks, fn_cont.text)
                parsed = [b for i, b in enumerate(parsed) if i != first_fn_idx]
            else:
                log.warning(
                    "first_footnote_continues_prev_footnote=True but no Footnote in unit %s",
                    uf.name,
                )

        if flag and parsed:
            cont = parsed[0]
            if isinstance(cont, Paragraph):
                _append_to_last_paragraph(all_blocks, cont.text)
                all_blocks.extend(parsed[1:])
            else:
                log.warning(
                    "first_block_continues_prev_tail=True but first block is not Paragraph "
                    "(kind=%s, unit=%s)", cont.kind, uf.name  # type: ignore[union-attr]
                )
                all_blocks.extend(parsed)
        else:
            all_blocks.extend(parsed)

    # Merge cross-page table continuations, absorb adjacent title/caption paragraphs, pair footnotes
    all_blocks = _merge_continued_tables(all_blocks)
    all_blocks = _absorb_table_text(all_blocks)
    all_blocks = _pair_footnotes(all_blocks)

    # Group blocks into chapters at heading-level-1 boundaries
    book = _build_book(all_blocks, work_dir)
    out_path.write_text(book.model_dump_json(indent=2), encoding="utf-8")


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
    """Find each Footnote's callout char in preceding paragraphs and embed an inline marker.

    The marker \x02fn-PAGE-CALLOUT\x03 is later converted to a <sup><a href=...> link by
    epub_builder. Footnotes whose callout cannot be located are left unpaired (epub_builder
    falls back to a standalone superscript reference).
    """
    result = list(blocks)
    for i, block in enumerate(result):
        if not isinstance(block, Footnote):
            continue
        callout = block.callout
        fn_marker = f"\x02fn-{block.provenance.page}-{callout}\x03"
        for j in range(i - 1, max(i - 40, -1), -1):
            candidate = result[j]
            if isinstance(candidate, Paragraph) and callout in candidate.text:
                result[j] = candidate.model_copy(update={
                    "text": candidate.text.replace(callout, fn_marker, 1)
                })
                result[i] = block.model_copy(update={"paired": True})
                log.debug("Paired footnote callout %r (page %d) into paragraph block %d", callout, block.provenance.page, j)
                break
            if isinstance(candidate, Table) and callout in candidate.html:
                # Replace all occurrences — same callout may appear in multiple cells
                result[j] = candidate.model_copy(update={
                    "html": candidate.html.replace(callout, fn_marker)
                })
                result[i] = block.model_copy(update={"paired": True})
                log.debug("Paired footnote callout %r (page %d) into table block %d", callout, block.provenance.page, j)
                break
    return result


def _append_to_last_footnote(blocks: list[Block], cont_text: str) -> None:
    """Append cont_text to the most recent Footnote in blocks."""
    for i in range(len(blocks) - 1, -1, -1):
        candidate = blocks[i]
        if isinstance(candidate, Footnote):
            blocks[i] = candidate.model_copy(update={"text": _cjk_join(candidate.text, cont_text)})
            return
    log.warning("first_footnote_continues_prev_footnote=True but no preceding Footnote found; dropping continuation")


def _append_to_last_paragraph(blocks: list[Block], cont_text: str) -> None:
    """Append cont_text to the last Paragraph in blocks, skipping trailing Footnotes."""
    for i in range(len(blocks) - 1, -1, -1):
        candidate = blocks[i]
        if isinstance(candidate, Footnote):
            continue
        if isinstance(candidate, Paragraph):
            blocks[i] = candidate.model_copy(update={"text": _cjk_join(candidate.text, cont_text)})
            return
        break
    log.warning("first_block_continues_prev_tail=True but no anchor Paragraph found; dropping continuation")


def _cjk_join(prev: str, cont: str) -> str:
    """Join two text segments: no space between CJK chars, one space between Latin/digit chars."""
    prev = prev.rstrip()
    cont = cont.lstrip()
    if not prev or not cont:
        return prev + cont
    a, b = prev[-1], cont[0]
    is_cjk = lambda c: "\u4e00" <= c <= "\u9fff"
    if is_cjk(a) or is_cjk(b):
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
