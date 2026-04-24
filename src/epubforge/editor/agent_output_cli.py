"""Typer sub-app for `epubforge editor agent-output <cmd>` commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from epubforge.editor.cli_support import CommandError

agent_output_app = typer.Typer(help="Agent output management", no_args_is_help=True)


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


@agent_output_app.command("begin")
def _begin_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    kind: Annotated[
        str,
        typer.Option("--kind", help="Agent role: scanner|fixer|reviewer|supervisor"),
    ],
    agent: Annotated[str, typer.Option("--agent", help="Agent ID")],
    chapter: Annotated[
        Optional[str],
        typer.Option("--chapter", help="Chapter UID (required for scanner)"),
    ] = None,
) -> None:
    """Create a new AgentOutput and return its output_id."""
    from epubforge.editor.tool_surface import run_agent_output_begin

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_agent_output_begin,
            work=work,
            kind=kind,
            agent=agent,
            chapter=chapter,
            cfg=cfg,
        )
    )


@agent_output_app.command("add-note")
def _add_note_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_id: Annotated[str, typer.Argument(help="Target AgentOutput UUID")],
    text: Annotated[str, typer.Option("--text", help="Note text (non-empty)")],
) -> None:
    """Append an observation note to the specified AgentOutput."""
    from epubforge.editor.tool_surface import run_agent_output_add_note

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_agent_output_add_note,
            work=work,
            output_id=output_id,
            text=text,
            cfg=cfg,
        )
    )


@agent_output_app.command("add-question")
def _add_question_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_id: Annotated[str, typer.Argument(help="Target AgentOutput UUID")],
    question: Annotated[
        str, typer.Option("--question", help="Question text (non-empty)")
    ],
    context_uid: Annotated[
        Optional[list[str]],
        typer.Option("--context-uid", help="Related block/chapter UID (repeatable)"),
    ] = None,
    option: Annotated[
        Optional[list[str]],
        typer.Option("--option", help="Candidate answer option (repeatable)"),
    ] = None,
) -> None:
    """Append an OpenQuestion to the specified AgentOutput."""
    from epubforge.editor.tool_surface import run_agent_output_add_question

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_agent_output_add_question,
            work=work,
            output_id=output_id,
            question=question,
            context_uids=context_uid or [],
            options=option or [],
            cfg=cfg,
        )
    )


@agent_output_app.command("add-command")
def _add_command_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_id: Annotated[str, typer.Argument(help="Target AgentOutput UUID")],
    command_file: Annotated[
        Path, typer.Option("--command-file", help="Path to PatchCommand JSON file")
    ],
) -> None:
    """Append a PatchCommand from a JSON file to the specified AgentOutput."""
    from epubforge.editor.tool_surface import run_agent_output_add_command

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_agent_output_add_command,
            work=work,
            output_id=output_id,
            command_file=command_file,
            cfg=cfg,
        )
    )


@agent_output_app.command("add-patch")
def _add_patch_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_id: Annotated[str, typer.Argument(help="Target AgentOutput UUID")],
    patch_file: Annotated[
        Path, typer.Option("--patch-file", help="Path to BookPatch JSON file")
    ],
) -> None:
    """Append a BookPatch from a JSON file to the specified AgentOutput."""
    from epubforge.editor.tool_surface import run_agent_output_add_patch

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_agent_output_add_patch,
            work=work,
            output_id=output_id,
            patch_file=patch_file,
            cfg=cfg,
        )
    )


@agent_output_app.command("add-memory-patch")
def _add_memory_patch_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_id: Annotated[str, typer.Argument(help="Target AgentOutput UUID")],
    patch_file: Annotated[
        Path, typer.Option("--patch-file", help="Path to MemoryPatch JSON file")
    ],
) -> None:
    """Append a MemoryPatch from a JSON file to the specified AgentOutput."""
    from epubforge.editor.tool_surface import run_agent_output_add_memory_patch

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_agent_output_add_memory_patch,
            work=work,
            output_id=output_id,
            patch_file=patch_file,
            cfg=cfg,
        )
    )


@agent_output_app.command("validate")
def _validate_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_id: Annotated[str, typer.Argument(help="Target AgentOutput UUID")],
) -> None:
    """Validate the specified AgentOutput without modifying any state."""
    from epubforge.editor.tool_surface import run_agent_output_validate

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(run_agent_output_validate, work=work, output_id=output_id, cfg=cfg)
    )


@agent_output_app.command("submit")
def _submit_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_id: Annotated[str, typer.Argument(help="Target AgentOutput UUID")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--no-apply", help="Actually apply changes (default: dry-run)"
        ),
    ] = False,
    stage: Annotated[
        bool,
        typer.Option(
            "--stage/--no-stage", help="Validate and archive without applying changes"
        ),
    ] = False,
) -> None:
    """Validate and optionally apply an AgentOutput (dry-run by default)."""
    from epubforge.editor.tool_surface import run_agent_output_submit

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(
        _run(
            run_agent_output_submit,
            work=work,
            output_id=output_id,
            apply=apply,
            stage=stage,
            cfg=cfg,
        )
    )
