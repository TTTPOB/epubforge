"""Shared text helpers for CJK-aware joining across epubforge modules."""

from __future__ import annotations

from functools import reduce


def is_no_space_char(c: str) -> bool:
    """Return True if *c* belongs to a script that needs no inter-word space.

    Covered ranges:
      U+3040-U+309F  Hiragana
      U+30A0-U+30FF  Katakana
      U+3400-U+4DBF  CJK Extension A
      U+4E00-U+9FFF  CJK Unified Ideographs (BMP)
      U+AC00-U+D7AF  Hangul Syllables
      U+FF00-U+FFEF  Fullwidth / halfwidth forms
      U+20000-U+2FFFF  CJK Extensions B-F (supplementary plane)
    """
    cp = ord(c)
    return (
        0x3040 <= cp <= 0x309F  # Hiragana
        or 0x30A0 <= cp <= 0x30FF  # Katakana
        or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
        or 0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0xAC00 <= cp <= 0xD7AF  # Hangul Syllables
        or 0xFF00 <= cp <= 0xFFEF  # Fullwidth / halfwidth forms
        or 0x20000 <= cp <= 0x2FFFF  # CJK Extensions B-F (supplementary)
    )


def cjk_join_pair(prev: str, cont: str) -> str:
    """Join two text segments: no space between CJK/kana/hangul chars, one space between Latin/digit chars.

    Special cases:
    - If prev ends with '-' (ASCII hyphen) and cont starts with a Latin letter,
      drop the hyphen and join without a space (soft-hyphen line-break continuation).
    - If either boundary character belongs to a no-space script, join directly.
    - Otherwise insert a single space.
    """
    prev = prev.rstrip()
    cont = cont.lstrip()
    if not prev or not cont:
        return prev + cont
    a, b = prev[-1], cont[0]
    # Latin hyphen continuation: drop trailing hyphen when next fragment starts with a letter
    if a == "-" and b.isalpha() and b.isascii():
        return prev[:-1] + cont
    if is_no_space_char(a) or is_no_space_char(b):
        return prev + cont
    return prev + " " + cont


def cjk_join(parts: list[str]) -> str:
    """Join a list of text segments using CJK-aware joining rules."""
    return reduce(cjk_join_pair, parts, "")
