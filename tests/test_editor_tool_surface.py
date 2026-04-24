from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from epubforge.cli import app
from epubforge.ir.semantic import Paragraph
from epubforge.editor.log import read_current_log
from epubforge.editor.leases import LeaseState
from epubforge.editor.memory import EditMemory
from epubforge.editor.state import (
    chapter_uids,
    book_id_from_paths,
    initialize_book_state,
    resolve_editor_paths,
    write_initial_state,
)
from collections.abc import Callable

from epubforge.io import load_book
from epubforge.ir.semantic import Book, Chapter, Provenance


REPO_ROOT = Path(__file__).resolve().parents[1]

runner = CliRunner()


def _minimal_book(prov: Callable[..., Provenance]) -> Book:
    return Book(
        title="Legacy Sample",
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


def _invoke(args: list[str], input: str | None = None, env: dict[str, str] | None = None):
    """Invoke the root app with optional env overrides via monkeypatching."""
    return runner.invoke(app, args, input=input, env=env, catch_exceptions=False)


def test_init_command_creates_edit_state(prov, tmp_path: Path) -> None:
    work_dir = tmp_path / "sample-init"
    book = _minimal_book(prov)
    (work_dir / "05_semantic.json").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(book.model_dump_json(indent=2), encoding="utf-8")

    result = _invoke(["editor", "init", str(work_dir)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["book_version"] == 0

    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    assert book.op_log_version == 0
    assert book.uid_seed
    meta = json.loads(paths.meta_path.read_text(encoding="utf-8"))
    # stage3 may be None (legacy init without active manifest)
    assert meta["initialized_at"] == book.initialized_at
    assert meta["uid_seed"] == book.uid_seed
    assert all(chapter.uid for chapter in book.chapters)
    assert all(block.uid for chapter in book.chapters for block in chapter.blocks)


def test_doctor_propose_apply_queue_and_render_prompt_work_together(prov, tmp_path: Path) -> None:
    work_dir = tmp_path / "legacy-doctor"
    book = _minimal_book(prov)
    (work_dir / "05_semantic.json").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(book.model_dump_json(indent=2), encoding="utf-8")
    imported = _invoke(["editor", "init", str(work_dir)])
    assert imported.exit_code == 0, imported.output

    # Load book to get chapter/block uids
    book = load_book(resolve_editor_paths(work_dir).book_path)
    chapter_uid = book.chapters[0].uid
    block_uid = book.chapters[0].blocks[0].uid
    assert chapter_uid is not None
    assert block_uid is not None

    # Propose and apply a scanner memory patch that marks the chapter as read (read_passes=1).
    # This must happen BEFORE the first doctor call so that new_applied_op_count=0 on first
    # doctor (previous=None) and quiet_round streak accumulates correctly.
    acquired_scan = _invoke(
        ["editor", "acquire-lease", str(work_dir), "--chapter", chapter_uid, "--agent", "scanner-1", "--task", "scan chapter"],
    )
    assert acquired_scan.exit_code == 0, acquired_scan.output

    scan_envelope = [
        {
            "op_id": str(uuid4()),
            "ts": "2026-04-23T08:00:00Z",
            "agent_id": "scanner-1",
            "base_version": 0,
            "op": {"op": "noop", "purpose": "milestone"},
            "memory_patches": [
                {
                    "conventions": [],
                    "patterns": [],
                    "chapter_status": [{"chapter_uid": chapter_uid, "read_passes": 1}],
                    "open_questions": [],
                }
            ],
            "rationale": "scanner pass completed",
        }
    ]
    scan_proposed = _invoke(
        ["editor", "propose-op", str(work_dir)],
        input=json.dumps(scan_envelope, ensure_ascii=False),
    )
    assert scan_proposed.exit_code == 0, scan_proposed.output
    assert json.loads(scan_proposed.output)["accepted"] == 1

    scan_applied = _invoke(["editor", "apply-queue", str(work_dir)])
    assert scan_applied.exit_code == 0, scan_applied.output
    assert json.loads(scan_applied.output)["new_version"] == 1

    released_scan = _invoke(
        ["editor", "release-lease", str(work_dir), "--chapter", chapter_uid, "--agent", "scanner-1"],
    )
    assert released_scan.exit_code == 0, released_scan.output

    # First doctor: previous=None → new_applied_op_count=0, quiet_round=True, streak=1.
    # Chapters are scanned. Not converged yet (streak < 2).
    first_doctor = _invoke(["editor", "doctor", str(work_dir)])
    assert first_doctor.exit_code == 0, first_doctor.output
    assert json.loads(first_doctor.output)["readiness"]["converged"] is False

    # Second doctor: quiet_round=True, streak=2. Converged.
    second_doctor = _invoke(["editor", "doctor", str(work_dir)])
    assert second_doctor.exit_code == 0, second_doctor.output
    assert json.loads(second_doctor.output)["readiness"]["converged"] is True

    # Acquire lease and apply a text-fix op
    acquired = _invoke(
        ["editor", "acquire-lease", str(work_dir), "--chapter", chapter_uid, "--agent", "fixer-1", "--task", "fix text"],
    )
    assert acquired.exit_code == 0, acquired.output

    envelope = [
        {
            "op_id": str(uuid4()),
            "ts": "2026-04-23T08:00:00Z",
            "agent_id": "fixer-1",
            "base_version": 1,
            "preconditions": [{"kind": "field_equals", "block_uid": block_uid, "field": "text", "expected": "Alpha paragraph."}],
            "op": {"op": "set_text", "block_uid": block_uid, "field": "text", "value": "Alpha paragraph revised."},
            "rationale": "normalize paragraph text",
        }
    ]
    proposed = _invoke(
        ["editor", "propose-op", str(work_dir)],
        input=json.dumps(envelope, ensure_ascii=False),
    )
    assert proposed.exit_code == 0, proposed.output
    assert json.loads(proposed.output)["accepted"] == 1

    applied = _invoke(["editor", "apply-queue", str(work_dir)])
    assert applied.exit_code == 0, applied.output
    apply_payload = json.loads(applied.output)
    assert apply_payload["new_version"] == 2

    updated_book = load_book(resolve_editor_paths(work_dir).book_path)
    assert updated_book.op_log_version == 2
    updated_block = updated_book.chapters[0].blocks[0]
    assert isinstance(updated_block, Paragraph)
    assert updated_block.text == "Alpha paragraph revised."

    prompt = _invoke(
        ["editor", "render-prompt", str(work_dir), "--kind", "fixer", "--chapter", chapter_uid, "--issues", "Unknown style class"],
    )
    assert prompt.exit_code == 0, prompt.output
    assert "当前 book.op_log_version=2" in prompt.output
    assert "当前 memory 快照：" in prompt.output

    released = _invoke(
        ["editor", "release-lease", str(work_dir), "--chapter", chapter_uid, "--agent", "fixer-1"],
    )
    assert released.exit_code == 0, released.output

    paths = resolve_editor_paths(work_dir)
    paths.meta_path.unlink()
    missing_meta = _invoke(["editor", "doctor", str(work_dir)])
    assert missing_meta.exit_code != 0


def test_book_lock_run_script_snapshot_and_compact_commands(prov, tmp_path: Path) -> None:
    work_dir = tmp_path / "legacy-tools"
    book = _minimal_book(prov)
    (work_dir / "05_semantic.json").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(book.model_dump_json(indent=2), encoding="utf-8")
    imported = _invoke(["editor", "init", str(work_dir)])
    assert imported.exit_code == 0, imported.output

    locked = _invoke(["editor", "acquire-book-lock", str(work_dir), "--agent", "supervisor", "--reason", "compact"])
    assert locked.exit_code == 0, locked.output

    contended = _invoke(["editor", "acquire-book-lock", str(work_dir), "--agent", "other", "--reason", "compact"])
    assert contended.exit_code != 0
    assert contended.output.strip() == "null"

    released = _invoke(["editor", "release-book-lock", str(work_dir), "--agent", "supervisor"])
    assert released.exit_code == 0, released.output

    # run-script --write uses EPUBFORGE_EDITOR_NOW subprocess env; inject via monkeypatch in env dict
    scripted = _invoke(
        ["editor", "run-script", str(work_dir), "--write", "dash fix", "--agent", "fixer-7"],
        env={"EPUBFORGE_EDITOR_NOW": "2026-04-23T08:00:00Z"},
    )
    assert scripted.exit_code == 0, scripted.output
    script_payload = json.loads(scripted.output)
    script_path = Path(script_payload["path"])
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

    executed = _invoke(["editor", "run-script", str(work_dir), "--exec", str(script_path)])
    assert executed.exit_code == 0, executed.output
    script_result = json.loads(executed.output)
    assert script_result["work_dir"] == str(work_dir.resolve())

    snapshotted = _invoke(["editor", "snapshot", str(work_dir), "--tag", "pre-compact"])
    assert snapshotted.exit_code == 0, snapshotted.output
    snapshot_path = Path(json.loads(snapshotted.output)["snapshot"])
    assert snapshot_path.exists()
    assert (snapshot_path / "book.json").exists()

    compacted = _invoke(["editor", "compact", str(work_dir)])
    assert compacted.exit_code == 0, compacted.output
    current_log = read_current_log(resolve_editor_paths(work_dir).edit_state_dir)
    assert len(current_log) == 1
    assert current_log[0].op.op == "compact_marker"


# ---------------------------------------------------------------------------
# Helpers shared by run-script sandbox tests
# ---------------------------------------------------------------------------


def _init_work_dir(prov: Callable[..., Provenance], tmp_path: Path) -> Path:
    """Create and initialize a minimal work dir; return it."""
    work_dir = tmp_path / "work"
    book = _minimal_book(prov)
    (work_dir / "05_semantic.json").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(book.model_dump_json(indent=2), encoding="utf-8")
    result = _invoke(["editor", "init", str(work_dir)])
    assert result.exit_code == 0, result.output
    return work_dir


def _run_script_exec(work_dir: Path, exec_path: str):
    return _invoke(["editor", "run-script", str(work_dir), "--exec", exec_path])


# ---------------------------------------------------------------------------
# §1.1 run-script sandbox rejection tests
# ---------------------------------------------------------------------------


def test_run_script_rejects_absolute_outside_scratch(prov, tmp_path: Path) -> None:
    work_dir = _init_work_dir(prov, tmp_path)
    outside = tmp_path / "evil.py"
    outside.write_text("pass\n", encoding="utf-8")

    result = _run_script_exec(work_dir, str(outside))

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert "scratch_dir" in payload["error"]


def test_run_script_rejects_dotdot_escape(prov, tmp_path: Path) -> None:
    work_dir = _init_work_dir(prov, tmp_path)
    paths = resolve_editor_paths(work_dir)
    # Create a real .py file one level above scratch_dir
    escape_target = paths.scratch_dir.parent / "escape.py"
    escape_target.write_text("pass\n", encoding="utf-8")
    rel_escape = "../escape.py"

    result = _run_script_exec(work_dir, rel_escape)

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert "scratch_dir" in payload["error"]


def test_run_script_rejects_symlink_escape(prov, tmp_path: Path) -> None:
    work_dir = _init_work_dir(prov, tmp_path)
    paths = resolve_editor_paths(work_dir)
    # Create a real .py file outside scratch and a symlink inside scratch pointing to it
    real_script = tmp_path / "real_outside.py"
    real_script.write_text("pass\n", encoding="utf-8")
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    link = paths.scratch_dir / "link.py"
    link.symlink_to(real_script)

    result = _run_script_exec(work_dir, str(link))

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert "scratch_dir" in payload["error"]


def test_run_script_rejects_non_py_suffix(prov, tmp_path: Path) -> None:
    work_dir = _init_work_dir(prov, tmp_path)
    paths = resolve_editor_paths(work_dir)
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    sh_file = paths.scratch_dir / "script.sh"
    sh_file.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    result = _run_script_exec(work_dir, str(sh_file))

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert ".py" in payload["error"]


def test_run_script_accepts_relative_inside_scratch(prov, tmp_path: Path) -> None:
    work_dir = _init_work_dir(prov, tmp_path)
    paths = resolve_editor_paths(work_dir)
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    good_script = paths.scratch_dir / "good.py"
    good_script.write_text('import json; print(json.dumps({"ok": True}))\n', encoding="utf-8")

    # Pass a relative path (just the filename, resolved relative to scratch_dir by the helper)
    result = _run_script_exec(work_dir, "good.py")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# §1.7 propose-op all-or-nothing tests
# ---------------------------------------------------------------------------


def _make_valid_envelope(block_uid: str, text_value: str = "Alpha paragraph.") -> dict:
    return {
        "op_id": str(uuid4()),
        "ts": "2026-04-23T08:00:00Z",
        "agent_id": "test-agent",
        "base_version": 0,
        "op": {
            "op": "set_text",
            "block_uid": block_uid,
            "field": "text",
            "value": text_value,
        },
        "rationale": "test",
    }


def _setup_initialized_work(prov: Callable[..., Provenance], tmp_path: Path) -> tuple[Path, str]:
    """Return (work_dir, block_uid_of_first_block)."""
    work_dir = tmp_path / "work"
    book = _minimal_book(prov)
    (work_dir / "05_semantic.json").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(book.model_dump_json(indent=2), encoding="utf-8")
    imported = _invoke(["editor", "init", str(work_dir)])
    assert imported.exit_code == 0, imported.output
    book = load_book(resolve_editor_paths(work_dir).book_path)
    block_uid = book.chapters[0].blocks[0].uid
    assert block_uid
    return work_dir, block_uid


def test_propose_op_all_invalid_rejects_batch(prov, tmp_path: Path) -> None:
    work_dir, _block_uid = _setup_initialized_work(prov, tmp_path)
    bad_envelope = {"not": "valid"}

    result = _invoke(
        ["editor", "propose-op", str(work_dir)],
        input=json.dumps([bad_envelope]),
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["accepted"] == 0
    assert payload["rejected"] >= 1

    # staging.jsonl must not exist or be empty
    paths = resolve_editor_paths(work_dir)
    staging = paths.edit_state_dir / "staging.jsonl"
    assert not staging.exists() or staging.read_text(encoding="utf-8").strip() == ""


def test_propose_op_mixed_batch_rejected_entirely(prov, tmp_path: Path) -> None:
    work_dir, block_uid = _setup_initialized_work(prov, tmp_path)
    good = _make_valid_envelope(block_uid)
    bad = {"not": "valid"}

    result = _invoke(
        ["editor", "propose-op", str(work_dir)],
        input=json.dumps([good, bad]),
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    # All-or-nothing: accepted must be 0 even though one was valid
    assert payload["accepted"] == 0
    assert payload["rejected"] == 2

    paths = resolve_editor_paths(work_dir)
    staging = paths.edit_state_dir / "staging.jsonl"
    assert not staging.exists() or staging.read_text(encoding="utf-8").strip() == ""


def test_propose_op_all_valid_appended_atomically(prov, tmp_path: Path) -> None:
    work_dir, block_uid = _setup_initialized_work(prov, tmp_path)
    env1 = _make_valid_envelope(block_uid, "Alpha paragraph.")
    env2 = _make_valid_envelope(block_uid, "Beta value.")

    result = _invoke(
        ["editor", "propose-op", str(work_dir)],
        input=json.dumps([env1, env2]),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["accepted"] == 2
    assert payload["rejected"] == 0

    paths = resolve_editor_paths(work_dir)
    staging = paths.edit_state_dir / "staging.jsonl"
    lines = [ln for ln in staging.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# §1.2 write_initial_state decoupling tests
# ---------------------------------------------------------------------------


def _make_book_and_memory(prov: Callable[..., Provenance], work_dir: Path) -> tuple[Book, EditMemory]:
    """Create an initialized book and matching memory for a work_dir."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    book = initialize_book_state(_minimal_book(prov), initialized_at=now)
    paths = resolve_editor_paths(work_dir)
    memory = EditMemory.create(
        book_id=book_id_from_paths(paths),
        updated_at=now,
        updated_by="test",
        chapter_uids=chapter_uids(book),
    )
    return book, memory


def test_write_initial_state_does_not_touch_book_json(prov, tmp_path: Path) -> None:
    work_dir = tmp_path / "wis-no-book"
    work_dir.mkdir()
    paths = resolve_editor_paths(work_dir)
    book, memory = _make_book_and_memory(prov, work_dir)

    write_initial_state(paths, book=book, memory=memory, leases=LeaseState())

    # book.json must NOT be created by write_initial_state
    assert not paths.book_path.exists()
    # meta, memory, leases, log, staging must all exist
    assert paths.meta_path.exists()
    assert paths.memory_path.exists()
    assert paths.leases_path.exists()
    assert paths.current_log_path.exists()
    assert paths.staging_path.exists()


def test_run_init_persists_book(prov, tmp_path: Path) -> None:
    work_dir = tmp_path / "init-persists"
    book = _minimal_book(prov)
    (work_dir / "05_semantic.json").parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "05_semantic.json").write_text(book.model_dump_json(indent=2), encoding="utf-8")

    result = _invoke(["editor", "init", str(work_dir)])

    assert result.exit_code == 0, result.output
    paths = resolve_editor_paths(work_dir)
    # book.json must exist after run_init
    assert paths.book_path.exists()
    book = load_book(paths.book_path)
    assert book.op_log_version == 0
    assert book.uid_seed


# ---------------------------------------------------------------------------
# §1.6a memory_patches envelope-only schema tests
# ---------------------------------------------------------------------------


def test_propose_op_accepts_memory_patches_in_envelope(prov, tmp_path: Path) -> None:
    work_dir, block_uid = _setup_initialized_work(prov, tmp_path)

    envelope = _make_valid_envelope(block_uid)
    envelope["memory_patches"] = [
        {
            "conventions": [
                {
                    "canonical_key": "book:-:dash_range_style",
                    "scope": "book",
                    "topic": "dash_range_style",
                    "statement": "Use en-dash for ranges.",
                    "value": "en-dash",
                    "confidence": 0.9,
                    "evidence_uids": ["blk-1"],
                    "contributed_by": "test-agent",
                    "contributed_at": "2026-04-23T08:00:00Z",
                }
            ],
            "patterns": [],
            "chapter_status": [],
            "open_questions": [],
        }
    ]

    result = _invoke(
        ["editor", "propose-op", str(work_dir)],
        input=json.dumps([envelope]),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["accepted"] == 1
    assert payload["rejected"] == 0

    paths = resolve_editor_paths(work_dir)
    staging = paths.edit_state_dir / "staging.jsonl"
    lines = [ln for ln in staging.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1

    # Verify memory_patches round-trips through staging.jsonl
    from epubforge.editor.ops import OpEnvelope

    stored = OpEnvelope.model_validate_json(lines[0])
    assert stored.memory_patches is not None
    assert len(stored.memory_patches) == 1
    assert stored.memory_patches[0].conventions[0].topic == "dash_range_style"


# ---------------------------------------------------------------------------
# Real console-script smoke test (subprocess) — catches entry-point regressions
# ---------------------------------------------------------------------------


def test_smoke_epubforge_editor_doctor_help_via_subprocess() -> None:
    """Verify the real console-script entry-point can load and show editor help."""
    result = subprocess.run(
        ["uv", "run", "epubforge", "editor", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "editor" in result.stdout or "Editor" in result.stdout
