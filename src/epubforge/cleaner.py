"""Stage 3 — LLM text cleaning of simple pages."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from epubforge.config import Config
from epubforge.llm.client import LLMClient, Message
from epubforge.llm.prompts import CLEAN_SYSTEM

_HEADER_LABELS = frozenset({"section_header", "title"})
_SKIP_LABELS = frozenset({"page_header", "page_footer"})


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
    raw: dict[str, Any] = json.loads(raw_path.read_text(encoding="utf-8"))
    pages_data: list[dict[str, Any]] = json.loads(pages_path.read_text(encoding="utf-8"))["pages"]

    simple_set = {p["page"] for p in pages_data if p["kind"] == "simple"}
    if page_nos is not None:
        simple_set &= page_nos

    # Build page → ordered items mapping from all text collections
    page_items: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in raw.get("texts") or []:
        for prov in item.get("prov") or []:
            pno = prov.get("page_no", 0)
            if pno in simple_set:
                page_items[pno].append({
                    "label": item.get("label", ""),
                    "text": item.get("text", ""),
                    "bbox": prov.get("bbox"),
                })

    # Sort each page's items by vertical position (top bbox coord)
    for pno in page_items:
        page_items[pno].sort(key=lambda x: (x["bbox"] or {}).get("t", 0) if x["bbox"] else 0)

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
        raw_reply = client.chat(messages, response_format={"type": "json_object"})
        try:
            parsed = json.loads(raw_reply)
            blocks = parsed.get("blocks", [])
        except json.JSONDecodeError:
            blocks = [{"kind": "paragraph", "text": raw_reply,
                       "provenance": {"page": group["pages"][0], "source": "llm"}}]

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
        # Start a new group when a section header appears (but not at the very first page)
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
    lines: list[str] = []
    for it in items:
        if it["label"] in _SKIP_LABELS:
            continue
        prefix = f"[{it['label'].upper()}] " if it["label"] in _HEADER_LABELS else ""
        lines.append(f"{prefix}{it['text']}")
    return "\n".join(lines)
