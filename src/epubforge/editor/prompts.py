"""Prompt rendering helpers for editor subagents."""

from __future__ import annotations

import json
from pathlib import Path

from epubforge.editor.memory import EditMemory
from epubforge.ir.semantic import Book, Chapter


SCANNER_PROMPT = """你是 epubforge 编辑系统的 scanner subagent。

任务：通读 chapter（uid={chapter_uid}，标题：{chapter_title}），完成以下事：
1. 读取 {book_path} 中该 chapter 的所有 block
2. 参考 docs/rules/*.md 记录新的 conventions、patterns 与 open questions
3. 只在非常确定的小修改时产 EditOp；复杂修改留给 fixer
4. 更新 chapter_status[{chapter_uid}].read_passes += 1

当前 book.op_log_version={book_version}，produce 的每条 op envelope 必须写 base_version={book_version}。
当前 memory 快照：{memory_snapshot}

如需临时脚本，先调 `epubforge editor run-script {work_dir} --write <desc>` 获取 scratch 路径，
写代码后再调 `epubforge editor run-script {work_dir} --exec <path>` 执行。

输出格式：JSON，包含 `ops` / `memory_patches` / `open_questions` / `notes`。
"""

FIXER_PROMPT = """你是 epubforge 编辑系统的 fixer subagent。

任务：修复以下 audit issues / hints：
{issues_and_hints}

约束：
- 你已持有 chapter {chapter_uid} 的 lease；只对该 chapter 的 block 产 op
- 只产 OpEnvelope；不直接改 book.json
- 复杂判断要基于 memory.conventions；不要臆断
- 所有 op 的 base_version 必须等于当前 book_version={book_version}

当前 book.op_log_version={book_version}
当前 memory 快照：{memory_snapshot}
当前 chapter 标题：{chapter_title}
当前 book.json：{book_path}

输出格式：JSON，包含 `ops` / `memory_patches` / `open_questions` / `notes`。
"""

REVIEWER_PROMPT = """你是 epubforge 编辑系统的 reviewer subagent。

任务：复核以下问题与建议：
{issues_and_hints}

约束：
- 你只能给出审查意见、OpenQuestion 或必要的修正 op
- 所有 op 的 base_version 必须等于当前 book_version={book_version}

当前 book.op_log_version={book_version}
当前 chapter：{chapter_uid} / {chapter_title}
当前 memory 快照：{memory_snapshot}
当前 book.json：{book_path}
"""


def _memory_snapshot(memory: EditMemory) -> str:
    return json.dumps(memory.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)


def _chapter_for_uid(book: Book, chapter_uid: str) -> Chapter:
    for chapter in book.chapters:
        if chapter.uid == chapter_uid:
            return chapter
    raise ValueError(f"chapter not found: {chapter_uid}")


def _issues_block(issues: list[str] | None) -> str:
    if not issues:
        return "- 未显式提供 issues；请先查看 doctor 输出。"
    return "\n".join(f"- {item}" for item in issues)


def render_prompt(
    *,
    kind: str,
    book: Book,
    memory: EditMemory,
    work_dir: Path,
    book_path: Path,
    chapter_uid: str,
    issues: list[str] | None = None,
) -> str:
    chapter = _chapter_for_uid(book, chapter_uid)
    template = {
        "scanner": SCANNER_PROMPT,
        "fixer": FIXER_PROMPT,
        "reviewer": REVIEWER_PROMPT,
    }.get(kind)
    if template is None:
        raise ValueError(f"unsupported prompt kind: {kind}")

    return template.format(
        book_path=book_path,
        book_version=book.op_log_version,
        chapter_title=chapter.title,
        chapter_uid=chapter_uid,
        issues_and_hints=_issues_block(issues),
        memory_snapshot=_memory_snapshot(memory),
        work_dir=work_dir,
    )
