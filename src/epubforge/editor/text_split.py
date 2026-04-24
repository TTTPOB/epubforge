"""Text splitting utilities for block text operations."""

from __future__ import annotations

import re

from epubforge.markers import FN_MARKER_FULL_RE

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


def split_text(
    text: str,
    *,
    strategy: str,
    marker_occurrence: int = 1,
    line_index: int | None = None,
    text_match: str | None = None,
    max_splits: int = 1,
    display_lines: list[str] | None = None,
) -> list[str]:
    """Split text using the given strategy.

    Returns a list of text segments (at least 2 for a successful split).
    Raises ValueError on failure.
    """
    if strategy == "at_text_match":
        if text_match is None:
            raise ValueError("text_match is required for at_text_match strategy")
        idx = text.find(text_match)
        if idx <= 0:
            raise ValueError(f"text_match {text_match!r} not found for split")
        return [text[:idx], text[idx:]]

    if strategy == "at_marker":
        matches = list(FN_MARKER_FULL_RE.finditer(text))
        if len(matches) < marker_occurrence:
            raise ValueError("marker occurrence for split not found")
        cut = matches[marker_occurrence - 1].end()
        return [text[:cut], text[cut:]]

    if strategy == "at_line_index":
        if display_lines is None:
            raise ValueError("at_line_index requires display_lines")
        if line_index is None:
            raise ValueError("line_index is required for at_line_index strategy")
        if line_index >= len(display_lines) - 1:
            raise ValueError("line_index must leave content on both sides of split")
        left = "\n".join(display_lines[: line_index + 1])
        right = "\n".join(display_lines[line_index + 1 :])
        return [left, right]

    # at_sentence (default)
    sentence_breaks = [match.end() for match in SENTENCE_SPLIT_RE.finditer(text)]
    if len(sentence_breaks) < max_splits:
        raise ValueError("at_sentence could not produce enough segments")
    cut_positions = sentence_breaks[:max_splits]
    segments: list[str] = []
    start = 0
    for cut in cut_positions:
        segments.append(text[start:cut])
        start = cut
    segments.append(text[start:])
    if any(segment == "" for segment in segments):
        raise ValueError("at_sentence produced an empty split segment")
    return segments


__all__ = ["split_text", "SENTENCE_SPLIT_RE"]
