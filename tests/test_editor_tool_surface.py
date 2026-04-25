from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from epubforge.cli import app
from epubforge.editor.log import read_current_log
from epubforge.editor.memory import EditMemory
from epubforge.editor.state import (
    book_id_from_paths,
    chapter_uids,
    initialize_book_state,
    resolve_editor_paths,
    write_initial_state,
)
from epubforge.io import load_book
from epubforge.ir.semantic import Book, Chapter, Paragraph, Provenance


REPO_ROOT = Path(__file__).resolve().parents[1]

runner = CliRunner()


def _minimal_book(prov: Callable[..., Provenance]) -> Book:
    return Book(
        title="Sample",
        chapters=[
            Chapter(
                title="Chapter 1",
                blocks=[
                    Paragraph(text="Alpha paragraph.", provenance=prov(1)),
                    Paragraph(text="Beta paragraph.", provenance=prov(1)),
                ],
            )
        ],
    )


def _invoke(
    args: list[str], input: str | None = None, env: dict[str, str] | None = None
):
    return runner.invoke(app, args, input=input, env=env, catch_exceptions=False)


def _init_work_dir(
    prov: Callable[..., Provenance], tmp_path: Path, name: str = "work"
) -> Path:
    work_dir = tmp_path / name
    book = _minimal_book(prov)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(
        book.model_dump_json(indent=2), encoding="utf-8"
    )
    result = _invoke(["editor", "init", str(work_dir)])
    assert result.exit_code == 0, result.output
    return work_dir


def test_init_command_creates_current_edit_state(prov, tmp_path: Path) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "sample-init")
    paths = resolve_editor_paths(work_dir)

    book = load_book(paths.book_path)
    assert book.uid_seed
    assert paths.meta_path.exists()
    assert paths.memory_path.exists()
    assert paths.current_log_path.exists()
    assert paths.agent_outputs_dir.exists()
    assert all(chapter.uid for chapter in book.chapters)
    assert all(block.uid for chapter in book.chapters for block in chapter.blocks)


def test_agent_output_submit_updates_book_and_audit_log(prov, tmp_path: Path) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "agent-output")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    chapter_uid = book.chapters[0].uid
    block_uid = book.chapters[0].blocks[0].uid
    assert chapter_uid is not None
    assert block_uid is not None

    begun = _invoke(
        [
            "editor",
            "agent-output",
            "begin",
            str(work_dir),
            "--kind",
            "fixer",
            "--agent",
            "fixer-1",
            "--chapter",
            chapter_uid,
        ]
    )
    assert begun.exit_code == 0, begun.output
    output_id = json.loads(begun.output)["output_id"]

    patch_file = tmp_path / "patch.json"
    patch_file.write_text(
        json.dumps(
            {
                "patch_id": str(uuid4()),
                "agent_id": "fixer-1",
                "scope": {"chapter_uid": chapter_uid},
                "changes": [
                    {
                        "op": "set_field",
                        "target_uid": block_uid,
                        "field": "text",
                        "old": "Alpha paragraph.",
                        "new": "Alpha paragraph revised.",
                    }
                ],
                "rationale": "normalize paragraph text",
            }
        ),
        encoding="utf-8",
    )
    added = _invoke(
        [
            "editor",
            "agent-output",
            "add-patch",
            str(work_dir),
            output_id,
            "--patch-file",
            str(patch_file),
        ]
    )
    assert added.exit_code == 0, added.output

    submitted = _invoke(
        ["editor", "agent-output", "submit", str(work_dir), output_id, "--apply"]
    )
    assert submitted.exit_code == 0, submitted.output
    assert json.loads(submitted.output)["patches_applied"] == 1

    updated_book = load_book(paths.book_path)
    updated_block = updated_book.chapters[0].blocks[0]
    assert isinstance(updated_block, Paragraph)
    assert updated_block.text == "Alpha paragraph revised."
    log = read_current_log(paths.edit_state_dir)
    assert [entry.kind for entry in log] == ["agent_output_submitted"]


def test_doctor_render_prompt_run_script_and_compact(
    prov, tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path, "tools")
    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    chapter_uid = book.chapters[0].uid
    assert chapter_uid is not None

    doctor = _invoke(["editor", "doctor", str(work_dir)])
    assert doctor.exit_code == 0, doctor.output
    assert "readiness" in json.loads(doctor.output)

    prompt = _invoke(
        [
            "editor",
            "render-prompt",
            str(work_dir),
            "--kind",
            "fixer",
            "--chapter",
            chapter_uid,
            "--issues",
            "Unknown style class",
        ]
    )
    assert prompt.exit_code == 0, prompt.output
    assert "AgentOutput" in prompt.output
    assert "BookPatch" in prompt.output
    assert "当前 memory 快照：" in prompt.output

    scripted = _invoke(
        [
            "editor",
            "run-script",
            str(work_dir),
            "--write",
            "dash fix",
            "--agent",
            "fixer-7",
        ],
        env={"EPUBFORGE_EDITOR_NOW": "2026-04-23T08:00:00Z"},
    )
    assert scripted.exit_code == 0, scripted.output
    script_path = Path(json.loads(scripted.output)["path"])
    assert script_path.name == "20260423T080000Z_fixer-7_dash-fix.py"

    script_path.write_text(
        """import json
import os
from pathlib import Path

payload = {
    "cwd": str(Path.cwd()),
    "work_dir": os.environ["EPUBFORGE_WORK_DIR"],
    "edit_state_dir": os.environ["EPUBFORGE_EDIT_STATE_DIR"],
}
print(json.dumps(payload, ensure_ascii=False))
""",
        encoding="utf-8",
    )
    executed = _invoke(
        ["editor", "run-script", str(work_dir), "--exec", str(script_path)]
    )
    assert executed.exit_code == 0, executed.output
    assert json.loads(executed.output)["work_dir"] == str(work_dir.resolve())

    compacted = _invoke(["editor", "compact", str(work_dir)])
    assert compacted.exit_code == 0, compacted.output
    current_log = read_current_log(paths.edit_state_dir)
    assert len(current_log) == 1
    assert current_log[0].kind == "compact_marker"


def test_run_script_sandbox_rejections_and_relative_acceptance(
    prov, tmp_path: Path
) -> None:
    work_dir = _init_work_dir(prov, tmp_path)
    paths = resolve_editor_paths(work_dir)

    outside = tmp_path / "evil.py"
    outside.write_text("pass\n", encoding="utf-8")
    result = _invoke(["editor", "run-script", str(work_dir), "--exec", str(outside)])
    assert result.exit_code != 0
    assert "scratch_dir" in json.loads(result.output)["error"]

    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    sh_file = paths.scratch_dir / "script.sh"
    sh_file.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    result = _invoke(["editor", "run-script", str(work_dir), "--exec", str(sh_file)])
    assert result.exit_code != 0
    assert ".py" in json.loads(result.output)["error"]

    good_script = paths.scratch_dir / "good.py"
    good_script.write_text(
        'import json; print(json.dumps({"ok": True}))\n', encoding="utf-8"
    )
    result = _invoke(["editor", "run-script", str(work_dir), "--exec", "good.py"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True


def test_write_initial_state_does_not_touch_book_json(prov, tmp_path: Path) -> None:
    work_dir = tmp_path / "wis-no-book"
    work_dir.mkdir()
    paths = resolve_editor_paths(work_dir)
    book = initialize_book_state(
        _minimal_book(prov), initialized_at="2026-04-23T08:00:00Z"
    )
    memory = EditMemory.create(
        book_id=book_id_from_paths(paths),
        updated_at="2026-04-23T08:00:00Z",
        updated_by="test",
        chapter_uids=chapter_uids(book),
    )

    write_initial_state(paths, book=book, memory=memory)

    assert not paths.book_path.exists()
    assert paths.meta_path.exists()
    assert paths.memory_path.exists()
    assert paths.current_log_path.exists()


def test_smoke_epubforge_editor_help_via_subprocess() -> None:
    result = subprocess.run(
        ["uv", "run", "python", "-m", "epubforge", "editor", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "agent-output" in result.stdout
    assert "projection" in result.stdout


def test_editor_snapshot_command_is_removed() -> None:
    """Regression: 'editor snapshot' must fail with 'No such command'."""
    result = subprocess.run(
        ["uv", "run", "python", "-m", "epubforge", "editor", "snapshot", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, f"expected non-zero exit, got:\n{result.stdout}"
    assert "No such command" in result.stderr


def test_editor_snapshot_not_listed_in_help() -> None:
    """Regression: 'editor --help' must not list snapshot as a command."""
    result = subprocess.run(
        ["uv", "run", "python", "-m", "epubforge", "editor", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Check that "snapshot" does not appear as a command line item.
    # "snapshot" could appear in other contexts (e.g. compact description),
    # so we specifically look for the command listing pattern.
    # The Typer Commands section lists each command on its own line
    # starting with │ command-name.
    for line in result.stdout.splitlines():
        assert "snapshot" not in line, f"snapshot found in help line: {line!r}"
