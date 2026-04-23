from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from epubforge.ir.semantic import Figure, Footnote, Paragraph, Table
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
from epubforge.io import load_book, save_book
from epubforge.ir.semantic import Book, Chapter, Provenance


REPO_ROOT = Path(__file__).resolve().parents[1]


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _legacy_book() -> Book:
    return Book(
        title="Legacy Sample",
        chapters=[
            Chapter(
                title="Chapter 1",
                blocks=[
                    Paragraph(text="Alpha paragraph.", provenance=_prov(1)),
                    Paragraph(text="Beta paragraph.", provenance=_prov(1)),
                ],
            )
        ],
    )


def _verified_legacy_book() -> Book:
    return Book(
        title="Verified Synthetic",
        chapters=[
            Chapter(
                title="Chapter 1",
                blocks=[
                    Paragraph(
                        text="Intro \x02fn-1-①\x03 text.",
                        style_class="epigraph",
                        provenance=_prov(1),
                    ),
                    Footnote(callout="①", text="Synthetic note.", paired=True, provenance=_prov(1)),
                    Figure(caption="Synthetic figure", provenance=_prov(1)),
                    Table(
                        html="<table><tbody><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></tbody></table>",
                        table_title="Table 1",
                        caption="Synthetic table",
                        provenance=_prov(1),
                    ),
                ],
            ),
            Chapter(
                title="Chapter 2",
                blocks=[Paragraph(text="Clean follow-up paragraph.", provenance=_prov(2))],
            ),
        ],
    )


def _run_module(module: str, *args: str, input_text: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + merged_env.get("PYTHONPATH", "")
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=REPO_ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        env=merged_env,
        check=False,
    )


def _write_legacy_artifact(work_dir: Path, filename: str) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    artifact = work_dir / filename
    save_book(_legacy_book(), artifact, allow_legacy=True)
    return artifact


def _write_verified_legacy_artifact(work_dir: Path, filename: str) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    artifact = work_dir / filename
    save_book(_verified_legacy_book(), artifact, allow_legacy=True)
    return artifact


def test_init_command_creates_edit_state(tmp_path: Path) -> None:
    work_dir = tmp_path / "sample-init"
    save_book(_legacy_book(), work_dir / "05_semantic.json", allow_legacy=True)

    completed = _run_module("epubforge.editor.init", str(work_dir))

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["book_version"] == 0

    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    assert book.version == 0
    assert book.uid_seed
    meta = json.loads(paths.meta_path.read_text(encoding="utf-8"))
    assert meta == {"initialized_at": book.initialized_at, "uid_seed": book.uid_seed}
    assert all(chapter.uid for chapter in book.chapters)
    assert all(block.uid for chapter in book.chapters for block in chapter.blocks)


def test_import_legacy_writes_noop_baseline_and_assume_verified_only_changes_memory(tmp_path: Path) -> None:
    base_work = tmp_path / "legacy-a"
    _write_legacy_artifact(base_work, "07_footnote_verified.json")
    completed = _run_module(
        "epubforge.editor.import-legacy",
        str(base_work),
        "--from",
        "07_footnote_verified.json",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["book_version"] == 1

    paths = resolve_editor_paths(base_work)
    book = load_book(paths.book_path)
    assert book.version == 1
    base_meta = json.loads(paths.meta_path.read_text(encoding="utf-8"))
    assert base_meta == {"initialized_at": book.initialized_at, "uid_seed": book.uid_seed}
    memory = json.loads(paths.memory_path.read_text(encoding="utf-8"))
    assert memory["assume_verified"] is False
    assert all(status["read_passes"] == 0 for status in memory["chapter_status"].values())
    current_log = read_current_log(paths.edit_state_dir)
    assert len(current_log) == 1
    assert current_log[0].op.op == "noop"
    assert current_log[0].op.purpose == "legacy_baseline"

    verified_work = tmp_path / "legacy-b"
    _write_legacy_artifact(verified_work, "06_proofread.json")
    verified = _run_module(
        "epubforge.editor.import-legacy",
        str(verified_work),
        "--from",
        "06_proofread.json",
        "--assume-verified",
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    verified_payload = json.loads(verified.stdout)
    assert verified_payload["book_version"] == 1

    verified_paths = resolve_editor_paths(verified_work)
    verified_book = load_book(verified_paths.book_path)
    verified_meta = json.loads(verified_paths.meta_path.read_text(encoding="utf-8"))
    assert verified_meta == {"initialized_at": verified_book.initialized_at, "uid_seed": verified_book.uid_seed}
    verified_memory = json.loads(verified_paths.memory_path.read_text(encoding="utf-8"))
    assert verified_book.version == 1
    assert verified_memory["assume_verified"] is True
    assert all(status["read_passes"] == 1 for status in verified_memory["chapter_status"].values())


def test_doctor_propose_apply_queue_and_render_prompt_work_together(tmp_path: Path) -> None:
    work_dir = tmp_path / "legacy-doctor"
    _write_legacy_artifact(work_dir, "07_footnote_verified.json")
    imported = _run_module(
        "epubforge.editor.import-legacy",
        str(work_dir),
        "--from",
        "07_footnote_verified.json",
        "--assume-verified",
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr

    first_doctor = _run_module("epubforge.editor.doctor", str(work_dir), "--json")
    second_doctor = _run_module("epubforge.editor.doctor", str(work_dir), "--json")
    assert first_doctor.returncode == 0, first_doctor.stdout + first_doctor.stderr
    assert second_doctor.returncode == 0, second_doctor.stdout + second_doctor.stderr
    assert json.loads(first_doctor.stdout)["readiness"]["converged"] is False
    assert json.loads(second_doctor.stdout)["readiness"]["converged"] is True

    book = load_book(resolve_editor_paths(work_dir).book_path)
    chapter_uid = book.chapters[0].uid
    block_uid = book.chapters[0].blocks[0].uid
    assert chapter_uid is not None
    assert block_uid is not None

    acquired = _run_module(
        "epubforge.editor.acquire-lease",
        str(work_dir),
        "--chapter",
        chapter_uid,
        "--agent",
        "fixer-1",
        "--task",
        "fix text",
    )
    assert acquired.returncode == 0, acquired.stdout + acquired.stderr

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
    proposed = _run_module(
        "epubforge.editor.propose-op",
        str(work_dir),
        input_text=json.dumps(envelope, ensure_ascii=False),
    )
    assert proposed.returncode == 0, proposed.stdout + proposed.stderr
    assert json.loads(proposed.stdout)["accepted"] == 1

    applied = _run_module("epubforge.editor.apply-queue", str(work_dir))
    assert applied.returncode == 0, applied.stdout + applied.stderr
    apply_payload = json.loads(applied.stdout)
    assert apply_payload["new_version"] == 2

    updated_book = load_book(resolve_editor_paths(work_dir).book_path)
    assert updated_book.version == 2
    updated_block = updated_book.chapters[0].blocks[0]
    assert isinstance(updated_block, Paragraph)
    assert updated_block.text == "Alpha paragraph revised."

    prompt = _run_module(
        "epubforge.editor.render-prompt",
        str(work_dir),
        "--kind",
        "fixer",
        "--chapter",
        chapter_uid,
        "--issues",
        "Unknown style class",
    )
    assert prompt.returncode == 0, prompt.stdout + prompt.stderr
    assert "当前 book.version=2" in prompt.stdout
    assert "当前 memory 快照：" in prompt.stdout
    assert '"assume_verified": true' in prompt.stdout

    released = _run_module(
        "epubforge.editor.release-lease",
        str(work_dir),
        "--chapter",
        chapter_uid,
        "--agent",
        "fixer-1",
    )
    assert released.returncode == 0, released.stdout + released.stderr

    paths = resolve_editor_paths(work_dir)
    paths.meta_path.unlink()
    missing_meta = _run_module("epubforge.editor.doctor", str(work_dir), "--json")
    assert missing_meta.returncode != 0
    assert "meta.json" in missing_meta.stdout


def test_book_lock_run_script_snapshot_and_compact_commands(tmp_path: Path) -> None:
    work_dir = tmp_path / "legacy-tools"
    _write_legacy_artifact(work_dir, "07_footnote_verified.json")
    imported = _run_module(
        "epubforge.editor.import-legacy",
        str(work_dir),
        "--from",
        "07_footnote_verified.json",
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr

    locked = _run_module(
        "epubforge.editor.acquire-book-lock",
        str(work_dir),
        "--agent",
        "supervisor",
        "--reason",
        "compact",
    )
    assert locked.returncode == 0, locked.stdout + locked.stderr

    contended = _run_module(
        "epubforge.editor.acquire-book-lock",
        str(work_dir),
        "--agent",
        "other",
        "--reason",
        "compact",
    )
    assert contended.returncode != 0
    assert contended.stdout.strip() == "null"

    released = _run_module(
        "epubforge.editor.release-book-lock",
        str(work_dir),
        "--agent",
        "supervisor",
    )
    assert released.returncode == 0, released.stdout + released.stderr

    scripted = _run_module(
        "epubforge.editor.run-script",
        str(work_dir),
        "--write",
        "dash fix",
        "--agent",
        "fixer-7",
        env={"EPUBFORGE_EDITOR_NOW": "2026-04-23T08:00:00Z"},
    )
    assert scripted.returncode == 0, scripted.stdout + scripted.stderr
    script_payload = json.loads(scripted.stdout)
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

    executed = _run_module("epubforge.editor.run-script", str(work_dir), "--exec", str(script_path))
    assert executed.returncode == 0, executed.stdout + executed.stderr
    script_result = json.loads(executed.stdout)
    assert script_result["cwd"] == str(REPO_ROOT)
    assert script_result["work_dir"] == str(work_dir.resolve())

    snapshotted = _run_module("epubforge.editor.snapshot", str(work_dir), "--tag", "pre-compact")
    assert snapshotted.returncode == 0, snapshotted.stdout + snapshotted.stderr
    snapshot_path = Path(json.loads(snapshotted.stdout)["snapshot"])
    assert snapshot_path.exists()
    assert (snapshot_path / "book.json").exists()

    compacted = _run_module("epubforge.editor.compact", str(work_dir))
    assert compacted.returncode == 0, compacted.stdout + compacted.stderr
    current_log = read_current_log(resolve_editor_paths(work_dir).edit_state_dir)
    assert len(current_log) == 1
    assert current_log[0].op.op == "compact_marker"


def test_import_legacy_assume_verified_synthetic_regression_converges_after_two_doctor_rounds(tmp_path: Path) -> None:
    work_dir = tmp_path / "verified-import"
    _write_verified_legacy_artifact(work_dir, "07_footnote_verified.json")

    imported = _run_module(
        "epubforge.editor.import-legacy",
        str(work_dir),
        "--from",
        "07_footnote_verified.json",
        "--assume-verified",
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr
    payload = json.loads(imported.stdout)
    assert payload["book_version"] == 1
    assert payload["assume_verified"] is True

    paths = resolve_editor_paths(work_dir)
    book = load_book(paths.book_path)
    memory = json.loads(paths.memory_path.read_text(encoding="utf-8"))
    assert book.version == 1
    assert all(chapter.uid for chapter in book.chapters)
    assert all(block.uid for chapter in book.chapters for block in chapter.blocks)
    assert memory["assume_verified"] is True
    assert memory["imported"] is True
    assert memory["imported_from"] == "07_footnote_verified.json"
    assert all(status["read_passes"] == 1 for status in memory["chapter_status"].values())

    first_doctor = _run_module("epubforge.editor.doctor", str(work_dir), "--json")
    second_doctor = _run_module("epubforge.editor.doctor", str(work_dir), "--json")
    assert first_doctor.returncode == 0, first_doctor.stdout + first_doctor.stderr
    assert second_doctor.returncode == 0, second_doctor.stdout + second_doctor.stderr

    first_payload = json.loads(first_doctor.stdout)
    second_payload = json.loads(second_doctor.stdout)
    assert first_payload["readiness"]["converged"] is False
    assert second_payload["readiness"]["converged"] is True
    assert second_payload["readiness"]["chapters_unscanned"] == []


# ---------------------------------------------------------------------------
# Helpers shared by run-script sandbox tests
# ---------------------------------------------------------------------------


def _init_work_dir(tmp_path: Path) -> Path:
    """Create and initialize a minimal work dir; return it."""
    work_dir = tmp_path / "work"
    save_book(_legacy_book(), work_dir / "05_semantic.json", allow_legacy=True)
    completed = _run_module("epubforge.editor.init", str(work_dir))
    assert completed.returncode == 0, completed.stderr
    return work_dir


def _run_script_exec(work_dir: Path, exec_path: str) -> subprocess.CompletedProcess[str]:
    return _run_module("epubforge.editor.run-script", str(work_dir), "--exec", exec_path)


# ---------------------------------------------------------------------------
# §1.1 run-script sandbox rejection tests
# ---------------------------------------------------------------------------


def test_run_script_rejects_absolute_outside_scratch(tmp_path: Path) -> None:
    work_dir = _init_work_dir(tmp_path)
    outside = tmp_path / "evil.py"
    outside.write_text("pass\n", encoding="utf-8")

    result = _run_script_exec(work_dir, str(outside))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert "scratch_dir" in payload["error"]


def test_run_script_rejects_dotdot_escape(tmp_path: Path) -> None:
    work_dir = _init_work_dir(tmp_path)
    paths = resolve_editor_paths(work_dir)
    # Create a real .py file one level above scratch_dir
    escape_target = paths.scratch_dir.parent / "escape.py"
    escape_target.write_text("pass\n", encoding="utf-8")
    rel_escape = "../escape.py"

    result = _run_script_exec(work_dir, rel_escape)

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert "scratch_dir" in payload["error"]


def test_run_script_rejects_symlink_escape(tmp_path: Path) -> None:
    work_dir = _init_work_dir(tmp_path)
    paths = resolve_editor_paths(work_dir)
    # Create a real .py file outside scratch and a symlink inside scratch pointing to it
    real_script = tmp_path / "real_outside.py"
    real_script.write_text("pass\n", encoding="utf-8")
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    link = paths.scratch_dir / "link.py"
    link.symlink_to(real_script)

    result = _run_script_exec(work_dir, str(link))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert "scratch_dir" in payload["error"]


def test_run_script_rejects_non_py_suffix(tmp_path: Path) -> None:
    work_dir = _init_work_dir(tmp_path)
    paths = resolve_editor_paths(work_dir)
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    sh_file = paths.scratch_dir / "script.sh"
    sh_file.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    result = _run_script_exec(work_dir, str(sh_file))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert ".py" in payload["error"]


def test_run_script_accepts_relative_inside_scratch(tmp_path: Path) -> None:
    work_dir = _init_work_dir(tmp_path)
    paths = resolve_editor_paths(work_dir)
    paths.scratch_dir.mkdir(parents=True, exist_ok=True)
    good_script = paths.scratch_dir / "good.py"
    good_script.write_text('import json; print(json.dumps({"ok": True}))\n', encoding="utf-8")

    # Pass a relative path (just the filename, resolved relative to scratch_dir by the helper)
    result = _run_script_exec(work_dir, "good.py")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# §1.7 propose-op all-or-nothing tests
# ---------------------------------------------------------------------------


def _make_valid_envelope(block_uid: str, text_value: str = "Alpha paragraph.") -> dict:
    return {
        "op_id": str(uuid4()),
        "ts": "2026-04-23T08:00:00Z",
        "agent_id": "test-agent",
        "base_version": 1,
        "op": {
            "op": "set_text",
            "block_uid": block_uid,
            "field": "text",
            "value": text_value,
        },
        "rationale": "test",
    }


def _setup_initialized_work(tmp_path: Path) -> tuple[Path, str]:
    """Return (work_dir, block_uid_of_first_block)."""
    work_dir = tmp_path / "work"
    _write_legacy_artifact(work_dir, "07_footnote_verified.json")
    imported = _run_module(
        "epubforge.editor.import-legacy",
        str(work_dir),
        "--from",
        "07_footnote_verified.json",
    )
    assert imported.returncode == 0, imported.stderr
    book = load_book(resolve_editor_paths(work_dir).book_path)
    block_uid = book.chapters[0].blocks[0].uid
    assert block_uid
    return work_dir, block_uid


def test_propose_op_all_invalid_rejects_batch(tmp_path: Path) -> None:
    work_dir, _block_uid = _setup_initialized_work(tmp_path)
    bad_envelope = {"not": "valid"}

    result = _run_module(
        "epubforge.editor.propose-op",
        str(work_dir),
        input_text=json.dumps([bad_envelope]),
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["accepted"] == 0
    assert payload["rejected"] >= 1

    # staging.jsonl must not exist or be empty
    paths = resolve_editor_paths(work_dir)
    staging = paths.edit_state_dir / "staging.jsonl"
    assert not staging.exists() or staging.read_text(encoding="utf-8").strip() == ""


def test_propose_op_mixed_batch_rejected_entirely(tmp_path: Path) -> None:
    work_dir, block_uid = _setup_initialized_work(tmp_path)
    good = _make_valid_envelope(block_uid)
    bad = {"not": "valid"}

    result = _run_module(
        "epubforge.editor.propose-op",
        str(work_dir),
        input_text=json.dumps([good, bad]),
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    # All-or-nothing: accepted must be 0 even though one was valid
    assert payload["accepted"] == 0
    assert payload["rejected"] == 2

    paths = resolve_editor_paths(work_dir)
    staging = paths.edit_state_dir / "staging.jsonl"
    assert not staging.exists() or staging.read_text(encoding="utf-8").strip() == ""


def test_propose_op_all_valid_appended_atomically(tmp_path: Path) -> None:
    work_dir, block_uid = _setup_initialized_work(tmp_path)
    env1 = _make_valid_envelope(block_uid, "Alpha paragraph.")
    env2 = _make_valid_envelope(block_uid, "Beta value.")

    result = _run_module(
        "epubforge.editor.propose-op",
        str(work_dir),
        input_text=json.dumps([env1, env2]),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["accepted"] == 2
    assert payload["rejected"] == 0

    paths = resolve_editor_paths(work_dir)
    staging = paths.edit_state_dir / "staging.jsonl"
    lines = [ln for ln in staging.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# §1.2 write_initial_state decoupling tests
# ---------------------------------------------------------------------------


def _make_book_and_memory(work_dir: Path) -> tuple[Book, EditMemory]:
    """Create an initialized book and matching memory for a work_dir."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    book = initialize_book_state(_legacy_book(), initialized_at=now)
    paths = resolve_editor_paths(work_dir)
    memory = EditMemory.create(
        book_id=book_id_from_paths(paths),
        updated_at=now,
        updated_by="test",
        chapter_uids=chapter_uids(book),
    )
    return book, memory


def test_write_initial_state_does_not_touch_book_json(tmp_path: Path) -> None:
    work_dir = tmp_path / "wis-no-book"
    work_dir.mkdir()
    paths = resolve_editor_paths(work_dir)
    book, memory = _make_book_and_memory(work_dir)

    write_initial_state(paths, book=book, memory=memory, leases=LeaseState())

    # book.json must NOT be created by write_initial_state
    assert not paths.book_path.exists()
    # meta, memory, leases, log, staging must all exist
    assert paths.meta_path.exists()
    assert paths.memory_path.exists()
    assert paths.leases_path.exists()
    assert paths.current_log_path.exists()
    assert paths.staging_path.exists()


def test_run_init_persists_book(tmp_path: Path) -> None:
    work_dir = tmp_path / "init-persists"
    save_book(_legacy_book(), work_dir / "05_semantic.json", allow_legacy=True)

    completed = _run_module("epubforge.editor.init", str(work_dir))

    assert completed.returncode == 0, completed.stdout + completed.stderr
    paths = resolve_editor_paths(work_dir)
    # book.json must exist after run_init
    assert paths.book_path.exists()
    book = load_book(paths.book_path)
    assert book.version == 0
    assert book.uid_seed


def test_run_import_legacy_persists_book_and_log(tmp_path: Path) -> None:
    work_dir = tmp_path / "import-persists"
    _write_legacy_artifact(work_dir, "07_footnote_verified.json")

    completed = _run_module(
        "epubforge.editor.import-legacy",
        str(work_dir),
        "--from",
        "07_footnote_verified.json",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    paths = resolve_editor_paths(work_dir)
    # book.json must exist and be at version 1 (noop applied)
    assert paths.book_path.exists()
    book = load_book(paths.book_path)
    assert book.version == 1
    # edit log must exist via paths.current_log_path (no hardcoded filename)
    assert paths.current_log_path.exists()
    current_log = read_current_log(paths.edit_state_dir)
    assert len(current_log) == 1
    assert current_log[0].op.op == "noop"
