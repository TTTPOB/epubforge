"""Stage 6 — EPUB3 generation from Semantic IR."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ebooklib import epub

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

_CSS = """
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
"""


def build_epub(semantic_path: Path, out_path: Path) -> None:
    book_model = Book.model_validate_json(semantic_path.read_text(encoding="utf-8"))
    ebook = epub.EpubBook()
    ebook.set_identifier(str(uuid.uuid4()))
    ebook.set_title(book_model.title)
    ebook.set_language(book_model.language)
    for author in book_model.authors:
        ebook.add_author(author)

    css_item = epub.EpubItem(
        uid="style", file_name="style/main.css",
        media_type="text/css", content=_CSS.encode(),
    )
    ebook.add_item(css_item)

    spine: list[Any] = ["nav"]
    toc: list[Any] = []

    for i, chapter in enumerate(book_model.chapters):
        xhtml_name = f"chap{i:04d}.xhtml"
        body_html, footnotes_html = _render_chapter(chapter)
        full_html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops">\n'
            '<head><meta charset="utf-8"/>'
            f'<title>{_esc(chapter.title)}</title>'
            '<link rel="stylesheet" href="../style/main.css"/>'
            '</head>\n'
            f'<body>\n<h1>{_esc(chapter.title)}</h1>\n'
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
        toc.append(epub.Link(xhtml_name, chapter.title, f"chap{i:04d}"))

    ebook.toc = toc
    ebook.spine = spine
    ebook.add_item(epub.EpubNcx())
    ebook.add_item(epub.EpubNav())

    epub.write_epub(str(out_path), ebook)


def _render_chapter(chapter: Chapter) -> tuple[str, str]:
    """Return (body_html, footnotes_html) for the chapter."""
    parts: list[str] = []
    footnotes: list[Footnote] = []

    for block in chapter.blocks:
        if isinstance(block, Paragraph):
            parts.append(f"<p>{_esc(block.text)}</p>")
        elif isinstance(block, Heading):
            tag = f"h{min(block.level + 1, 6)}"  # h1 reserved for chapter title
            parts.append(f"<{tag}>{_esc(block.text)}</{tag}>")
        elif isinstance(block, Footnote):
            footnotes.append(block)
            parts.append(
                f'<sup epub:type="noteref"><a href="#{_fn_id(block)}">{_esc(block.callout)}</a></sup>'
            )
        elif isinstance(block, Figure):
            img_tag = (
                f'<img src="../images/{_esc(block.image_ref or "")}" alt="{_esc(block.caption)}"/>'
                if block.image_ref else ""
            )
            parts.append(
                f'<figure>{img_tag}'
                f'<figcaption>{_esc(block.caption)}</figcaption></figure>'
            )
        elif isinstance(block, Table):
            title_html = f'<p class="table-title">{_esc(block.table_title)}</p>' if block.table_title else ""
            caption_html = f'<p class="table-caption">{_esc(block.caption)}</p>' if block.caption else ""
            parts.append(f"{title_html}{block.html}{caption_html}")
        elif isinstance(block, Equation):
            parts.append(f'<p class="equation">{_esc(block.latex)}</p>')

    body_html = "\n".join(parts)

    footnotes_html = ""
    if footnotes:
        fn_parts = ['<aside epub:type="footnotes">']
        for fn in footnotes:
            fn_parts.append(
                f'<aside epub:type="footnote" id="{_fn_id(fn)}">'
                f'<p><sup>{_esc(fn.callout)}</sup> {_esc(fn.text)}</p></aside>'
            )
        fn_parts.append("</aside>")
        footnotes_html = "\n".join(fn_parts)

    return body_html, footnotes_html


def _fn_id(fn: Footnote) -> str:
    return f"fn-{fn.provenance.page}-{fn.callout}"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
