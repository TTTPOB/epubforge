"""Scratch script helpers for editor tool-surface commands."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from epubforge.editor.state import SCRATCH_DIRNAME, resolve_editor_paths


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ScriptExecResult:
    returncode: int
    stdout: str
    stderr: str


def _utc_now() -> datetime:
    override = os.environ.get("EPUBFORGE_EDITOR_NOW")
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def _slug(value: str, *, fallback: str) -> str:
    normalized = _FILENAME_SAFE_RE.sub("-", value.strip()).strip("-._")
    return normalized or fallback


def allocate_script_path(work_dir: str | Path, description: str, *, agent_id: str = "agent") -> Path:
    paths = resolve_editor_paths(work_dir)
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    desc_slug = _slug(description, fallback="script")
    agent_slug = _slug(agent_id, fallback="agent")
    base = f"{stamp}_{agent_slug}_{desc_slug}"
    candidate = paths.scratch_dir / f"{base}.py"
    suffix = 2
    while candidate.exists():
        candidate = paths.scratch_dir / f"{base}_{suffix}.py"
        suffix += 1
    return candidate


def write_script_stub(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    path.write_text(
        """#!/usr/bin/env python3
\"\"\"Scratch script for epubforge editor.\"\"\"


def main() -> None:
    pass


if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )
    return path


def _resolve_within_scratch(raw: str | Path, scratch_dir: Path) -> Path:
    """Resolve *raw* to an absolute path and verify it is inside *scratch_dir*.

    Raises ValueError if the path escapes the sandbox, is not a .py file,
    or does not exist.
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = scratch_dir / p
    if p.suffix != ".py":
        raise ValueError(f"script must be a .py file, got: {p}")
    resolved = p.resolve(strict=True)
    if not resolved.is_relative_to(scratch_dir.resolve()):
        raise ValueError(f"script must reside under scratch_dir ({scratch_dir}), got: {resolved}")
    return resolved


def run_script(path: str | Path, *, work_dir: str | Path) -> ScriptExecResult:
    paths = resolve_editor_paths(work_dir)
    script_path = _resolve_within_scratch(path, paths.scratch_dir)
    env = os.environ.copy()
    pythonpath_parts = [str(PROJECT_ROOT / "src")]
    if current := env.get("PYTHONPATH"):
        pythonpath_parts.append(current)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["EPUBFORGE_PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["EPUBFORGE_WORK_DIR"] = str(paths.work_dir.resolve())
    env["EPUBFORGE_EDIT_STATE_DIR"] = str(paths.edit_state_dir.resolve())
    completed = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return ScriptExecResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


__all__ = [
    "PROJECT_ROOT",
    "SCRATCH_DIRNAME",
    "ScriptExecResult",
    "allocate_script_path",
    "run_script",
    "write_script_stub",
]
