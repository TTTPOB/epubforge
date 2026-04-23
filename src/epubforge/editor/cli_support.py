"""Shared CLI helpers for editor tool-surface modules."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
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


class JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser variant that fails with CommandError instead of stderr text."""

    def error(self, message: str) -> None:
        raise CommandError(message, exit_code=2)


def emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")


def emit_text(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def run_cli(main_fn: Callable[[list[str] | None], int], argv: list[str] | None = None) -> int:
    try:
        return main_fn(argv)
    except CommandError as exc:
        if exc.raw_stdout is not None:
            emit_text(exc.raw_stdout)
        else:
            emit_json(exc.payload)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001
        emit_json({"error": str(exc)})
        return 1
