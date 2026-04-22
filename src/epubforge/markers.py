"""Shared helpers for inline footnote markers."""

from __future__ import annotations

import re

FN_MARKER_RE = re.compile(r"\x02[^\x03]*\x03")
FN_MARKER_FULL_RE = re.compile(r"\x02fn-(\d+)-([^\x03]*)\x03")


def make_fn_marker(page: int, callout: str) -> str:
    return f"\x02fn-{page}-{callout}\x03"


def strip_markers(text: str) -> str:
    return FN_MARKER_RE.sub("", text)


def has_raw_callout(text: str, callout: str) -> bool:
    return callout in strip_markers(text)


def count_raw_callout(text: str, callout: str) -> int:
    return strip_markers(text).count(callout)


def replace_first_raw(text: str, callout: str, replacement: str) -> str:
    done = [False]
    pattern = re.compile(r"\x02[^\x03]*\x03|" + re.escape(callout))

    def _sub(match: re.Match[str]) -> str:
        if done[0] or match.group() != callout:
            return match.group()
        done[0] = True
        return replacement

    return pattern.sub(_sub, text)


def replace_all_raw(text: str, callout: str, replacement: str) -> str:
    pattern = re.compile(r"\x02[^\x03]*\x03|" + re.escape(callout))

    def _sub(match: re.Match[str]) -> str:
        return replacement if match.group() == callout else match.group()

    return pattern.sub(_sub, text)


def replace_nth_raw(text: str, callout: str, replacement: str, n: int) -> str:
    counter = [-1]
    pattern = re.compile(r"\x02[^\x03]*\x03|" + re.escape(callout))

    def _sub(match: re.Match[str]) -> str:
        if match.group() != callout:
            return match.group()
        counter[0] += 1
        return replacement if counter[0] == n else match.group()

    return pattern.sub(_sub, text)
