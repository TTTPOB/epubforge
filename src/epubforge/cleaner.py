"""Stage 3 — LLM text cleaning of simple pages."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.document import DocItem

from epubforge.config import Config
from epubforge.llm.client import LLMClient, Message
from epubforge.llm.prompts import CLEAN_SYSTEM
from epubforge.ir.semantic import CleanOutput

_HEADER_LABELS = frozenset({DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE})
_SKIP_LABELS = frozenset({DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER})


def clean_simple_pages(
    raw_path: Path,
    pages_path: Path,
    out_dir: Path,
    cfg: Config,
    *,
    force: bool = False,
    page_nos: set[int] | None = None,
) -> None:
    """Clean simple pages via LLM; write one JSON per section-group to *out_dir*."""
    doc = DoclingDocument.load_from_json(raw_path)
    pages_data: list[dict[str, Any]] = json.loads(pages_path.read_text(encoding="utf-8"))["pages"]

    simple_set = {p["page"] for p in pages_data if p["kind"] == "simple"}
    if page_nos is not None:
        simple_set &= page_nos

    # Build page → ordered items mapping using Docling reading order.
    # iterate_items() traverses the document body tree in reading order,
    # handling multi-column layouts correctly — no manual bbox sort needed.
    page_items: dict[int, list[dict[str, Any]]] = defaultdict(list)
    _seen: set[tuple[str, int]] = set()
    for item, _level in doc.iterate_items():
        if not isinstance(item, DocItem):
            continue
        text = getattr(item, "text", None)
        if not text:
            continue
        for prov in item.prov:
            pno = prov.page_no
            if pno not in simple_set:
                continue
            key = (item.self_ref, pno)
            if key in _seen:
                continue
            _seen.add(key)
            page_items[pno].append({
                "label": item.label,
                "text": text,
                "page": pno,
            })

    # Group consecutive simple pages into section-bounded chunks
    groups = _build_groups(sorted(simple_set), page_items)

    client = LLMClient(cfg, use_vlm=False)

    for i, group in enumerate(groups):
        out_path = out_dir / f"group_{i:04d}.json"
        if out_path.exists() and not force:
            continue

        user_text = _format_blocks_for_llm(group["items"])
        messages: list[Message] = [
            {"role": "system", "content": CLEAN_SYSTEM},
            {"role": "user", "content": user_text},
        ]
        result = client.chat_parsed(messages, response_format=CleanOutput)
        blocks = [b.model_dump(exclude_none=True) for b in result.blocks]

        out_path.write_text(
            json.dumps({"pages": group["pages"], "blocks": blocks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _build_groups(
    sorted_pages: list[int],
    page_items: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Split pages into groups at section-header boundaries."""
    groups: list[dict[str, Any]] = []
    current_pages: list[int] = []
    current_items: list[dict[str, Any]] = []

    def flush() -> None:
        if current_pages:
            groups.append({"pages": list(current_pages), "items": list(current_items)})

    for pno in sorted_pages:
        items = page_items.get(pno, [])
        has_header = any(it["label"] in _HEADER_LABELS for it in items)
        if has_header and current_pages:
            flush()
            current_pages.clear()
            current_items.clear()
        current_pages.append(pno)
        current_items.extend(items)

    flush()
    return groups


def _format_blocks_for_llm(items: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for it in items:
        if it["label"] in _SKIP_LABELS:
            continue
        label_str = it["label"].value if isinstance(it["label"], DocItemLabel) else it["label"]
        prefix = f"[{label_str.upper()}] " if it["label"] in _HEADER_LABELS else ""
        pno = it.get("page", 0)
        chunks.append(f"[BLOCK p{pno}]\n{prefix}{it['text']}\n[/BLOCK]")
    return "\n".join(chunks)
