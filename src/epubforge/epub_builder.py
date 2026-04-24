"""Stage 7 — EPUB3 generation from Semantic IR."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote as _url_quote

from ebooklib import epub

from epubforge.io import EDITABLE_BOOK_PATH
from epubforge.stage3_artifacts import Stage3ContractError, load_active_stage3_manifest

_FN_MARKER_RE = re.compile(r"\x02(fn-\d+-[^\x03]*)\x03")
_OPF_MODIFIED_RE = re.compile(r"(<meta property=\"dcterms:modified\">)([^<]+)(</meta>)")
_FIXED_EPUB_MODIFIED = "2000-01-01T00:00:00Z"
_FIXED_ZIP_TIMESTAMP = (2000, 1, 1, 0, 0, 0)

log = logging.getLogger(__name__)

from epubforge.ir.semantic import (
    Book,
    Chapter,
    Equation,
    Figure,
    Footnote,
    Heading,
    Paragraph,
    Table,
)
from epubforge.ir.style_registry import StyleRegistry

_CSS_BASE = """
body { font-family: serif; line-height: 1.6; margin: 1em 2em; }
h1 { font-size: 1.8em; margin-top: 2em; }
h2 { font-size: 1.4em; margin-top: 1.5em; }
h2.centered { text-align: center; }
h2.centered-section { text-align: center; break-before: page; page-break-before: always; }
h3 { font-size: 1.2em; margin-top: 1.2em; }
p { margin: 0.5em 0; text-indent: 1.5em; }
figure { margin: 1em 0; text-align: center; }
figcaption { font-size: 0.9em; color: #555; }
table { border-collapse: collapse; width: 100%; margin: 0.3em 0 0; }
td, th { border: 1px solid #ccc; padding: 0.3em 0.6em; }
p.table-title { font-weight: bold; margin: 1em 0 0.2em; text-indent: 0; }
p.table-caption { font-size: 0.88em; color: #555; margin: 0.2em 0 1em; text-indent: 0; }
aside.footnote { font-size: 0.85em; border-top: 1px solid #ccc; margin-top: 2em; padding-top: 0.5em; }
.equation { font-family: monospace; margin: 0.8em 0; }
p.epigraph { font-style: italic; margin: 1em 3em; text-indent: 0; }
p.blockquote { margin: 1em 2em; text-indent: 0; }
p.poem { white-space: pre-wrap; text-indent: 0; text-align: center; margin: 1em 0; }
p.caption { font-size: 0.88em; color: #555; text-indent: 0; }
p.attribution { text-align: right; font-style: italic; text-indent: 0; }
p.dedication { text-align: center; font-style: italic; margin: 2em 0; }
p.centered-bold { text-align: center; font-weight: bold; text-indent: 0; }
p.preface-note { font-size: 0.9em; margin: 0.5em 1em; text-indent: 0; }
"""


def _generate_css(registry: StyleRegistry | None) -> str:
    if not registry:
        return _CSS_BASE
    extra: list[str] = []
    existing_classes = {
        line.split("{")[0].strip().lstrip("p.").lstrip(".")
        for line in _CSS_BASE.splitlines()
        if "{" in line and line.strip().startswith("p.")
    }
    for style in registry.styles:
        if style.css_class in existing_classes:
            continue
        rules = "; ".join(f"{k}: {v}" for k, v in style.css_rules.items())
        extra.append(f"p.{style.css_class} {{ {rules}; }}")
    if not extra:
        return _CSS_BASE
    return _CSS_BASE + "\n" + "\n".join(extra)


def _load_registry(registry_path: Path) -> StyleRegistry | None:
    try:
        return StyleRegistry.model_validate_json(registry_path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("epub_builder: failed to load style registry from %s", registry_path)
        return None


def resolve_build_source(work_dir: Path) -> Path:
    """Resolve the semantic source file for EPUB build.

    Priority order:
    1. edit_state/book.json  (editable, curated)
    2. 05_semantic.json      (post-editor semantic)
    3. 05_semantic_raw.json  (raw assembler output)
    """
    candidates = [
        work_dir / EDITABLE_BOOK_PATH,
        work_dir / "05_semantic.json",
        work_dir / "05_semantic_raw.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"No build source found under {work_dir}: checked "
        + ", ".join(str(c.relative_to(work_dir)) for c in candidates)
    )


def _check_build_source_freshness(work_dir: Path, source_manifest_sha256: str) -> None:
    """Raise Stage3ContractError if an active manifest exists but its sha differs."""
    try:
        pointer, _ = load_active_stage3_manifest(work_dir)
    except Stage3ContractError:
        # No active manifest available — staleness check not possible, allow build.
        return
    if pointer.manifest_sha256 != source_manifest_sha256:
        raise Stage3ContractError(
            f"Build source is stale: source manifest_sha256={source_manifest_sha256} "
            f"does not match active manifest_sha256={pointer.manifest_sha256}. "
            "Re-run assemble (Stage 4) to refresh the build source."
        )


def build_epub(
    semantic_path: Path,
    out_path: Path,
    *,
    images_dir: Path | None = None,
    registry_path: Path | None = None,
    work_dir: Path | None = None,
) -> None:
    book_model = Book.model_validate_json(semantic_path.read_text(encoding="utf-8"))

    # Stale manifest check: if the build source records a manifest sha and an
    # active manifest exists, fail when the shas don't match to prevent building
    # from stale extraction data.
    if book_model.extraction.stage3_manifest_sha256:
        work_dir = work_dir or semantic_path.parent
        _check_build_source_freshness(
            work_dir, book_model.extraction.stage3_manifest_sha256
        )

    registry = _load_registry(registry_path) if registry_path else None
    css = _generate_css(registry)

    ebook = epub.EpubBook()
    ebook.set_identifier(_deterministic_identifier(book_model, css=css, images_dir=images_dir))
    ebook.set_title(book_model.title)
    ebook.set_language(book_model.language)
    for author in book_model.authors:
        ebook.add_author(author)

    css_item = epub.EpubItem(
        uid="style", file_name="style/main.css",
        media_type="text/css", content=css.encode(),
    )
    ebook.add_item(css_item)

    figure_to_filename = _map_figures_to_images(book_model, images_dir, ebook)

    # Pre-pass 1: index all Footnote objects by (page, callout).
    all_footnotes_by_key: dict[tuple[int, str], Footnote] = {}
    for chapter in book_model.chapters:
        for block in chapter.blocks:
            if isinstance(block, Footnote):
                key = (block.provenance.page, block.callout)
                if key not in all_footnotes_by_key:
                    all_footnotes_by_key[key] = block

    # Pre-pass 2: find cross-chapter markers and compute "borrowed" footnotes.
    # A footnote is "borrowed" when a marker \x02fn-PAGE-CALLOUT\x03 appears in
    # chapter A but the matching Footnote block lives in chapter B.  The footnote
    # body is rendered in chapter A (the callout's home) and suppressed in B.
    borrowed_by: dict[int, list[Footnote]] = {}   # ch_idx → borrowed Footnotes
    borrowed_keys: set[tuple[int, str]] = set()    # keys removed from source chapter

    for i, chapter in enumerate(book_model.chapters):
        local_keys: set[tuple[int, str]] = {
            (b.provenance.page, b.callout)
            for b in chapter.blocks if isinstance(b, Footnote)
        }
        ch_borrowed: list[Footnote] = []
        seen: set[tuple[int, str]] = set()
        for block in chapter.blocks:
            if isinstance(block, Paragraph):
                texts_to_scan = [block.text]
            elif isinstance(block, Table):
                texts_to_scan = [block.html, block.table_title, block.caption]
            else:
                continue
            for text in texts_to_scan:
                for m in _FN_MARKER_RE.finditer(text):
                    raw = m.group(1)
                    ps = raw.split("-", 2)
                    if len(ps) < 3:
                        continue
                    try:
                        page = int(ps[1])
                        callout = ps[2]
                    except ValueError:
                        continue
                    key = (page, callout)
                    if key not in local_keys and key in all_footnotes_by_key and key not in seen:
                        ch_borrowed.append(all_footnotes_by_key[key])
                        borrowed_keys.add(key)
                        seen.add(key)
        if ch_borrowed:
            borrowed_by[i] = ch_borrowed

    spine: list[str | epub.EpubHtml] = ["nav"]
    # entries: (level, href, title, uid)
    toc_entries: list[tuple[int, str, str, str]] = []

    for i, chapter in enumerate(book_model.chapters):
        xhtml_name = f"chap{i:04d}.xhtml"
        ch_id = chapter.id or f"chap{i:04d}"
        body_html, footnotes_html = _render_chapter(
            chapter, ch_id, figure_to_filename,
            borrowed_by.get(i), borrowed_keys or None,
        )
        full_html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops">\n'
            '<head><meta charset="utf-8"/>'
            f'<title>{_esc(chapter.title)}</title>'
            '<link rel="stylesheet" href="../style/main.css"/>'
            '</head>\n'
            f'<body>\n<h1 id="{_esc(ch_id)}">{_esc(chapter.title)}</h1>\n'
            f'{body_html}\n{footnotes_html}\n'
            '</body>\n</html>'
        )
        chap_item = epub.EpubHtml(
            title=chapter.title,
            file_name=xhtml_name,
            lang=book_model.language,
        )
        chap_item.content = full_html.encode("utf-8")
        chap_item.add_item(css_item)
        ebook.add_item(chap_item)
        spine.append(chap_item)
        toc_entries.append((1, xhtml_name, chapter.title, ch_id))
        for block in chapter.blocks:
            if isinstance(block, Heading) and block.id:
                href = f"{xhtml_name}#{block.id}"
                lvl = min(block.level + 1, 6)  # +1: headings are always children of their chapter
                toc_entries.append((lvl, href, block.text, block.id))

    ebook.toc = _build_nested_toc(toc_entries)
    ebook.spine = spine
    ebook.add_item(epub.EpubNcx())
    ebook.add_item(epub.EpubNav())

    epub.write_epub(str(out_path), ebook)
    _normalize_epub_archive(out_path)

    n_chapters = len(book_model.chapters)
    n_blocks = sum(len(ch.blocks) for ch in book_model.chapters)
    n_images = len(figure_to_filename)
    size = out_path.stat().st_size
    log.info(
        "epub_builder: chapters=%d blocks=%d images=%d size=%d bytes → %s",
        n_chapters, n_blocks, n_images, size, out_path.name,
    )


def _deterministic_identifier(book_model: Book, *, css: str, images_dir: Path | None) -> str:
    payload = {
        "title": book_model.title,
        "language": book_model.language,
        "authors": list(book_model.authors),
        "chapters": [_chapter_identity(chapter) for chapter in book_model.chapters],
        "css": css,
        "images": _image_manifest(images_dir),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return str(uuid.UUID(digest[:32]))


def _chapter_identity(chapter: Chapter) -> dict[str, object]:
    return {
        "title": chapter.title,
        "level": chapter.level,
        "id": chapter.id,
        "blocks": [_block_identity(block) for block in chapter.blocks],
    }


def _block_identity(block: Paragraph | Heading | Footnote | Figure | Table | Equation) -> dict[str, object]:
    payload = block.model_dump(mode="json")
    payload.pop("uid", None)
    return payload


def _image_manifest(images_dir: Path | None) -> dict[str, str]:
    if images_dir is None or not images_dir.exists():
        return {}
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(images_dir.glob("*.png"))
    }


def _normalize_epub_archive(out_path: Path) -> None:
    with zipfile.ZipFile(out_path, "r") as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out_path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(tmp_path, "w") as target:
            for info, payload in entries:
                data = payload
                if info.filename == "EPUB/content.opf":
                    data = _OPF_MODIFIED_RE.sub(
                        rf"\1{_FIXED_EPUB_MODIFIED}\3",
                        payload.decode("utf-8"),
                        count=1,
                    ).encode("utf-8")

                normalized = zipfile.ZipInfo(filename=info.filename, date_time=_FIXED_ZIP_TIMESTAMP)
                normalized.compress_type = info.compress_type
                normalized.comment = info.comment
                normalized.extra = info.extra
                normalized.create_system = info.create_system
                normalized.create_version = info.create_version
                normalized.extract_version = info.extract_version
                normalized.flag_bits = info.flag_bits
                normalized.volume = info.volume
                normalized.internal_attr = info.internal_attr
                normalized.external_attr = info.external_attr
                target.writestr(normalized, data)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _map_figures_to_images(
    book_model: Book, images_dir: Path | None, ebook: epub.EpubBook
) -> dict[int, str]:
    """Map each Figure block (by id()) to its disk filename using Figure.image_ref.

    Only Figure.image_ref is used to locate the image.  Page ordinal fallback
    is intentionally absent: if image_ref is None or the file is missing on
    disk, a warning is logged and the figure is not registered.
    """
    figure_to_filename: dict[int, str] = {}
    if images_dir is None or not images_dir.exists():
        return figure_to_filename

    registered: set[str] = set()
    for chapter in book_model.chapters:
        for block in chapter.blocks:
            if not isinstance(block, Figure):
                continue
            if not block.image_ref:
                log.warning(
                    "figure on page %d has no image_ref — skipping image registration",
                    block.provenance.page,
                )
                continue
            img_path = images_dir / block.image_ref
            if not img_path.exists():
                log.warning(
                    "figure image_ref=%r not found on disk — skipping (page %d)",
                    block.image_ref,
                    block.provenance.page,
                )
                continue
            fname = img_path.name
            figure_to_filename[id(block)] = fname
            if fname not in registered:
                stem = img_path.stem
                ebook.add_item(epub.EpubItem(
                    uid=f"img-{stem}",
                    file_name=f"images/{fname}",
                    media_type="image/png",
                    content=img_path.read_bytes(),
                ))
                registered.add(fname)

    return figure_to_filename


def _build_nested_toc(
    entries: list[tuple[int, str, str, str]],
) -> list[epub.Link | tuple[epub.Link | epub.Section, list]]:
    """Build a nested ebooklib TOC structure from flat (level, href, title, uid) entries."""
    if not entries:
        return []

    result: list = []
    # stack[i] = list that items at level i+1 are appended to
    stack: list[list] = [result]
    prev_level = 1

    for level, href, title, uid in entries:
        link = epub.Link(href, title, uid)
        if level <= prev_level:
            # same level or shallower: pop back
            target_depth = level - 1
            while len(stack) > target_depth + 1:
                stack.pop()
        else:
            # deeper: the last item in current list becomes a section with children
            children: list = []
            if stack[-1]:
                last = stack[-1][-1]
                if isinstance(last, tuple):
                    # already a (Section/Link, children) pair
                    stack.append(last[1])
                else:
                    # promote to tuple
                    stack[-1][-1] = (last, children)
                    stack.append(children)
            else:
                stack[-1].append((epub.Section(title), children))
                stack.append(children)
        stack[-1].append(link)
        prev_level = level

    return result


def _render_chapter(
    chapter: Chapter,
    chapter_id: str,
    figure_to_filename: dict[int, str],
    borrowed_footnotes: list[Footnote] | None = None,
    borrowed_keys: set[tuple[int, str]] | None = None,
) -> tuple[str, str]:
    """Return (body_html, footnotes_html) for the chapter.

    borrowed_footnotes: Footnote objects whose bodies belong here (callout is in this
    chapter but the Footnote block lives in the next chapter).
    borrowed_keys: (page, callout) pairs borrowed away by a previous chapter — skip
    rendering these when encountered in this chapter's block list.
    """
    # Pre-pass: assign sequential numbers to footnotes.
    # 1) Local footnotes (excluding keys borrowed away to another chapter).
    fn_map: dict[tuple[int, str], tuple[int, str]] = {}
    n = 0
    for block in chapter.blocks:
        if isinstance(block, Footnote):
            key = (block.provenance.page, block.callout)
            if borrowed_keys and key in borrowed_keys:
                continue  # body rendered in the chapter that holds the callout
            if key in fn_map:
                log.warning(
                    "duplicate footnote key (page=%d, callout=%r) in chapter %r",
                    key[0], key[1], chapter_id,
                )
                continue
            n += 1
            fn_map[key] = (n, f"{chapter_id}-fn{n}")
    # 2) Borrowed footnotes from next chapter(s).
    for fn in (borrowed_footnotes or []):
        key = (fn.provenance.page, fn.callout)
        if key not in fn_map:
            n += 1
            fn_map[key] = (n, f"{chapter_id}-fn{n}")

    parts: list[str] = []
    footnotes: list[Footnote] = []

    for block in chapter.blocks:
        if isinstance(block, Paragraph):
            parts.append(_render_paragraph(block, fn_map))
        elif isinstance(block, Heading):
            tag = f"h{min(block.level + 1, 6)}"  # h1 reserved for chapter title
            id_attr = f' id="{_esc(block.id)}"' if block.id else ""
            cls_attr = f' class="{_esc(block.style_class)}"' if block.style_class else ""
            parts.append(f"<{tag}{id_attr}{cls_attr}>{_esc(block.text)}</{tag}>")
        elif isinstance(block, Footnote):
            key = (block.provenance.page, block.callout)
            if borrowed_keys and key in borrowed_keys:
                continue  # suppressed — body is in the borrowing chapter
            if not block.orphan:
                footnotes.append(block)
            if not block.paired and not block.orphan:
                entry = fn_map.get(key)
                if entry:
                    n_val, fn_id = entry
                    parts.append(
                        f'<sup epub:type="noteref"><a href="#{fn_id}">{n_val}</a></sup>'
                    )
        elif isinstance(block, Figure):
            fname = figure_to_filename.get(id(block))
            if fname:
                img_tag = f'<img src="images/{_url_quote(fname, safe="_-.")}" alt="{_esc(block.caption)}"/>'
            else:
                img_tag = ""
            parts.append(
                f'<figure>{img_tag}'
                f'<figcaption>{_esc(block.caption)}</figcaption></figure>'
            )
        elif isinstance(block, Table):
            title_html = f'<p class="table-title">{_render_inline(block.table_title, fn_map)}</p>' if block.table_title else ""
            caption_html = f'<p class="table-caption">{_render_inline(block.caption, fn_map)}</p>' if block.caption else ""
            parts.append(f"{title_html}{_apply_fn_markers(block.html, fn_map)}{caption_html}")
        elif isinstance(block, Equation):
            parts.append(f'<p class="equation">{_esc(block.latex)}</p>')

    body_html = "\n".join(parts)

    all_footnotes_to_render: list[Footnote] = footnotes + list(borrowed_footnotes or [])
    footnotes_html = ""
    if all_footnotes_to_render:
        fn_parts = ['<aside epub:type="footnotes">']
        for fn in all_footnotes_to_render:
            key = (fn.provenance.page, fn.callout)
            entry = fn_map.get(key)
            if entry:
                n_val, fn_id = entry
            else:
                n_val, fn_id = 0, f"{chapter_id}-fn0"
            fn_parts.append(
                f'<aside epub:type="footnote" id="{fn_id}">'
                f'<p><sup>{n_val}</sup> {_esc(fn.text)}</p></aside>'
            )
        fn_parts.append("</aside>")
        footnotes_html = "\n".join(fn_parts)

    return body_html, footnotes_html


def _render_paragraph(p: Paragraph, fn_map: dict[tuple[int, str], tuple[int, str]]) -> str:
    cls = p.style_class or (p.role if p.role != "body" else None)
    cls_attr = f' class="{_esc(cls)}"' if cls else ""
    if p.display_lines:
        inner = "<br/>".join(_esc(line) for line in p.display_lines)
        inner = _apply_fn_markers(inner, fn_map)
    else:
        inner = _render_inline(p.text, fn_map)
    return f"<p{cls_attr}>{inner}</p>"


def _apply_fn_markers(html: str, fn_map: dict[tuple[int, str], tuple[int, str]]) -> str:
    """Replace \x02fn-PAGE-CALLOUT\x03 markers with sequential noteref links."""
    def to_link(m: re.Match[str]) -> str:
        raw = m.group(1)  # "fn-PAGE-CALLOUT"
        parts = raw.split("-", 2)
        try:
            page = int(parts[1])
            callout = parts[2]
        except (IndexError, ValueError):
            return m.group(0)
        entry = fn_map.get((page, callout))
        if entry is None:
            log.warning("fn marker page=%d callout=%r has no fn_map entry", page, callout)
            return m.group(0)
        n_val, fn_id = entry
        return f'<sup epub:type="noteref"><a href="#{fn_id}">{n_val}</a></sup>'
    return _FN_MARKER_RE.sub(to_link, html)


def _render_inline(text: str, fn_map: dict[tuple[int, str], tuple[int, str]]) -> str:
    """Escape HTML then convert fn markers to sequential noteref links."""
    escaped = _esc(text).replace("\n", "<br/>")
    return _apply_fn_markers(escaped, fn_map)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
