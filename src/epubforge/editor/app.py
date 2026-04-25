"""Typer sub-app for `epubforge editor <cmd>` commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from epubforge.editor.cli_support import CommandError

editor_app = typer.Typer(help="Editor subsystem commands", no_args_is_help=True)


def _run(fn, *args, **kwargs) -> int:
    """Call a business function, translating CommandError to a JSON stderr + exit."""
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


@editor_app.command("init")
def _init_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory (e.g. work/mybook)")],
) -> None:
    """Initialize edit_state from 05_semantic.json."""
    from epubforge.editor.tool_surface import run_init

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_init, work=work, cfg=cfg))


@editor_app.command("doctor")
def _doctor_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_json: Annotated[
        bool,
        typer.Option(
            "--json", help="Output JSON (always true; kept for compatibility)"
        ),
    ] = True,
) -> None:
    """Run doctor detectors and readiness evaluation."""
    from epubforge.editor.tool_surface import run_doctor

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_doctor, work=work, output_json=output_json, cfg=cfg))


@editor_app.command("run-script")
def _run_script_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    write: Annotated[
        Optional[str],
        typer.Option(
            "--write", help="Allocate new scratch script with this description"
        ),
    ] = None,
    exec_path: Annotated[
        Optional[str], typer.Option("--exec", help="Execute this scratch script path")
    ] = None,
    agent: Annotated[
        str, typer.Option("--agent", help="Agent ID for naming")
    ] = "agent",
) -> None:
    """Allocate or execute scratch scripts."""
    from epubforge.editor.tool_surface import run_run_script

    if write is None and exec_path is None:
        typer.echo('{"error": "either --write or --exec must be provided"}')
        raise typer.Exit(2)
    if write is not None and exec_path is not None:
        typer.echo('{"error": "--write and --exec are mutually exclusive"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_run_script,
            work=work,
            write=write,
            exec_path=exec_path,
            agent=agent,
            cfg=cfg,
        )
    )


@editor_app.command("compact")
def _compact_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
) -> None:
    """Compact accepted edit log into an archive record."""
    from epubforge.editor.tool_surface import run_compact

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_compact, work=work, cfg=cfg))


@editor_app.command("diff-books")
def _diff_books_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    proposed_file: Annotated[
        Path,
        typer.Option("--proposed-file", help="Proposed Book JSON file to compare"),
    ],
    base_file: Annotated[
        Optional[Path],
        typer.Option(
            "--base-file",
            help="Base Book JSON file (default: <work>/edit_state/book.json)",
        ),
    ] = None,
) -> None:
    """Diff two Book JSON files and print a schema-valid BookPatch JSON."""
    from epubforge.editor.tool_surface import run_diff_books

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_diff_books,
            work=work,
            proposed_file=proposed_file,
            base_file=base_file,
            cfg=cfg,
        )
    )


@editor_app.command("render-page")
def _render_page_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    page: Annotated[
        int, typer.Option("--page", help="1-based page number to render")
    ] = 0,
    dpi: Annotated[int, typer.Option("--dpi", help="Render DPI")] = 200,
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out",
            help="Output JPEG path (default: edit_state/audit/page_images/page_NNNN.jpg)",
        ),
    ] = None,
) -> None:
    """Render a single page of the source PDF to JPEG (no LLM/VLM)."""
    from epubforge.editor.tool_surface import run_render_page

    if page <= 0:
        typer.echo('{"error": "--page must be a positive integer"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(run_render_page, work=work, page=page, dpi=dpi, out=out, cfg=cfg)
    )


@editor_app.command("vlm-page")
def _vlm_page_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    page: Annotated[int, typer.Option("--page", help="1-based page number")] = 0,
    dpi: Annotated[int, typer.Option("--dpi", help="Render DPI")] = 200,
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out",
            help="Output JSON path (default: edit_state/audit/vlm_pages/page_NNNN.json)",
        ),
    ] = None,
) -> None:
    """Render a page, load evidence, call VLM, write result — never mutates book.json."""
    from epubforge.editor.tool_surface import run_vlm_page

    if page <= 0:
        typer.echo('{"error": "--page must be a positive integer"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(run_vlm_page, work=work, page=page, dpi=dpi, out=out, cfg=cfg)
    )


@editor_app.command("render-prompt")
def _render_prompt_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    kind: Annotated[
        str, typer.Option("--kind", help="Prompt kind: scanner|fixer|reviewer")
    ] = "",
    chapter: Annotated[str, typer.Option("--chapter", help="Chapter UID")] = "",
    issues: Annotated[
        Optional[list[str]], typer.Option("--issues", help="Issue strings (repeatable)")
    ] = None,
) -> None:
    """Render a subagent prompt with current memory and patch workflow instructions."""
    from epubforge.editor.tool_surface import run_render_prompt

    if kind not in ("scanner", "fixer", "reviewer"):
        typer.echo('{"error": "--kind must be one of: scanner, fixer, reviewer"}')
        raise typer.Exit(2)
    if not chapter:
        typer.echo('{"error": "--chapter is required"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_render_prompt,
            work=work,
            kind=kind,
            chapter=chapter,
            issues=issues,
            cfg=cfg,
        )
    )  # type: ignore[arg-type]


from epubforge.editor.agent_output_cli import agent_output_app  # noqa: E402
from epubforge.editor.projection_cli import projection_app  # noqa: E402

editor_app.add_typer(agent_output_app, name="agent-output")
editor_app.add_typer(projection_app, name="projection")
