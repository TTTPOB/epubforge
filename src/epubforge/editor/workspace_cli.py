"""Typer sub-app for ``epubforge editor workspace`` commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from epubforge.editor.cli_support import CommandError

workspace_app = typer.Typer(help="Git workspace management for agentic editing", no_args_is_help=True)


def _run(fn, *args, **kwargs) -> int:
    """Call a business function, translating CommandError to JSON stdout + exit."""
    try:
        return fn(*args, **kwargs)
    except CommandError as exc:
        if exc.raw_stdout is not None:
            typer.echo(exc.raw_stdout)
        else:
            typer.echo(json.dumps(exc.payload, ensure_ascii=False))
        raise typer.Exit(exc.exit_code)
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise typer.Exit(1)


@workspace_app.command("create")
def _create_cmd(
    work: Annotated[Path, typer.Argument(help="Work directory")],
    branch: Annotated[str, typer.Option("--branch", help="New branch name (agent/<kind>-<id>)")],
    base_ref: Annotated[
        str,
        typer.Option("--base-ref", help="Git ref to base the new branch on"),
    ] = "HEAD",
) -> None:
    """Create a new Git worktree for agent use."""
    from epubforge.editor.tool_surface import run_workspace_create

    raise typer.Exit(_run(run_workspace_create, work=work, branch=branch, base_ref=base_ref))


@workspace_app.command("list")
def _list_cmd(
    work: Annotated[Path, typer.Argument(help="Work directory")],
    agent_only: Annotated[
        bool,
        typer.Option("--agent-only/--no-agent-only", help="Only show agent/* worktrees"),
    ] = False,
) -> None:
    """List all Git worktrees (optionally filtered to agent/* branches)."""
    from epubforge.editor.tool_surface import run_workspace_list

    raise typer.Exit(_run(run_workspace_list, work=work, agent_only=agent_only))


@workspace_app.command("merge")
def _merge_cmd(
    work: Annotated[Path, typer.Argument(help="Work directory")],
    branch: Annotated[str, typer.Option("--branch", help="Agent branch to merge")],
    timeout: Annotated[
        int,
        typer.Option("--timeout", help="Merge timeout in seconds"),
    ] = 60,
) -> None:
    """Merge an agent branch and validate semantically."""
    from epubforge.editor.tool_surface import run_workspace_merge

    raise typer.Exit(_run(run_workspace_merge, work=work, branch=branch, timeout=timeout))


@workspace_app.command("remove")
def _remove_cmd(
    work: Annotated[Path, typer.Argument(help="Work directory")],
    branch: Annotated[str, typer.Option("--branch", help="Branch whose worktree to remove")],
    force: Annotated[
        bool,
        typer.Option("--force/--no-force", help="Force removal of unmerged worktree/branch"),
    ] = False,
) -> None:
    """Remove a Git worktree and optionally its branch."""
    from epubforge.editor.tool_surface import run_workspace_remove

    raise typer.Exit(_run(run_workspace_remove, work=work, branch=branch, force=force))


@workspace_app.command("gc")
def _gc_cmd(
    work: Annotated[Path, typer.Argument(help="Work directory")],
    max_age_days: Annotated[
        int,
        typer.Option("--max-age-days", help="Remove agent worktrees older than this many days"),
    ] = 7,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Report candidates without removing"),
    ] = False,
) -> None:
    """Garbage-collect orphaned agent worktrees older than max-age-days."""
    from epubforge.editor.tool_surface import run_workspace_gc

    raise typer.Exit(_run(run_workspace_gc, work=work, max_age_days=max_age_days, dry_run=dry_run))


__all__ = ["workspace_app"]
