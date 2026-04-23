"""Typer sub-app for `epubforge editor <cmd>` commands."""

from __future__ import annotations

import sys
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


@editor_app.command("import-legacy")
def _import_legacy_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    source: Annotated[str, typer.Option("--from", help="Legacy artifact filename or path")] = "",
    assume_verified: Annotated[bool, typer.Option("--assume-verified", help="Mark all chapters as read_passes=1")] = False,
) -> None:
    """Initialize edit_state from a legacy artifact."""
    from epubforge.editor.tool_surface import run_import_legacy

    if not source:
        typer.echo('{"error": "--from is required"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_import_legacy, work=work, source=source, assume_verified=assume_verified, cfg=cfg))


@editor_app.command("doctor")
def _doctor_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    output_json: Annotated[bool, typer.Option("--json", help="Output JSON (always true; kept for compatibility)")] = True,
) -> None:
    """Run doctor detectors and readiness evaluation."""
    from epubforge.editor.tool_surface import run_doctor

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_doctor, work=work, output_json=output_json, cfg=cfg))


@editor_app.command("propose-op")
def _propose_op_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
) -> None:
    """Validate OpEnvelope[] from stdin and append to staging.jsonl."""
    from epubforge.editor.tool_surface import run_propose_op

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    payload_json = sys.stdin.read()
    raise typer.Exit(_run(run_propose_op, work=work, payload_json=payload_json, cfg=cfg))


@editor_app.command("apply-queue")
def _apply_queue_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
) -> None:
    """Apply staged envelopes to book.json and edit log."""
    from epubforge.editor.tool_surface import run_apply_queue

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_apply_queue, work=work, cfg=cfg))


@editor_app.command("acquire-lease")
def _acquire_lease_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    chapter: Annotated[str, typer.Option("--chapter", help="Chapter UID")] = "",
    agent: Annotated[str, typer.Option("--agent", help="Agent ID")] = "",
    task: Annotated[str, typer.Option("--task", help="Task description")] = "",
    ttl: Annotated[Optional[int], typer.Option("--ttl", help="Lease TTL in seconds (default: cfg.editor.lease_ttl_seconds)")] = None,
) -> None:
    """Acquire a chapter lease."""
    from epubforge.editor.tool_surface import run_acquire_lease

    if not chapter:
        typer.echo('{"error": "--chapter is required"}')
        raise typer.Exit(2)
    if not agent:
        typer.echo('{"error": "--agent is required"}')
        raise typer.Exit(2)
    if not task:
        typer.echo('{"error": "--task is required"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_acquire_lease, work=work, chapter=chapter, agent=agent, task=task, ttl=ttl, cfg=cfg))


@editor_app.command("release-lease")
def _release_lease_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    chapter: Annotated[str, typer.Option("--chapter", help="Chapter UID")] = "",
    agent: Annotated[str, typer.Option("--agent", help="Agent ID")] = "",
) -> None:
    """Release a chapter lease."""
    from epubforge.editor.tool_surface import run_release_lease

    if not chapter:
        typer.echo('{"error": "--chapter is required"}')
        raise typer.Exit(2)
    if not agent:
        typer.echo('{"error": "--agent is required"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_release_lease, work=work, chapter=chapter, agent=agent, cfg=cfg))


@editor_app.command("acquire-book-lock")
def _acquire_book_lock_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    agent: Annotated[str, typer.Option("--agent", help="Agent ID")] = "",
    reason: Annotated[str, typer.Option("--reason", help="Reason: topology_op|compact|init")] = "",
    ttl: Annotated[Optional[int], typer.Option("--ttl", help="Lease TTL in seconds (default: cfg.editor.book_exclusive_ttl_seconds)")] = None,
) -> None:
    """Acquire the book-wide exclusive lease."""
    from epubforge.editor.tool_surface import run_acquire_book_lock

    if not agent:
        typer.echo('{"error": "--agent is required"}')
        raise typer.Exit(2)
    if reason not in ("topology_op", "compact", "init"):
        typer.echo('{"error": "--reason must be one of: topology_op, compact, init"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_acquire_book_lock, work=work, agent=agent, reason=reason, ttl=ttl, cfg=cfg))  # type: ignore[arg-type]


@editor_app.command("release-book-lock")
def _release_book_lock_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    agent: Annotated[str, typer.Option("--agent", help="Agent ID")] = "",
) -> None:
    """Release the book-wide exclusive lease."""
    from epubforge.editor.tool_surface import run_release_book_lock

    if not agent:
        typer.echo('{"error": "--agent is required"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_release_book_lock, work=work, agent=agent, cfg=cfg))


@editor_app.command("run-script")
def _run_script_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    write: Annotated[Optional[str], typer.Option("--write", help="Allocate new scratch script with this description")] = None,
    exec_path: Annotated[Optional[str], typer.Option("--exec", help="Execute this scratch script path")] = None,
    agent: Annotated[str, typer.Option("--agent", help="Agent ID for naming")] = "agent",
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
    raise typer.Exit(_run(run_run_script, work=work, write=write, exec_path=exec_path, agent=agent, cfg=cfg))


@editor_app.command("compact")
def _compact_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
) -> None:
    """Compact the accepted edit log into an archive snapshot."""
    from epubforge.editor.tool_surface import run_compact

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_compact, work=work, cfg=cfg))


@editor_app.command("snapshot")
def _snapshot_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    tag: Annotated[Optional[str], typer.Option("--tag", help="Snapshot tag (default: current timestamp)")] = None,
) -> None:
    """Copy current edit_state into snapshots/<tag>/."""
    from epubforge.editor.tool_surface import run_snapshot

    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_snapshot, work=work, tag=tag, cfg=cfg))


@editor_app.command("render-prompt")
def _render_prompt_cmd(
    ctx: typer.Context,
    work: Annotated[Path, typer.Argument(help="Work directory")],
    kind: Annotated[str, typer.Option("--kind", help="Prompt kind: scanner|fixer|reviewer")] = "",
    chapter: Annotated[str, typer.Option("--chapter", help="Chapter UID")] = "",
    issues: Annotated[Optional[list[str]], typer.Option("--issues", help="Issue strings (repeatable)")] = None,
) -> None:
    """Render a subagent prompt with current book.op_log_version and memory snapshot."""
    from epubforge.editor.tool_surface import run_render_prompt

    if kind not in ("scanner", "fixer", "reviewer"):
        typer.echo('{"error": "--kind must be one of: scanner, fixer, reviewer"}')
        raise typer.Exit(2)
    if not chapter:
        typer.echo('{"error": "--chapter is required"}')
        raise typer.Exit(2)
    app_ctx = ctx.find_root().obj
    cfg = app_ctx.config
    raise typer.Exit(_run(run_render_prompt, work=work, kind=kind, chapter=chapter, issues=issues, cfg=cfg))  # type: ignore[arg-type]
