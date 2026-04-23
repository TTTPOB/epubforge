"""Shared CLI helpers for editor tool-surface modules."""

from __future__ import annotations

import json
import sys
from typing import Any


class CommandError(RuntimeError):
    """Structured command failure that should surface as JSON on stdout."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        payload: dict[str, Any] | None = None,
        raw_stdout: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.payload = payload or {"error": message}
        self.raw_stdout = raw_stdout


def emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")


def emit_text(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
