"""CLI entrypoint for `python -m epubforge.editor.apply-queue`."""

from __future__ import annotations

from epubforge.editor.cli_support import run_cli
from epubforge.editor.tool_surface import run_apply_queue


def main(argv: list[str] | None = None) -> int:
    return run_cli(run_apply_queue, argv)


if __name__ == "__main__":
    raise SystemExit(main())

