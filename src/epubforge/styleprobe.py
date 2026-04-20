"""Style prototype probe — trusts docling labels, clusters only TEXT blocks with VLM."""

from __future__ import annotations

import base64
import json
import re
import shutil
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

import fitz
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import CoordOrigin
from rich.console import Console
from rich.progress import track

from epubforge.config import Config
from epubforge.ir.semantic import StyleClusterLabel, StyleRole
from epubforge.llm.client import LLMClient, Message
from epubforge.llm.prompts import STYLE_PROBE_SYSTEM
from epubforge.pdf_render import render_block_crop, render_page_with_highlight

console = Console()

# Docling labels that map 1-to-1 to a role without needing VLM.
_LABEL_ROLE_MAP: dict[str, StyleRole] = {
    DocItemLabel.PAGE_HEADER.value: StyleRole.page_header,
    DocItemLabel.PAGE_FOOTER.value: StyleRole.page_footer,
    DocItemLabel.FOOTNOTE.value: StyleRole.footnote,
    DocItemLabel.CAPTION.value: StyleRole.caption,
    DocItemLabel.LIST_ITEM.value: StyleRole.list_item,
    DocItemLabel.SECTION_HEADER.value: StyleRole.section_title,
    DocItemLabel.TITLE.value: StyleRole.chapter_title,
}

_FS_BUCKETS = [(0.80, "xs"), (0.95, "s"), (1.15, "m"), (1.50, "l")]
_W_BUCKETS = [(0.35, "narrow"), (0.60, "half"), (0.85, "wide")]
_CX_BUCKETS = [(0.42, "left"), (0.58, "center")]
_INDENT_BUCKETS = [(0.05, "none"), (0.15, "small"), (0.30, "medium")]

# Language-neutral text patterns.
_RE_PURE_NUM = re.compile(r"^\s*\d+\s*$")
_RE_DASH_TAIL = re.compile(r"(——|—|--|―)\s*\S{1,15}\s*$")


def _bucket(value: float, thresholds: list[tuple[float, str]], last: str) -> str:
    for threshold, name in thresholds:
        if value < threshold:
            return name
    return last


def run_style_probe(
    pdf_path: Path,
    cfg: Config,
    *,
    min_cluster_size: int = 3,
    max_exemplars: int = 3,
    force: bool = False,
) -> None:
    book_dir = cfg.book_work_dir(pdf_path)
    raw_path = book_dir / "01_raw.json"
    if not raw_path.exists():
        raise SystemExit(
            f"01_raw.json not found at {raw_path}. Run `epubforge parse {pdf_path}` first."
        )

    proto_path = book_dir / "style_prototypes.json"
    styles_dir = book_dir / "styles"
    report_path = book_dir / "styles_report.html"

    if proto_path.exists() and not force:
        console.print("[yellow]style_prototypes.json already exists, skipping (use --force to rerun)[/yellow]")
        return

    if force:
        if styles_dir.exists():
            shutil.rmtree(styles_dir)
        proto_path.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)

    console.print("[bold]Loading Docling document…[/bold]")
    doc = DoclingDocument.load_from_json(raw_path)

    console.print("[bold]Extracting block features…[/bold]")
    book_name = pdf_path.stem
    blocks = _extract_all_blocks(doc)

    if not blocks:
        raise SystemExit("No text blocks found in document.")

    book_line_pitch = _estimate_line_pitch(blocks)
    console.print(f"  book_line_pitch = {book_line_pitch:.2f} pt")

    # --- Stage A: dispatch blocks into docling-direct vs needs-clustering ---

    direct_groups: dict[StyleRole, list[int]] = defaultdict(list)
    to_cluster: list[int] = []

    for i, b in enumerate(blocks):
        direct_role = _assign_direct_role(b)
        if direct_role is not None:
            direct_groups[direct_role].append(i)
        else:
            to_cluster.append(i)

    console.print(
        f"  dispatched: {sum(len(v) for v in direct_groups.values())} docling-direct, "
        f"{len(to_cluster)} to cluster"
    )

    # --- Stage B: cluster TEXT blocks by signature ---

    features = [_compute_features(blocks[i], book_line_pitch) for i in to_cluster]
    signatures = [_make_signature(f) for f in features]

    cluster_map: dict[str, list[int]] = defaultdict(list)
    for local_i, sig in enumerate(signatures):
        cluster_map[sig].append(to_cluster[local_i])

    sorted_clusters = sorted(cluster_map.items(), key=lambda kv: -len(kv[1]))
    console.print(f"  {len(sorted_clusters)} TEXT-signature clusters")

    # --- Stage C: render exemplars + label clusters via VLM ---

    cfg.require_vlm()
    client = LLMClient(cfg, use_vlm=True)
    fitz_doc = fitz.open(str(pdf_path))

    styles_dir.mkdir(parents=True, exist_ok=True)

    prototypes: list[dict[str, Any]] = []
    assignments: dict[str, str] = {}

    # (1) One prototype per docling-direct role — no VLM, just exemplar renders.
    for role, member_indices in sorted(direct_groups.items(), key=lambda kv: -len(kv[1])):
        dl_id = f"dl_{role.value}"
        support = len(member_indices)
        sampled = _stratified_sample(member_indices, blocks, max_exemplars)
        dl_dir = styles_dir / dl_id
        dl_dir.mkdir(parents=True, exist_ok=True)

        exemplars: list[dict[str, Any]] = []
        for n, bi in enumerate(sampled):
            b = blocks[bi]
            img_b64, _ = render_block_crop(
                fitz_doc, b["page_no"], b["bbox"], b["coord_origin"]
            )
            crop_path = dl_dir / f"{n}.jpg"
            crop_path.write_bytes(base64.b64decode(img_b64))
            exemplars.append({
                "page": b["page_no"],
                "ref": b["self_ref"],
                "bbox": b["bbox_list"],
                "crop": f"styles/{dl_id}/{n}.jpg",
                "role": role.value,
                "confidence": 1.0,
                "reason": f"docling label={b['label']}",
            })

        prototypes.append({
            "id": dl_id,
            "source": "docling",
            "signature": f"label={[blocks[i]['label'] for i in member_indices[:1]][0]}",
            "support": support,
            "role": role.value,
            "confidence": 1.0,
            "exemplars": exemplars,
        })
        for bi in member_indices:
            assignments[blocks[bi]["self_ref"]] = dl_id

    # (2) Signature clusters — call VLM for those meeting min support.
    for idx, (sig, member_indices) in enumerate(track(sorted_clusters, description="Labelling TEXT clusters…")):
        vp_id = f"vp_{idx + 1:03d}"
        support = len(member_indices)

        if support < min_cluster_size:
            role_str = StyleRole.other.value
            confidence = 0.0
            exemplar_data: list[dict[str, Any]] = []
        else:
            sampled = _stratified_sample(member_indices, blocks, max_exemplars)
            vp_dir = styles_dir / vp_id
            vp_dir.mkdir(parents=True, exist_ok=True)

            exemplar_images: list[tuple[tuple[str, str], tuple[str, str]]] = []
            exemplar_data = []
            for n, bi in enumerate(sampled):
                b = blocks[bi]
                crop_b64, crop_mime = render_block_crop(
                    fitz_doc, b["page_no"], b["bbox"], b["coord_origin"]
                )
                page_b64, page_mime = render_page_with_highlight(
                    fitz_doc, b["page_no"], b["bbox"], b["coord_origin"]
                )
                crop_path = vp_dir / f"{n}.jpg"
                crop_path.write_bytes(base64.b64decode(crop_b64))
                exemplar_images.append(((page_b64, page_mime), (crop_b64, crop_mime)))
                exemplar_data.append({
                    "page": b["page_no"],
                    "ref": b["self_ref"],
                    "bbox": b["bbox_list"],
                    "crop": f"styles/{vp_id}/{n}.jpg",
                })

            sig_hint = _signature_to_human(sig)
            label_result = _label_cluster(client, exemplar_images, sig_hint)

            for n, ex_label in enumerate(label_result.exemplars):
                exemplar_data[n]["role"] = ex_label.role.value
                exemplar_data[n]["confidence"] = ex_label.confidence
                exemplar_data[n]["reason"] = ex_label.reason

            roles = [e.role for e in label_result.exemplars]
            majority_role = max(set(roles), key=roles.count)
            avg_conf = sum(e.confidence for e in label_result.exemplars) / len(label_result.exemplars)
            role_str = majority_role.value
            confidence = round(avg_conf, 3)

        prototypes.append({
            "id": vp_id,
            "source": "cluster+vlm",
            "signature": sig,
            "support": support,
            "role": role_str,
            "confidence": confidence,
            "exemplars": exemplar_data,
        })
        for bi in member_indices:
            assignments[blocks[bi]["self_ref"]] = vp_id

    fitz_doc.close()

    output = {
        "book": book_name,
        "n_blocks": len(blocks),
        "n_direct": sum(len(v) for v in direct_groups.values()),
        "n_clustered": len(to_cluster),
        "n_clusters": len(sorted_clusters),
        "book_line_pitch": round(book_line_pitch, 3),
        "prototypes": prototypes,
        "assignments": assignments,
    }

    proto_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]Wrote {proto_path}[/green]")

    _render_html_report(report_path, output, book_dir)
    console.print(f"[green]Wrote {report_path}[/green]")


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

def _assign_direct_role(block: dict[str, Any]) -> StyleRole | None:
    """Return a fixed role for blocks docling has already classified, else None."""
    # Furniture layer is page header/footer regardless of label resolution quirks.
    if block.get("content_layer") == "furniture":
        if block["label"] == DocItemLabel.PAGE_HEADER.value:
            return StyleRole.page_header
        # default furniture → page_footer (covers page numbers marked as page_footer)
        return StyleRole.page_footer

    label = block["label"]
    return _LABEL_ROLE_MAP.get(label)


# ---------------------------------------------------------------------------
# Block extraction helpers
# ---------------------------------------------------------------------------

def _extract_all_blocks(doc: DoclingDocument) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in doc.texts:
        text: str = getattr(item, "text", "") or ""
        if not text.strip():
            continue
        content_layer = getattr(item, "content_layer", "body")
        if hasattr(content_layer, "value"):
            content_layer = content_layer.value
        for prov in item.prov:
            if prov.bbox is None:
                continue
            page = doc.pages.get(prov.page_no)
            if page is None or page.size is None:
                continue
            blocks.append({
                "self_ref": item.self_ref,
                "label": item.label.value if hasattr(item.label, "value") else str(item.label),
                "content_layer": content_layer,
                "text": text,
                "page_no": prov.page_no,
                "page_w": page.size.width,
                "page_h": page.size.height,
                "bbox": prov.bbox,
                "bbox_list": [prov.bbox.l, prov.bbox.t, prov.bbox.r, prov.bbox.b],
                "coord_origin": prov.bbox.coord_origin,
            })
    return blocks


def _estimate_line_pitch(blocks: list[dict[str, Any]]) -> float:
    """Estimate the baseline character side (pt) using sqrt(bbox_area / char_count)
    on body-layer TEXT blocks. Matches the metric used in `_compute_features`,
    so font_scale = 1.0 means "typical body character size". We use all multi-char
    body TEXT blocks (majority = body prose) so the median tracks body-text size."""
    sides: list[float] = []
    for b in blocks:
        if b["label"] != DocItemLabel.TEXT.value:
            continue
        if b.get("content_layer") != "body":
            continue
        text: str = b["text"]
        if len(text) < 10:
            continue
        bbox = b["bbox"]
        h = abs(bbox.t - bbox.b)
        w = abs(bbox.r - bbox.l)
        if h <= 0 or w <= 0:
            continue
        side = (h * w / len(text)) ** 0.5
        if 3.0 < side < 80.0:
            sides.append(side)
    if not sides:
        return 12.0
    return statistics.median(sides)


# ---------------------------------------------------------------------------
# Feature extraction and signature bucketing (TEXT-only)
# ---------------------------------------------------------------------------

def _compute_features(b: dict[str, Any], book_line_pitch: float) -> dict[str, Any]:
    bbox = b["bbox"]
    page_w: float = b["page_w"]
    page_h: float = b["page_h"]

    if b["coord_origin"] == CoordOrigin.BOTTOMLEFT:
        top = page_h - bbox.t
        bottom = page_h - bbox.b
    else:
        top = bbox.t
        bottom = bbox.b

    left = bbox.l
    right = bbox.r

    width = (right - left) / page_w
    height_px = bottom - top
    bbox_width_px = right - left
    x_center = (left + right) / 2 / page_w
    left_indent = left / page_w
    right_indent = (page_w - right) / page_w

    text: str = b["text"]
    char_count = len(text)

    # Area-based font size estimate (works for horizontal AND vertical text):
    # assume CJK chars are roughly square, so char_side ≈ sqrt(area / n_chars).
    if char_count > 0 and height_px > 0 and bbox_width_px > 0:
        est_char_side = (height_px * bbox_width_px / char_count) ** 0.5
    else:
        est_char_side = book_line_pitch
    font_scale = est_char_side / book_line_pitch if book_line_pitch > 0 else 1.0

    # Vertical orientation: tall narrow block with multiple chars.
    is_vertical = (
        bbox_width_px > 0
        and height_px > 3 * bbox_width_px
        and char_count >= 3
    )

    # Line count: split on '\n', else derive from dominant axis length.
    if "\n" in text:
        line_count_hint = max(1, len(text.split("\n")))
    elif is_vertical:
        line_count_hint = 1  # one column of vertical text
    else:
        line_count_hint = max(1, round(height_px / est_char_side)) if est_char_side > 0 else 1

    # Pattern bucket (language-neutral).
    if _RE_PURE_NUM.match(text):
        pat = "pure_num"
    elif _RE_DASH_TAIL.search(text) and char_count < 40:
        pat = "dash_tail"
    else:
        pat = "none"

    return {
        "font_scale": font_scale,
        "width": width,
        "x_center": x_center,
        "left_indent": left_indent,
        "right_indent": right_indent,
        "line_count": line_count_hint,
        "is_vertical": is_vertical,
        "char_count": char_count,
        "pat": pat,
    }


def _make_signature(f: dict[str, Any]) -> str:
    fs = _bucket(f["font_scale"], _FS_BUCKETS, "xl")
    w = _bucket(f["width"], _W_BUCKETS, "full")
    cx = _bucket(f["x_center"], _CX_BUCKETS, "right")
    il = _bucket(f["left_indent"], _INDENT_BUCKETS, "large")
    ir = _bucket(f["right_indent"], _INDENT_BUCKETS, "large")
    lc = f["line_count"]
    sl = "single" if lc == 1 else ("few" if lc <= 3 else "many")
    orient = "vert" if f["is_vertical"] else "horiz"
    pat = f["pat"]
    return f"fs={fs}|w={w}|cx={cx}|il={il}|ir={ir}|sl={sl}|orient={orient}|pat={pat}"


# ---------------------------------------------------------------------------
# Stratified exemplar sampling
# ---------------------------------------------------------------------------

def _stratified_sample(
    member_indices: list[int],
    blocks: list[dict[str, Any]],
    k: int,
) -> list[int]:
    if len(member_indices) <= k:
        return list(member_indices)
    sorted_by_page = sorted(member_indices, key=lambda i: blocks[i]["page_no"])
    step = len(sorted_by_page) / k
    return [sorted_by_page[int(i * step)] for i in range(k)]


# ---------------------------------------------------------------------------
# VLM call
# ---------------------------------------------------------------------------

def _label_cluster(
    client: LLMClient,
    exemplar_images: list[tuple[tuple[str, str], tuple[str, str]]],
    sig_hint: str,
) -> StyleClusterLabel:
    n = len(exemplar_images)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Cluster signature hints: {sig_hint}. "
                f"Below are {n} exemplar(s), each shown as a pair of images: the first is the "
                "full page with the target block outlined in red; the second is a zoomed crop "
                "of the target block. All exemplars come from blocks that docling labeled as "
                "generic TEXT (body-layer). Assign a role to each exemplar in order."
            ),
        }
    ]
    for i, ((page_b64, page_mime), (crop_b64, crop_mime)) in enumerate(exemplar_images, start=1):
        content.append({
            "type": "text",
            "text": f"Exemplar {i} — full page (target outlined in red):",
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{page_mime};base64,{page_b64}"},
        })
        content.append({
            "type": "text",
            "text": f"Exemplar {i} — zoomed crop:",
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{crop_mime};base64,{crop_b64}"},
        })

    messages: list[Message] = cast(list[Message], [
        {"role": "system", "content": STYLE_PROBE_SYSTEM},
        {"role": "user", "content": content},
    ])

    return client.chat_parsed(messages, response_format=StyleClusterLabel, temperature=0.0)


def _signature_to_human(sig: str) -> str:
    parts = dict(kv.split("=", 1) for kv in sig.split("|") if "=" in kv)
    fs_map = {"xs": "small font", "s": "slightly small font", "m": "normal font", "l": "large font", "xl": "very large font"}
    hints = [
        fs_map.get(parts.get("fs", "m"), "normal font"),
        f"width={parts.get('w', '?')}",
        f"x_pos={parts.get('cx', '?')}",
        f"left_indent={parts.get('il', '?')}",
        f"right_indent={parts.get('ir', '?')}",
        f"lines={parts.get('sl', '?')}",
    ]
    if parts.get("orient") == "vert":
        hints.append("vertical text")
    if parts.get("pat") != "none":
        hints.append(f"pattern={parts.get('pat', '')}")
    return ", ".join(hints)


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_ROLE_COLORS: dict[str, str] = {
    "body": "#eee",
    "chapter_title": "#ffe0b2",
    "section_title": "#fff9c4",
    "subsection_title": "#f0f4c3",
    "footnote": "#e3f2fd",
    "page_header": "#fce4ec",
    "page_footer": "#fce4ec",
    "epigraph": "#e8f5e9",
    "blockquote": "#e8f5e9",
    "caption": "#f3e5f5",
    "list_item": "#e0f7fa",
    "poem": "#fff3e0",
    "other": "#f5f5f5",
}


def _render_html_report(
    report_path: Path,
    data: dict[str, Any],
    book_dir: Path,
) -> None:
    direct_rows: list[str] = []
    cluster_rows: list[str] = []

    for proto in data["prototypes"]:
        role = proto["role"]
        conf = proto["confidence"]
        support = proto["support"]
        pid = proto["id"]
        sig = proto["signature"]
        source = proto.get("source", "cluster+vlm")

        role_color = _ROLE_COLORS.get(role, "#fafafa")

        exemplar_imgs = ""
        for ex in proto.get("exemplars", []):
            crop_rel = ex.get("crop", "")
            ex_role = ex.get("role", "")
            ex_conf = ex.get("confidence", "")
            ex_reason = ex.get("reason", "")
            img_path = book_dir / crop_rel
            if img_path.exists():
                conf_str = f"{ex_conf:.2f}" if isinstance(ex_conf, (int, float)) else str(ex_conf)
                exemplar_imgs += (
                    f'<div style="display:inline-block;margin:4px;vertical-align:top">'
                    f'<img src="{crop_rel}" style="max-width:300px;max-height:200px;border:1px solid #ccc">'
                    f'<br><small>{ex_role} ({conf_str}) — {ex_reason}</small>'
                    f"</div>"
                )

        row = (
            f'<tr style="background:{role_color}">'
            f"<td>{pid}</td>"
            f"<td><b>{role}</b></td>"
            f"<td>{conf:.2f}</td>"
            f"<td>{support}</td>"
            f'<td style="font-size:11px;word-break:break-all">{sig}</td>'
            f"<td>{exemplar_imgs}</td>"
            f"</tr>"
        )

        if source == "docling":
            direct_rows.append(row)
        else:
            cluster_rows.append(row)

    def _table(rows: list[str]) -> str:
        return (
            "<table><thead><tr>"
            "<th>ID</th><th>Role</th><th>Conf</th><th>Support</th><th>Signature</th><th>Exemplars</th>"
            "</tr></thead><tbody>"
            + "\n".join(rows)
            + "</tbody></table>"
        )

    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Style Prototypes</title>"
        "<style>body{font-family:sans-serif;font-size:13px}table{border-collapse:collapse;width:100%;margin-bottom:24px}"
        "th,td{border:1px solid #ccc;padding:4px 8px;vertical-align:top}th{background:#ddd}"
        "h2{margin-top:32px}</style>"
        "</head><body>"
        f"<h1>Style Prototypes — {data['book']}</h1>"
        f"<p>Blocks: {data['n_blocks']} | Direct (docling): {data['n_direct']} | "
        f"Clustered (TEXT): {data['n_clustered']} | "
        f"Clusters: {data['n_clusters']} | "
        f"Line pitch: {data['book_line_pitch']} pt</p>"
        "<h2>1. Docling-classified (trust label)</h2>"
        + _table(direct_rows)
        + "<h2>2. Clustered from TEXT-label blocks (VLM-labelled)</h2>"
        + _table(cluster_rows)
        + "</body></html>"
    )

    report_path.write_text(html, encoding="utf-8")
