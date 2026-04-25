"""Typer sub-app for `epubforge editor projection <cmd>` commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from epubforge.editor.cli_support import CommandError

projection_app = typer.Typer(help="Read-only projection export commands", no_args_is_help=True)


def _run(fn, *args, **kwargs) -> int:
    """Call a business function, translating CommandError to JSON stdout + exit."""
    try:
        return fn(*args, **kwargs)
    except CommandError as exc:
        if exc.raw_stdout is not None:
            typer.echo(exc.raw_stdout)
        else:
            import json

            typer.echo(json.dumps(exc.payload, ensure_ascii=False))
        raise typer.Exit(exc.exit_code)
    except Exception as exc:  # noqa: BLE001
        import json

        typer.echo(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise typer.Exit(1)


@projection_app.command("export")
def _export_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    chapter: Annotated[
        Optional[str],
        typer.Option("--chapter", help="Chapter UID to export"),
    ] = None,
) -> None:
    """Export book IR to Markdown-ish read-only projection files."""
    from epubforge.editor.tool_surface import run_projection_export

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(run_projection_export, work=work, cfg=cfg, chapter_uid=chapter)
    )
