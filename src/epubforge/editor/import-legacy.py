"""CLI entrypoint for `python -m epubforge.editor.import-legacy`."""

from __future__ import annotations

from epubforge.editor.cli_support import run_cli
from epubforge.editor.tool_surface import run_import_legacy


def main(argv: list[str] | None = None) -> int:
    return run_cli(run_import_legacy, argv)


if __name__ == "__main__":
    raise SystemExit(main())

