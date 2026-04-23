"""Hard-rule table structure detectors."""

from __future__ import annotations

import re
from dataclasses import dataclass

from epubforge.audit.models import AuditBundle, AuditIssue
from epubforge.query import find_blocks
from epubforge.ir.semantic import Book, Table


ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
COLSPAN_RE = re.compile(r'colspan\s*=\s*["\']?(\d+)', re.IGNORECASE)
ROWSPAN_RE = re.compile(r'rowspan\s*=\s*["\']?(\d+)', re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class CellSpec:
    colspan: int
    rowspan: int
    text: str


def _parse_cells(row_html: str) -> list[CellSpec]:
    cells: list[CellSpec] = []
    for attrs, inner_html in CELL_RE.findall(row_html):
        colspan_match = COLSPAN_RE.search(attrs)
        rowspan_match = ROWSPAN_RE.search(attrs)
        cells.append(
            CellSpec(
                colspan=int(colspan_match.group(1)) if colspan_match else 1,
                rowspan=int(rowspan_match.group(1)) if rowspan_match else 1,
                text=TAG_RE.sub("", inner_html).strip(),
            )
        )
    return cells


def _row_logical_widths(rows: list[str]) -> list[int]:
    widths: list[int] = []
    active_rowspans: list[int] = []
    for row_html in rows:
        inherited = len(active_rowspans)
        active_rowspans = [remaining - 1 for remaining in active_rowspans if remaining > 1]
        cells = _parse_cells(row_html)
        if not cells and inherited == 0:
            continue
        width = inherited
        for cell in cells:
            width += cell.colspan
            if cell.rowspan > 1:
                active_rowspans.extend([cell.rowspan - 1] * cell.colspan)
        widths.append(width)
    return widths


def detect_table_issues(book: Book) -> AuditBundle:
    issues: list[AuditIssue] = []
    for ref in find_blocks(book, kinds={"table"}):
        block = ref.block
        assert isinstance(block, Table)
        html = block.html
        bodies = TBODY_RE.findall(html)
        join_count = max(0, len(bodies) - 1)
        if join_count > 0:
            issues.append(
                AuditIssue(
                    code="table.double_tbody",
                    page=block.provenance.page,
                    block_index=ref.block_idx,
                    chapter_uid=ref.chapter.uid,
                    block_uid=block.uid,
                    message=f"found {join_count + 1} tbody sections",
                )
            )
        for body_index in range(1, len(bodies)):
            previous_rows = ROW_RE.findall(bodies[body_index - 1])
            current_rows = ROW_RE.findall(bodies[body_index])
            if not previous_rows or not current_rows:
                continue
            first_cells = _parse_cells(current_rows[0])
            if not first_cells:
                continue
            empty_cells = sum(1 for cell in first_cells if not cell.text)
            if empty_cells * 2 >= len(first_cells):
                issues.append(
                    AuditIssue(
                        code="table.split_row_suspected",
                        page=block.provenance.page,
                        block_index=ref.block_idx,
                        chapter_uid=ref.chapter.uid,
                        block_uid=block.uid,
                        message=f"tbody {body_index + 1} starts with {empty_cells}/{len(first_cells)} empty cells",
                    )
                )
                break
        row_widths = _row_logical_widths(ROW_RE.findall(html))
        if row_widths:
            expected = max(row_widths)
            for row_index, width in enumerate(row_widths):
                if width != expected:
                    issues.append(
                        AuditIssue(
                            code="table.column_count_mismatch",
                            page=block.provenance.page,
                            block_index=ref.block_idx,
                            chapter_uid=ref.chapter.uid,
                            block_uid=block.uid,
                            message=f"row {row_index} has {width} logical columns; expected {expected}",
                        )
                    )
                    break
    return AuditBundle(issues=tuple(issues))


__all__ = ["detect_table_issues"]
