"""Stage 7 — EPUB3 generation from Semantic IR."""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from pathlib import Path

from ebooklib import epub

_FN_MARKER_RE = re.compile(r"\x02(fn-\d+-[^\x03]*)\x03")

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


def build_epub(
    semantic_path: Path,
    out_path: Path,
    *,
    images_dir: Path | None = None,
    registry_path: Path | None = None,
) -> None:
    book_model = Book.model_validate_json(semantic_path.read_text(encoding="utf-8"))
    registry = _load_registry(registry_path) if registry_path else None
    css = _generate_css(registry)

    ebook = epub.EpubBook()
    ebook.set_identifier(str(uuid.uuid4()))
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

    spine: list[str | epub.EpubHtml] = ["nav"]
    # entries: (level, href, title, uid)
    toc_entries: list[tuple[int, str, str, str]] = []

    for i, chapter in enumerate(book_model.chapters):
        xhtml_name = f"chap{i:04d}.xhtml"
        ch_id = chapter.id or f"chap{i:04d}"
        body_html, footnotes_html = _render_chapter(chapter, ch_id, figure_to_filename)
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
                lvl = min(block.level, 6)
                toc_entries.append((lvl, href, block.text, block.id))

    ebook.toc = _build_nested_toc(toc_entries)
    ebook.spine = spine
    ebook.add_item(epub.EpubNcx())
    ebook.add_item(epub.EpubNav())

    epub.write_epub(str(out_path), ebook)


def _map_figures_to_images(
    book_model: Book, images_dir: Path | None, ebook: epub.EpubBook
) -> dict[int, str]:
    """Map each Figure block (by id()) to its disk filename, and register images with ebook."""
    figure_to_filename: dict[int, str] = {}
    if images_dir is None or not images_dir.exists():
        return figure_to_filename

    figures_by_page: dict[int, list[Figure]] = defaultdict(list)
    for chapter in book_model.chapters:
        for block in chapter.blocks:
            if isinstance(block, Figure):
                figures_by_page[block.provenance.page].append(block)

    registered: set[str] = set()
    for page, figs in figures_by_page.items():
        disk_files = sorted(images_dir.glob(f"p{page:04d}_*.png"))
        for ordinal, fig in enumerate(figs):
            if ordinal >= len(disk_files):
                log.warning("no image file for figure on page %d (ordinal %d)", page, ordinal)
                continue
            fname = disk_files[ordinal].name
            figure_to_filename[id(fig)] = fname
            if fname not in registered:
                stem = Path(fname).stem
                ebook.add_item(epub.EpubItem(
                    uid=f"img-{stem}",
                    file_name=f"images/{fname}",
                    media_type="image/png",
                    content=disk_files[ordinal].read_bytes(),
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
    chapter: Chapter, chapter_id: str, figure_to_filename: dict[int, str]
) -> tuple[str, str]:
    """Return (body_html, footnotes_html) for the chapter."""
    # Pre-pass: assign sequential numbers to footnotes within this chapter.
    fn_map: dict[tuple[int, str], tuple[int, str]] = {}
    n = 0
    for block in chapter.blocks:
        if isinstance(block, Footnote):
            key = (block.provenance.page, block.callout)
            if key in fn_map:
                log.warning(
                    "duplicate footnote key (page=%d, callout=%r) in chapter %r",
                    key[0], key[1], chapter_id,
                )
                continue
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
            parts.append(f"<{tag}{id_attr}>{_esc(block.text)}</{tag}>")
        elif isinstance(block, Footnote):
            footnotes.append(block)
            if not block.paired:
                key = (block.provenance.page, block.callout)
                entry = fn_map.get(key)
                if entry:
                    n_val, fn_id = entry
                    parts.append(
                        f'<sup epub:type="noteref"><a href="#{fn_id}">{n_val}</a></sup>'
                    )
        elif isinstance(block, Figure):
            fname = figure_to_filename.get(id(block))
            if fname:
                img_tag = f'<img src="images/{_esc(fname)}" alt="{_esc(block.caption)}"/>'
            else:
                img_tag = ""
            parts.append(
                f'<figure>{img_tag}'
                f'<figcaption>{_esc(block.caption)}</figcaption></figure>'
            )
        elif isinstance(block, Table):
            title_html = f'<p class="table-title">{_esc(block.table_title)}</p>' if block.table_title else ""
            caption_html = f'<p class="table-caption">{_esc(block.caption)}</p>' if block.caption else ""
            parts.append(f"{title_html}{_apply_fn_markers(block.html, fn_map)}{caption_html}")
        elif isinstance(block, Equation):
            parts.append(f'<p class="equation">{_esc(block.latex)}</p>')

    body_html = "\n".join(parts)

    footnotes_html = ""
    if footnotes:
        fn_parts = ['<aside epub:type="footnotes">']
        for fn in footnotes:
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
