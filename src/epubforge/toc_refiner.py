"""Stage 5.5 — LLM-based global TOC hierarchy refinement."""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from epubforge.assembler import _BookMeta, _build_book_from_stream
from epubforge.config import Config
from epubforge.ir.semantic import (
    Block,
    Book,
    Chapter,
    Heading,
    Provenance,
    TocRefineOutput,
)
from openai.types.chat import ChatCompletionMessageParam

from epubforge.llm.client import LLMClient
from epubforge.llm.prompts import TOC_REFINE_SYSTEM

log = logging.getLogger(__name__)

_SLUG_KEEP = re.compile(r"[^\w\u4e00-\u9fff-]", re.UNICODE)


def _slug(text: str, max_len: int = 40) -> str:
    """Produce a URL-safe id fragment from text."""
    nfkd = unicodedata.normalize("NFKD", text)
    s = _SLUG_KEEP.sub("-", nfkd).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:max_len].rstrip("-") or "h"


def _flatten_book(book: Book) -> list[Block]:
    """Expand Book into a single flat block stream, re-inserting chapter-title headings."""
    stream: list[Block] = []
    for chapter in book.chapters:
        prov = chapter.blocks[0].provenance if chapter.blocks else Provenance(page=0, source="passthrough")
        stream.append(Heading(level=1, text=chapter.title, provenance=prov))
        stream.extend(chapter.blocks)
    return stream


def refine_toc(raw_path: Path, out_path: Path, cfg: Config) -> None:
    """Load raw Book JSON, refine heading hierarchy with LLM, write corrected Book JSON."""
    book = Book.model_validate_json(raw_path.read_text(encoding="utf-8"))
    stream = _flatten_book(book)

    # Collect heading candidates with their position in the stream
    candidates: list[tuple[int, Heading]] = [
        (i, block) for i, block in enumerate(stream) if isinstance(block, Heading)
    ]

    if not candidates:
        log.warning("refine-toc: no headings found, writing raw book unchanged")
        out_path.write_text(book.model_dump_json(indent=2), encoding="utf-8")
        return

    log.info("refine-toc: %d heading candidates to refine", len(candidates))

    # Build user message listing all headings
    lines = ["Headings (idx | level | page | text):"]
    for seq, (stream_idx, h) in enumerate(candidates):
        lines.append(f"{seq} | {h.level} | p{h.provenance.page} | {h.text}")
    user_msg = "\n".join(lines)

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": TOC_REFINE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    client = LLMClient(cfg)
    result: TocRefineOutput = client.chat_parsed(messages, response_format=TocRefineOutput)

    if len(result.items) != len(candidates):
        raise ValueError(
            f"refine-toc: LLM returned {len(result.items)} items but expected {len(candidates)}"
        )

    # Apply corrections back to stream; track which stream indices to delete (merged)
    to_delete: set[int] = set()
    seen_ids: dict[str, int] = {}

    prev_stream_idx: int | None = None
    for seq, item in enumerate(result.items):
        stream_idx, orig_heading = candidates[seq]

        if item.merge_with_prev and prev_stream_idx is not None:
            # Append normalized text to previous heading and remove this one
            prev = stream[prev_stream_idx]
            assert isinstance(prev, Heading)
            merged_text = prev.text + item.text
            stream[prev_stream_idx] = prev.model_copy(update={"text": merged_text})
            to_delete.add(stream_idx)
            log.debug("refine-toc: merged heading %r + %r → %r", prev.text, item.text, merged_text)
            continue

        if item.text != orig_heading.text:
            log.debug("refine-toc: normalized %r → %r (p%d)", orig_heading.text, item.text, orig_heading.provenance.page)

        # Assign stable id
        slug_base = _slug(item.text)
        page = orig_heading.provenance.page
        id_candidate = f"{slug_base}-{page}"
        count = seen_ids.get(id_candidate, 0)
        seen_ids[id_candidate] = count + 1
        final_id = id_candidate if count == 0 else f"{id_candidate}-{count}"

        stream[stream_idx] = orig_heading.model_copy(update={
            "level": item.level,
            "text": item.text,
            "id": final_id,
        })
        prev_stream_idx = stream_idx

    # Remove merged headings
    clean_stream = [b for i, b in enumerate(stream) if i not in to_delete]

    # Rebuild book using the refined stream
    meta = _BookMeta(
        title=book.title,
        language=book.language,
        authors=book.authors,
        source_pdf=book.source_pdf,
    )
    refined_book = _build_book_from_stream(clean_stream, meta)

    # Propagate chapter ids from level-1 headings
    for chapter in refined_book.chapters:
        for block in chapter.blocks:
            pass  # chapter id is set below via title lookup
    # Map title → id from refined level-1 headings
    title_to_id: dict[str, str] = {}
    for block in clean_stream:
        if isinstance(block, Heading) and block.level == 1 and block.id:
            title_to_id[block.text] = block.id

    refined_chapters: list[Chapter] = []
    for chapter in refined_book.chapters:
        ch_id = title_to_id.get(chapter.title)
        refined_chapters.append(chapter.model_copy(update={"id": ch_id}))
    refined_book = refined_book.model_copy(update={"chapters": refined_chapters})

    out_path.write_text(refined_book.model_dump_json(indent=2), encoding="utf-8")
    log.info(
        "refine-toc: %d chapters (was %d), %d headings refined",
        len(refined_book.chapters),
        len(book.chapters),
        len(candidates),
    )
