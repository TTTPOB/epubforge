"""Style prototype probe — clusters block-level layout features and labels clusters via VLM."""

from __future__ import annotations

import base64
import io
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

console = Console()

_DPI = 150
_MARGIN_PT = 10  # crop margin in PDF points

_LABEL_BUCKET: dict[str, str] = {
    DocItemLabel.SECTION_HEADER.value: "heading",
    DocItemLabel.TITLE.value: "heading",
    DocItemLabel.FOOTNOTE.value: "footnote",
    DocItemLabel.LIST_ITEM.value: "list",
    DocItemLabel.CAPTION.value: "caption",
    DocItemLabel.PAGE_HEADER.value: "phdr",
    DocItemLabel.PAGE_FOOTER.value: "pftr",
    DocItemLabel.TEXT.value: "text",
}

_FS_BUCKETS = [
    (0.80, "xs"), (0.95, "s"), (1.15, "m"), (1.50, "l"),
]
_W_BUCKETS = [(0.35, "narrow"), (0.60, "half"), (0.85, "wide")]
_CX_BUCKETS = [(0.42, "left"), (0.58, "center")]
_ZONE_BUCKETS = [(0.25, "top"), (0.50, "upper"), (0.75, "lower")]
_INDENT_BUCKETS = [(0.05, "none"), (0.15, "small"), (0.30, "medium")]

_RE_CHAPTER = re.compile(r"^\s*第\s*[\d一二三四五六七八九十百零〇]+\s*[章节卷部编]")
_RE_AUTHOR = re.compile(r"(——|—|--|―)\s*[\u4e00-\u9fff\w]{1,15}\s*$")
_RE_FN_MARKER = re.compile(r"^[\d①②③④⑤⑥⑦⑧⑨⑩\[（(]+[\d\]）)]?\s")


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
        console.print(f"[yellow]style_prototypes.json already exists, skipping (use --force to rerun)[/yellow]")
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

    features = [_compute_features(b, book_line_pitch) for b in blocks]
    signatures = [_make_signature(f) for f in features]

    cluster_map: dict[str, list[int]] = defaultdict(list)
    for i, sig in enumerate(signatures):
        cluster_map[sig].append(i)

    sorted_clusters = sorted(cluster_map.items(), key=lambda kv: -len(kv[1]))
    console.print(f"  {len(sorted_clusters)} distinct signatures, {len(blocks)} blocks total")

    cfg.require_vlm()
    client = LLMClient(cfg, use_vlm=True)
    fitz_doc = fitz.open(str(pdf_path))

    styles_dir.mkdir(parents=True, exist_ok=True)

    prototypes: list[dict[str, Any]] = []
    assignments: dict[str, str] = {}

    for idx, (sig, member_indices) in enumerate(track(sorted_clusters, description="Labelling clusters…")):
        vp_id = f"vp_{idx + 1:03d}"
        support = len(member_indices)

        if support < min_cluster_size:
            role = StyleRole.other.value
            confidence = 0.0
            exemplar_data: list[dict[str, Any]] = []
        else:
            sampled = _stratified_sample(member_indices, blocks, max_exemplars)
            vp_dir = styles_dir / vp_id
            vp_dir.mkdir(parents=True, exist_ok=True)

            exemplar_images: list[tuple[str, str]] = []
            exemplar_data = []
            for n, bi in enumerate(sampled):
                b = blocks[bi]
                img_b64, mime = _render_block_crop(fitz_doc, b, doc)
                crop_path = vp_dir / f"{n}.jpg"
                crop_path.write_bytes(base64.b64decode(img_b64))
                exemplar_images.append((img_b64, mime))
                exemplar_data.append({
                    "page": b["page_no"],
                    "ref": b["self_ref"],
                    "bbox": b["bbox_list"],
                    "crop": f"styles/{vp_id}/{n}.jpg",
                })

            sig_hint = _signature_to_human(sig, features[member_indices[0]])
            label_result = _label_cluster(client, exemplar_images, sig_hint)

            for n, ex_label in enumerate(label_result.exemplars):
                exemplar_data[n]["role"] = ex_label.role.value
                exemplar_data[n]["confidence"] = ex_label.confidence
                exemplar_data[n]["reason"] = ex_label.reason

            roles = [e.role for e in label_result.exemplars]
            majority_role = max(set(roles), key=roles.count)
            avg_conf = sum(e.confidence for e in label_result.exemplars) / len(label_result.exemplars)
            role = majority_role.value
            confidence = round(avg_conf, 3)

        proto: dict[str, Any] = {
            "id": vp_id,
            "signature": sig,
            "support": support,
            "role": role,
            "confidence": confidence,
            "exemplars": exemplar_data,
        }
        prototypes.append(proto)

        for bi in member_indices:
            assignments[blocks[bi]["self_ref"]] = vp_id

    fitz_doc.close()

    output = {
        "book": book_name,
        "n_blocks": len(blocks),
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
# Block extraction helpers
# ---------------------------------------------------------------------------

def _extract_all_blocks(doc: DoclingDocument) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in doc.texts:
        text: str = getattr(item, "text", "") or ""
        if not text.strip():
            continue
        for prov in item.prov:
            if prov.bbox is None:
                continue
            page = doc.pages.get(prov.page_no)
            if page is None or page.size is None:
                continue
            blocks.append({
                "self_ref": item.self_ref,
                "label": item.label.value if hasattr(item.label, "value") else str(item.label),
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
    heights: list[float] = []
    for b in blocks:
        if b["label"] != DocItemLabel.TEXT.value:
            continue
        text: str = b["text"]
        if len(text) > 30:
            continue
        bbox = b["bbox"]
        h = abs(bbox.b - bbox.t)
        if 2.0 < h < 200.0:
            heights.append(h)
    if not heights:
        return 14.0
    return statistics.median(heights)


# ---------------------------------------------------------------------------
# Feature extraction and signature bucketing
# ---------------------------------------------------------------------------

def _compute_features(b: dict[str, Any], book_line_pitch: float) -> dict[str, Any]:
    bbox = b["bbox"]
    page_w: float = b["page_w"]
    page_h: float = b["page_h"]

    # Normalise to top-left origin for geometry calculations.
    # In BOTTOMLEFT origin: t > b (t is top, larger y value).
    # TL-top = page_h - BL-t; TL-bottom = page_h - BL-b.
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
    x_center = (left + right) / 2 / page_w
    left_indent = left / page_w
    right_indent = (page_w - right) / page_w
    top_norm = top / page_h

    text: str = b["text"]
    line_count_hint = max(1, len(text.split("\n")))
    if line_count_hint == 1:
        line_count_hint = max(1, round(height_px / book_line_pitch))

    line_pitch = height_px / line_count_hint if line_count_hint > 0 else book_line_pitch
    font_scale = line_pitch / book_line_pitch if book_line_pitch > 0 else 1.0

    char_count = len(text)
    starts_chapter = bool(_RE_CHAPTER.match(text))
    ends_author = bool(_RE_AUTHOR.search(text) and char_count < 40)
    looks_footnote_marker = bool(_RE_FN_MARKER.match(text))
    is_single_line = (line_count_hint == 1)

    label_bucket = _LABEL_BUCKET.get(b["label"], "other")

    return {
        "font_scale": font_scale,
        "width": width,
        "x_center": x_center,
        "top": top_norm,
        "left_indent": left_indent,
        "right_indent": right_indent,
        "line_count": line_count_hint,
        "is_single_line": is_single_line,
        "starts_chapter": starts_chapter,
        "ends_author": ends_author,
        "looks_footnote_marker": looks_footnote_marker,
        "char_count": char_count,
        "label_bucket": label_bucket,
    }


def _make_signature(f: dict[str, Any]) -> str:
    fs = _bucket(f["font_scale"], _FS_BUCKETS, "xl")
    w = _bucket(f["width"], _W_BUCKETS, "full")
    cx = _bucket(f["x_center"], _CX_BUCKETS, "right")
    zone = _bucket(f["top"], _ZONE_BUCKETS, "bottom")
    il = _bucket(f["left_indent"], _INDENT_BUCKETS, "large")
    ir = _bucket(f["right_indent"], _INDENT_BUCKETS, "large")
    sl = str(min(f["line_count"], 10))

    if f["starts_chapter"]:
        pat = "chapter"
    elif f["ends_author"]:
        pat = "author"
    elif f["looks_footnote_marker"]:
        pat = "fn"
    else:
        pat = "none"

    lbl = f["label_bucket"]
    return f"label={lbl}|fs={fs}|w={w}|cx={cx}|zone={zone}|il={il}|ir={ir}|sl={sl}|pat={pat}"


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
# Block crop rendering
# ---------------------------------------------------------------------------

def _render_block_crop(
    fitz_doc: fitz.Document,
    block: dict[str, Any],
    doc: DoclingDocument,
) -> tuple[str, str]:
    pno = block["page_no"]
    page = fitz_doc[pno - 1]
    page_h_fitz = page.rect.height

    bbox = block["bbox"]
    if block["coord_origin"] == CoordOrigin.BOTTOMLEFT:
        # BOTTOMLEFT: t is top (larger y). TL-top = page_h - BL-t.
        t = page_h_fitz - bbox.t
        b_coord = page_h_fitz - bbox.b
    else:
        t = bbox.t
        b_coord = bbox.b

    l = bbox.l - _MARGIN_PT
    r = bbox.r + _MARGIN_PT
    t = t - _MARGIN_PT
    b_coord = b_coord + _MARGIN_PT

    clip = fitz.Rect(
        max(0, l), max(0, t),
        min(page.rect.width, r), min(page_h_fitz, b_coord),
    )
    mat = fitz.Matrix(_DPI / 72, _DPI / 72)
    pix = page.get_pixmap(matrix=mat, clip=clip)
    buf = io.BytesIO(pix.tobytes("jpg", jpg_quality=80))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


# ---------------------------------------------------------------------------
# VLM call
# ---------------------------------------------------------------------------

def _label_cluster(
    client: LLMClient,
    exemplar_images: list[tuple[str, str]],
    sig_hint: str,
) -> StyleClusterLabel:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Cluster signature hints: {sig_hint}\n"
                f"Below are {len(exemplar_images)} cropped block image(s) from this cluster. "
                "Label each one in order."
            ),
        }
    ]
    for img_b64, mime in exemplar_images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{img_b64}"},
        })

    messages: list[Message] = cast(list[Message], [
        {"role": "system", "content": STYLE_PROBE_SYSTEM},
        {"role": "user", "content": content},
    ])

    return client.chat_parsed(messages, response_format=StyleClusterLabel, temperature=0.0)


def _signature_to_human(sig: str, features: dict[str, Any]) -> str:
    parts = dict(kv.split("=", 1) for kv in sig.split("|") if "=" in kv)
    hints: list[str] = []
    hints.append(f"label={parts.get('label', '?')}")
    fs_map = {"xs": "small font", "s": "slightly small font", "m": "normal font", "l": "large font", "xl": "very large font"}
    hints.append(fs_map.get(parts.get("fs", "m"), "normal font"))
    hints.append(f"width={parts.get('w', '?')}")
    hints.append(f"x_pos={parts.get('cx', '?')}")
    hints.append(f"page_zone={parts.get('zone', '?')}")
    hints.append(f"lines≈{parts.get('sl', '?')}")
    if parts.get("pat") != "none":
        hints.append(f"pattern={parts.get('pat', '')}")
    return ", ".join(hints)


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def _render_html_report(
    report_path: Path,
    data: dict[str, Any],
    book_dir: Path,
) -> None:
    rows: list[str] = []
    for proto in data["prototypes"]:
        role = proto["role"]
        conf = proto["confidence"]
        support = proto["support"]
        vp_id = proto["id"]
        sig = proto["signature"]

        role_color = {
            "body": "#eee",
            "chapter_title": "#ffe0b2",
            "section_title": "#fff9c4",
            "subsection_title": "#f0f4c3",
            "footnote": "#e3f2fd",
            "page_header": "#fce4ec",
            "page_footer": "#fce4ec",
            "epigraph": "#e8f5e9",
            "blockquote": "#e8f5e9",
            "other": "#f5f5f5",
        }.get(role, "#fafafa")

        exemplar_imgs = ""
        for ex in proto.get("exemplars", []):
            crop_rel = ex.get("crop", "")
            ex_role = ex.get("role", "")
            ex_conf = ex.get("confidence", "")
            ex_reason = ex.get("reason", "")
            img_path = book_dir / crop_rel
            if img_path.exists():
                exemplar_imgs += (
                    f'<div style="display:inline-block;margin:4px;vertical-align:top">'
                    f'<img src="{crop_rel}" style="max-width:300px;max-height:200px;border:1px solid #ccc">'
                    f'<br><small>{ex_role} ({ex_conf:.2f}) — {ex_reason}</small>'
                    f"</div>"
                )

        rows.append(
            f'<tr style="background:{role_color}">'
            f"<td>{vp_id}</td>"
            f"<td><b>{role}</b></td>"
            f"<td>{conf:.2f}</td>"
            f"<td>{support}</td>"
            f'<td style="font-size:11px;word-break:break-all">{sig}</td>'
            f"<td>{exemplar_imgs}</td>"
            f"</tr>"
        )

    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Style Prototypes</title>"
        "<style>body{font-family:sans-serif;font-size:13px}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ccc;padding:4px 8px;vertical-align:top}th{background:#ddd}</style>"
        "</head><body>"
        f"<h1>Style Prototypes — {data['book']}</h1>"
        f"<p>Blocks: {data['n_blocks']} | Clusters: {data['n_clusters']} | "
        f"Line pitch: {data['book_line_pitch']} pt</p>"
        "<table><thead><tr>"
        "<th>ID</th><th>Role</th><th>Conf</th><th>Support</th><th>Signature</th><th>Exemplars</th>"
        "</tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table></body></html>"
    )

    report_path.write_text(html, encoding="utf-8")
