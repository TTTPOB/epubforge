"""Tests for Phase 2 AgentOutput model + CLI tool-surface functions.

All tests call run_* functions from tool_surface.py directly (bypassing Typer/CLI),
matching the patterns in test_editor_tool_surface.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from epubforge.config import Config
from epubforge.editor.agent_output import (
    AgentOutput,
    SubmitResult,
    load_agent_output,
    save_agent_output,
    validate_agent_output,
    submit_agent_output,
)
from epubforge.editor.cli_support import CommandError
from epubforge.editor.memory import (
    ChapterStatus,
    EditMemory,
    MemoryPatch,
    OpenQuestion,
)
from epubforge.editor.patch_commands import PatchCommand
from epubforge.editor.patches import BookPatch, PatchScope, SetFieldChange
from epubforge.editor.state import load_editor_memory, resolve_editor_paths
from epubforge.editor.tool_surface import (
    run_agent_output_add_command,
    run_agent_output_add_memory_patch,
    run_agent_output_add_note,
    run_agent_output_add_patch,
    run_agent_output_add_question,
    run_agent_output_begin,
    run_agent_output_submit,
    run_agent_output_validate,
    run_init,
)
from epubforge.io import load_book
from epubforge.ir.semantic import Book, Chapter, Footnote, Heading, Paragraph, Provenance, Table


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, bbox=None, source="passthrough")


def _cfg() -> Config:
    return Config()


def _now_ts() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_agent_output(
    kind: str = "fixer",
    agent_id: str = "fixer-1",
    chapter_uid: str | None = None,
    **kwargs,
) -> AgentOutput:
    now = _now_ts()
    return AgentOutput(
        output_id=str(uuid4()),
        kind=kind,  # type: ignore[arg-type]
        agent_id=agent_id,
        chapter_uid=chapter_uid,
        created_at=now,
        updated_at=now,
        **kwargs,
    )


def _make_book() -> Book:
    return Book(
        title="Test Book",
        chapters=[
            Chapter(
                title="Chapter 1",
                blocks=[
                    Paragraph(text="Hello world", role="body", provenance=_prov()),
                    Heading(text="Section A", level=1, provenance=_prov()),
                    Footnote(
                        callout="1",
                        text="A footnote",
                        paired=False,
                        orphan=False,
                        provenance=_prov(),
                    ),
                ],
            ),
            Chapter(
                title="Chapter 2",
                blocks=[
                    Paragraph(text="Second chapter text", role="body", provenance=_prov()),
                    Table(
                        html="<table><tr><td>data</td></tr></table>",
                        provenance=_prov(),
                    ),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Fixture: initialized work directory
# ---------------------------------------------------------------------------


@pytest.fixture
def initialized_work(tmp_path: Path):
    """Return (work_dir, book) with initialized editor state."""
    work = tmp_path / "testbook"
    book = _make_book()
    work.mkdir(parents=True, exist_ok=True)
    (work / "05_semantic.json").write_text(book.model_dump_json(indent=2), encoding="utf-8")
    run_init(work=work, cfg=_cfg())
    paths = resolve_editor_paths(work)
    actual_book = load_book(paths.book_path)
    return work, actual_book


# ===========================================================================
# §8.1 AgentOutput model unit tests
# ===========================================================================


class TestAgentOutputModel:
    def test_agent_output_valid_scanner(self):
        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="scanner",
            agent_id="scanner-1",
            chapter_uid="ch-001",
            created_at=now,
            updated_at=now,
        )
        assert output.kind == "scanner"
        assert output.agent_id == "scanner-1"
        assert output.chapter_uid == "ch-001"
        assert output.patches == []
        assert output.notes == []

    def test_agent_output_valid_supervisor_no_chapter(self):
        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="supervisor",
            agent_id="supervisor-main",
            chapter_uid=None,
            created_at=now,
            updated_at=now,
        )
        assert output.chapter_uid is None
        assert output.kind == "supervisor"

    def test_agent_output_invalid_kind(self):
        now = _now_ts()
        with pytest.raises(ValidationError):
            AgentOutput(
                output_id=str(uuid4()),
                kind="unknown_kind",  # type: ignore[arg-type]
                agent_id="agent-1",
                created_at=now,
                updated_at=now,
            )

    def test_agent_output_empty_agent_id(self):
        now = _now_ts()
        with pytest.raises(ValidationError):
            AgentOutput(
                output_id=str(uuid4()),
                kind="fixer",
                agent_id="",  # empty
                created_at=now,
                updated_at=now,
            )

    def test_agent_output_invalid_timestamps(self):
        """updated_at < created_at should raise ValidationError."""
        with pytest.raises(ValidationError):
            AgentOutput(
                output_id=str(uuid4()),
                kind="fixer",
                agent_id="fixer-1",
                created_at="2026-04-24T10:05:00Z",
                updated_at="2026-04-24T10:00:00Z",  # earlier
            )

    def test_agent_output_extra_fields_forbidden(self):
        """StrictModel rejects extra fields."""
        now = _now_ts()
        with pytest.raises(ValidationError):
            AgentOutput(
                output_id=str(uuid4()),
                kind="fixer",
                agent_id="fixer-1",
                created_at=now,
                updated_at=now,
                unknown_field="oops",  # type: ignore[unexpected-keyword]
            )

    def test_patch_command_valid(self):
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_block",
            agent_id="fixer-1",
            rationale="Split this block in two",
        )
        assert cmd.op == "split_block"
        assert cmd.params == {}

    def test_patch_command_empty_op(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=str(uuid4()),
                op="",  # empty
                agent_id="fixer-1",
                rationale="some rationale",
            )


# ===========================================================================
# §8.2 begin command tests
# ===========================================================================


class TestBeginCommand:
    def test_begin_creates_file(self, initialized_work, capsys):
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        run_agent_output_begin(
            work=work,
            kind="scanner",
            agent="scanner-1",
            chapter=chapter_uid,
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)
        output_id = out["output_id"]
        paths = resolve_editor_paths(work)
        assert (paths.agent_outputs_dir / f"{output_id}.json").exists()

    def test_begin_returns_output_id_and_path(self, initialized_work, capsys):
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        run_agent_output_begin(
            work=work,
            kind="fixer",
            agent="fixer-1",
            chapter=chapter_uid,
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)
        assert "output_id" in out
        assert "path" in out
        assert "base_version" not in out  # D6: no base_version

    def test_begin_invalid_kind(self, initialized_work):
        work, book = initialized_work
        with pytest.raises(CommandError) as exc_info:
            run_agent_output_begin(
                work=work,
                kind="bad_kind",
                agent="fixer-1",
                chapter=None,
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 2

    def test_begin_missing_agent(self, initialized_work):
        work, book = initialized_work
        with pytest.raises(CommandError) as exc_info:
            run_agent_output_begin(
                work=work,
                kind="fixer",
                agent="",  # empty
                chapter=None,
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 2

    def test_begin_nonexistent_chapter(self, initialized_work):
        work, book = initialized_work
        with pytest.raises(CommandError) as exc_info:
            run_agent_output_begin(
                work=work,
                kind="fixer",
                agent="fixer-1",
                chapter="nonexistent-uid-xxxx",
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 1

    def test_begin_not_initialized(self, tmp_path):
        work = tmp_path / "notinit"
        work.mkdir()
        # ensure_initialized raises FileNotFoundError (converted to exit 1 by CLI layer)
        with pytest.raises((CommandError, FileNotFoundError)):
            run_agent_output_begin(
                work=work,
                kind="fixer",
                agent="fixer-1",
                chapter=None,
                cfg=_cfg(),
            )

    def test_begin_scanner_requires_chapter(self, initialized_work):
        work, book = initialized_work
        with pytest.raises(CommandError) as exc_info:
            run_agent_output_begin(
                work=work,
                kind="scanner",
                agent="scanner-1",
                chapter=None,  # missing chapter
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 2


# ===========================================================================
# §8.3 add-note tests
# ===========================================================================


class TestAddNote:
    def _begin(self, work, book, capsys, kind="fixer", chapter=None):
        """Helper: begin an output, return output_id."""
        if chapter is None and kind in ("scanner",):
            chapter = book.chapters[0].uid
        run_agent_output_begin(
            work=work, kind=kind, agent="fixer-1", chapter=chapter, cfg=_cfg()
        )
        return json.loads(capsys.readouterr().out)["output_id"]

    def test_add_note_appends(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        run_agent_output_add_note(work=work, output_id=oid, text="Note 1", cfg=_cfg())
        capsys.readouterr()
        run_agent_output_add_note(work=work, output_id=oid, text="Note 2", cfg=_cfg())
        out = json.loads(capsys.readouterr().out)

        assert out["notes_count"] == 2

    def test_add_note_trims_whitespace(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        run_agent_output_add_note(work=work, output_id=oid, text="  trimmed  ", cfg=_cfg())
        capsys.readouterr()

        paths = resolve_editor_paths(work)
        saved = load_agent_output(paths, oid)
        assert saved.notes[0] == "trimmed"

    def test_add_note_empty_text(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_note(work=work, output_id=oid, text="  ", cfg=_cfg())
        assert exc_info.value.exit_code == 2

    def test_add_note_updates_updated_at(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        paths = resolve_editor_paths(work)
        before = load_agent_output(paths, oid).updated_at

        # Small sleep to ensure timestamp differs
        time.sleep(1)
        run_agent_output_add_note(work=work, output_id=oid, text="Some note", cfg=_cfg())
        capsys.readouterr()

        after = load_agent_output(paths, oid).updated_at
        assert after >= before

    def test_add_note_nonexistent_output(self, initialized_work):
        work, book = initialized_work
        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_note(
                work=work,
                output_id=str(uuid4()),
                text="Note",
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 1


# ===========================================================================
# §8.4 add-question tests
# ===========================================================================


class TestAddQuestion:
    def _begin(self, work, book, capsys, kind="fixer", chapter=None):
        run_agent_output_begin(
            work=work, kind=kind, agent="test-agent", chapter=chapter, cfg=_cfg()
        )
        return json.loads(capsys.readouterr().out)["output_id"]

    def test_add_question_basic(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        run_agent_output_add_question(
            work=work,
            output_id=oid,
            question="What is this?",
            context_uids=[],
            options=[],
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)
        assert "q_id" in out
        assert out["questions_count"] == 1

    def test_add_question_with_context_uids(self, initialized_work, capsys):
        work, book = initialized_work
        block_uid = book.chapters[0].blocks[0].uid
        oid = self._begin(work, book, capsys)

        run_agent_output_add_question(
            work=work,
            output_id=oid,
            question="Is this block correct?",
            context_uids=[block_uid],
            options=[],
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)
        assert out["questions_count"] == 1

    def test_add_question_invalid_context_uid(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_question(
                work=work,
                output_id=oid,
                question="Does uid exist?",
                context_uids=["nonexistent-uid-xxxx"],
                options=[],
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 1

    def test_add_question_with_options(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        run_agent_output_add_question(
            work=work,
            output_id=oid,
            question="Which option?",
            context_uids=[],
            options=["Option A", "Option B"],
            cfg=_cfg(),
        )
        capsys.readouterr()

        paths = resolve_editor_paths(work)
        saved = load_agent_output(paths, oid)
        assert len(saved.open_questions) == 1
        assert "Option A" in saved.open_questions[0].options

    def test_add_question_empty_question(self, initialized_work, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_question(
                work=work,
                output_id=oid,
                question="   ",  # empty/whitespace
                context_uids=[],
                options=[],
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 2

    def test_add_question_asked_by_is_agent_id(self, initialized_work, capsys):
        """OpenQuestion.asked_by must be forced to output.agent_id."""
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        run_agent_output_add_question(
            work=work,
            output_id=oid,
            question="Who asked this?",
            context_uids=[],
            options=[],
            cfg=_cfg(),
        )
        capsys.readouterr()

        paths = resolve_editor_paths(work)
        saved = load_agent_output(paths, oid)
        assert saved.open_questions[0].asked_by == "test-agent"


# ===========================================================================
# §8.5 add-command tests
# ===========================================================================


class TestAddCommand:
    def _begin(self, work, book, capsys):
        run_agent_output_begin(work=work, kind="fixer", agent="fixer-1", chapter=None, cfg=_cfg())
        return json.loads(capsys.readouterr().out)["output_id"]

    def _write_command_file(self, tmp_path: Path, data: dict) -> Path:
        p = tmp_path / "command.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_add_command_valid_file(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        cmd_data = {
            "command_id": str(uuid4()),
            "op": "split_block",
            "agent_id": "fixer-1",
            "rationale": "Split this block",
        }
        cmd_file = self._write_command_file(tmp_path, cmd_data)

        run_agent_output_add_command(
            work=work, output_id=oid, command_file=cmd_file, cfg=_cfg()
        )
        out = json.loads(capsys.readouterr().out)
        assert "command_id" in out
        assert out["commands_count"] == 1

    def test_add_command_missing_file(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        nonexistent = tmp_path / "does_not_exist.json"
        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_command(
                work=work, output_id=oid, command_file=nonexistent, cfg=_cfg()
            )
        assert exc_info.value.exit_code == 2

    def test_add_command_invalid_json(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not json!!!", encoding="utf-8")

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_command(
                work=work, output_id=oid, command_file=bad_file, cfg=_cfg()
            )
        assert exc_info.value.exit_code == 1

    def test_add_command_schema_violation(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        # Missing required fields
        bad_data = {"op": "something"}
        bad_file = self._write_command_file(tmp_path, bad_data)

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_command(
                work=work, output_id=oid, command_file=bad_file, cfg=_cfg()
            )
        assert exc_info.value.exit_code == 1


# ===========================================================================
# §8.6 add-patch tests
# ===========================================================================


class TestAddPatch:
    def _begin(self, work, book, capsys, chapter=None):
        run_agent_output_begin(
            work=work, kind="fixer", agent="fixer-1", chapter=chapter, cfg=_cfg()
        )
        return json.loads(capsys.readouterr().out)["output_id"]

    def _make_patch(self, block_uid: str, chapter_uid: str | None = None) -> dict:
        return {
            "patch_id": str(uuid4()),
            "agent_id": "fixer-1",
            "scope": {"chapter_uid": chapter_uid},
            "changes": [
                {
                    "op": "set_field",
                    "target_uid": block_uid,
                    "field": "text",
                    "old": "Hello world",
                    "new": "Hello world updated",
                }
            ],
            "rationale": "Fix text",
        }

    def test_add_patch_valid(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        block_uid = book.chapters[0].blocks[0].uid
        oid = self._begin(work, book, capsys, chapter=chapter_uid)

        patch_data = self._make_patch(block_uid, chapter_uid)
        patch_file = tmp_path / "patch.json"
        patch_file.write_text(json.dumps(patch_data), encoding="utf-8")

        run_agent_output_add_patch(
            work=work, output_id=oid, patch_file=patch_file, cfg=_cfg()
        )
        out = json.loads(capsys.readouterr().out)
        assert "patch_id" in out
        assert out["patches_count"] == 1

    def test_add_patch_missing_file(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_patch(
                work=work,
                output_id=oid,
                patch_file=tmp_path / "missing.json",
                cfg=_cfg(),
            )
        assert exc_info.value.exit_code == 2

    def test_add_patch_invalid_json(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{invalid json", encoding="utf-8")

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_patch(
                work=work, output_id=oid, patch_file=bad_file, cfg=_cfg()
            )
        assert exc_info.value.exit_code == 1

    def test_add_patch_schema_violation(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        bad_data = {"patch_id": "not-a-uuid", "scope": {}}
        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps(bad_data), encoding="utf-8")

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_patch(
                work=work, output_id=oid, patch_file=bad_file, cfg=_cfg()
            )
        assert exc_info.value.exit_code == 1

    def test_add_patch_scope_mismatch(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        block_uid = book.chapters[0].blocks[0].uid
        oid = self._begin(work, book, capsys, chapter=chapter_uid)

        # Patch scope points to different chapter
        patch_data = self._make_patch(block_uid, "different-chapter-uid")
        patch_file = tmp_path / "patch.json"
        patch_file.write_text(json.dumps(patch_data), encoding="utf-8")

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_patch(
                work=work, output_id=oid, patch_file=patch_file, cfg=_cfg()
            )
        assert exc_info.value.exit_code == 1


# ===========================================================================
# §8.7 add-memory-patch tests
# ===========================================================================


class TestAddMemoryPatch:
    def _begin(self, work, book, capsys):
        run_agent_output_begin(
            work=work, kind="fixer", agent="fixer-1", chapter=None, cfg=_cfg()
        )
        return json.loads(capsys.readouterr().out)["output_id"]

    def test_add_memory_patch_valid(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        oid = self._begin(work, book, capsys)

        mp_data = {
            "conventions": [],
            "patterns": [],
            "chapter_status": [
                {"chapter_uid": chapter_uid, "read_passes": 1}
            ],
            "open_questions": [],
        }
        mp_file = tmp_path / "memory_patch.json"
        mp_file.write_text(json.dumps(mp_data), encoding="utf-8")

        run_agent_output_add_memory_patch(
            work=work, output_id=oid, patch_file=mp_file, cfg=_cfg()
        )
        out = json.loads(capsys.readouterr().out)
        assert out["memory_patches_count"] == 1

    def test_add_memory_patch_schema_violation(self, initialized_work, tmp_path, capsys):
        work, book = initialized_work
        oid = self._begin(work, book, capsys)

        # Invalid data: chapter_status items missing required chapter_uid
        bad_data = {
            "conventions": "not-a-list",  # should be list
        }
        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps(bad_data), encoding="utf-8")

        with pytest.raises(CommandError) as exc_info:
            run_agent_output_add_memory_patch(
                work=work, output_id=oid, patch_file=bad_file, cfg=_cfg()
            )
        assert exc_info.value.exit_code == 1


# ===========================================================================
# §8.8 validate tests
# ===========================================================================


class TestValidate:
    def _begin_scanner(self, work, book, capsys):
        chapter_uid = book.chapters[0].uid
        run_agent_output_begin(
            work=work,
            kind="scanner",
            agent="scanner-1",
            chapter=chapter_uid,
            cfg=_cfg(),
        )
        return json.loads(capsys.readouterr().out)["output_id"], chapter_uid

    def _add_read_pass(self, work, oid, chapter_uid, tmp_path):
        mp_data = {
            "conventions": [],
            "patterns": [],
            "chapter_status": [{"chapter_uid": chapter_uid, "read_passes": 1}],
            "open_questions": [],
        }
        mp_file = tmp_path / f"mp_{oid[:8]}.json"
        mp_file.write_text(json.dumps(mp_data), encoding="utf-8")
        # We need a capsys context, so use run function directly via patching
        import sys
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            run_agent_output_add_memory_patch(
                work=work, output_id=oid, patch_file=mp_file, cfg=_cfg()
            )
        finally:
            sys.stdout = old_stdout

    def test_validate_clean_output(self, initialized_work, capsys):
        work, book = initialized_work
        # Use supervisor (no chapter required, no read_passes check)
        run_agent_output_begin(
            work=work, kind="supervisor", agent="supervisor-1", chapter=None, cfg=_cfg()
        )
        oid = json.loads(capsys.readouterr().out)["output_id"]

        run_agent_output_validate(work=work, output_id=oid, cfg=_cfg())
        out = json.loads(capsys.readouterr().out)

        assert out["valid"] is True
        assert out["errors"] == []

    def test_validate_rejects_uncompiled_commands(self, initialized_work, capsys):
        work, book = initialized_work
        paths = resolve_editor_paths(work)
        output = _make_agent_output(
            kind="supervisor",
            agent_id="supervisor-1",
            commands=[
                PatchCommand(
                    command_id=str(uuid4()),
                    op="split_block",
                    agent_id="supervisor-1",
                    rationale="Split this block",
                )
            ],
        )
        save_agent_output(paths, output)

        result = run_agent_output_validate(work=work, output_id=output.output_id, cfg=_cfg())
        out = json.loads(capsys.readouterr().out)

        assert result == 1
        assert out["valid"] is False
        assert any("compilation is not implemented" in e for e in out["errors"])

    def test_submit_dry_run_rejects_uncompiled_commands(self, initialized_work, capsys):
        work, book = initialized_work
        paths = resolve_editor_paths(work)
        output = _make_agent_output(
            kind="supervisor",
            agent_id="supervisor-1",
            commands=[
                PatchCommand(
                    command_id=str(uuid4()),
                    op="split_block",
                    agent_id="supervisor-1",
                    rationale="Split this block",
                )
            ],
        )
        save_agent_output(paths, output)

        result = run_agent_output_submit(
            work=work,
            output_id=output.output_id,
            apply=False,
            stage=False,
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)

        assert result == 1
        assert out["valid"] is False
        assert any("compilation is not implemented" in e for e in out["errors"])
        assert (paths.agent_outputs_dir / f"{output.output_id}.json").exists()

    def test_validate_invalid_chapter_uid(self, initialized_work):
        work, book = initialized_work
        paths = resolve_editor_paths(work)

        # Create an output with a nonexistent chapter_uid by direct file manipulation
        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="fixer",
            agent_id="fixer-1",
            chapter_uid="nonexistent-chapter-uid",
            created_at=now,
            updated_at=now,
        )
        paths.agent_outputs_dir.mkdir(parents=True, exist_ok=True)
        save_agent_output(paths, output)

        errors = validate_agent_output(output, book)
        assert any("chapter_uid not found" in e for e in errors)

    def test_validate_scanner_topology_patch(self, initialized_work):
        """Scanner submitting insert_node should fail validate."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid

        # Build an in-memory scanner output with a topology patch
        now = _now_ts()
        new_uid = str(uuid4())
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="scanner",
            agent_id="scanner-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
            patches=[
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="scanner-1",
                    scope=PatchScope(chapter_uid=chapter_uid),
                    changes=[
                        {
                            "op": "insert_node",
                            "parent_uid": chapter_uid,
                            "after_uid": None,
                            "node": {"uid": new_uid, "kind": "paragraph", "text": "New", "role": "body", "provenance": {"page": 1, "bbox": None, "source": "passthrough"}},
                        }
                    ],
                    rationale="Adding a block",
                )
            ],
            memory_patches=[
                MemoryPatch(chapter_status=[ChapterStatus(chapter_uid=chapter_uid, read_passes=1)])
            ],
        )

        errors = validate_agent_output(output, book)
        assert any("scanner may only submit set_field" in e for e in errors)

    def test_validate_supervisor_any_patch(self, initialized_work):
        """Supervisor submitting topology patch should be valid (pass)."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        block_uid = book.chapters[0].blocks[0].uid

        now = _now_ts()
        # supervisor with a set_field patch
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="supervisor",
            agent_id="supervisor-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
            patches=[
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="supervisor-1",
                    scope=PatchScope(chapter_uid=chapter_uid),
                    changes=[
                        {
                            "op": "set_field",
                            "target_uid": block_uid,
                            "field": "text",
                            "old": "Hello world",
                            "new": "Hello world updated",
                        }
                    ],
                    rationale="supervisor fix",
                )
            ],
        )

        errors = validate_agent_output(output, book)
        # No kind-specific errors for supervisor
        kind_errors = [e for e in errors if "supervisor" in e.lower() and "may not" in e.lower()]
        assert len(kind_errors) == 0

    def test_validate_catches_stale_patch_precondition_without_side_effects(self, initialized_work):
        work, book = initialized_work
        block_uid = book.chapters[0].blocks[0].uid
        before = book.model_dump(mode="python")

        output = _make_agent_output(
            kind="supervisor",
            agent_id="supervisor-1",
            patches=[
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="supervisor-1",
                    scope=PatchScope(chapter_uid=book.chapters[0].uid),
                    changes=[
                        {
                            "op": "set_field",
                            "target_uid": block_uid,
                            "field": "text",
                            "old": "stale text",
                            "new": "Hello world updated",
                        }
                    ],
                    rationale="stale patch",
                )
            ],
        )

        errors = validate_agent_output(output, book)

        assert any("precondition mismatch" in e for e in errors)
        assert book.model_dump(mode="python") == before

    def test_validate_checks_multiple_patches_in_submission_order(self, initialized_work):
        work, book = initialized_work
        block_uid = book.chapters[0].blocks[0].uid

        output = _make_agent_output(
            kind="supervisor",
            agent_id="supervisor-1",
            patches=[
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="supervisor-1",
                    scope=PatchScope(chapter_uid=book.chapters[0].uid),
                    changes=[
                        {
                            "op": "set_field",
                            "target_uid": block_uid,
                            "field": "text",
                            "old": "Hello world",
                            "new": "Intermediate text",
                        }
                    ],
                    rationale="first patch",
                ),
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="supervisor-1",
                    scope=PatchScope(chapter_uid=book.chapters[0].uid),
                    changes=[
                        {
                            "op": "set_field",
                            "target_uid": block_uid,
                            "field": "text",
                            "old": "Intermediate text",
                            "new": "Final text",
                        }
                    ],
                    rationale="second patch",
                ),
            ],
        )

        errors = validate_agent_output(output, book)

        assert errors == []
        assert book.chapters[0].blocks[0].text == "Hello world"  # type: ignore[union-attr]

    def test_validate_scanner_no_read_pass_update(self, initialized_work):
        """Scanner without read_passes update should fail validation."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid

        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="scanner",
            agent_id="scanner-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
            # No memory_patches with read_passes > 0
        )

        errors = validate_agent_output(output, book)
        assert any("read_passes" in e for e in errors)

    def test_validate_multiple_errors(self, initialized_work):
        """Multiple errors should all be collected (not fail-fast)."""
        work, book = initialized_work

        now = _now_ts()
        # Output with invalid chapter_uid AND scanner with no read_passes
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="scanner",
            agent_id="scanner-1",
            chapter_uid="bad-chapter-uid-xxxx",
            created_at=now,
            updated_at=now,
        )

        errors = validate_agent_output(output, book)
        # Should have at least: chapter_uid not found + read_passes missing
        assert len(errors) >= 2


# ===========================================================================
# §8.9 submit tests
# ===========================================================================


class TestSubmit:
    def _begin_supervisor(self, work, book, capsys):
        run_agent_output_begin(
            work=work, kind="supervisor", agent="supervisor-1", chapter=None, cfg=_cfg()
        )
        return json.loads(capsys.readouterr().out)["output_id"]

    def test_submit_dry_run_no_side_effects(self, initialized_work, capsys):
        """Dry-run (no --apply): book.json should not change."""
        work, book = initialized_work
        oid = self._begin_supervisor(work, book, capsys)

        paths = resolve_editor_paths(work)
        before_mtime = paths.book_path.stat().st_mtime

        run_agent_output_submit(
            work=work,
            output_id=oid,
            apply=False,
            stage=False,
            cfg=_cfg(),
        )
        capsys.readouterr()

        after_mtime = paths.book_path.stat().st_mtime
        assert after_mtime == before_mtime  # book not touched

    def test_submit_apply_empty_patches(self, initialized_work, capsys):
        """Apply with no patches: output should be archived, book unchanged."""
        work, book = initialized_work
        oid = self._begin_supervisor(work, book, capsys)

        paths = resolve_editor_paths(work)
        book_before = load_book(paths.book_path)

        run_agent_output_submit(
            work=work,
            output_id=oid,
            apply=True,
            stage=False,
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)

        assert out["submitted"] is True
        assert out["patches_applied"] == 0

        # Output file should be gone (archived)
        assert not (paths.agent_outputs_dir / f"{oid}.json").exists()

        # book unchanged
        book_after = load_book(paths.book_path)
        assert book_after.op_log_version == book_before.op_log_version

    def test_submit_apply_with_set_field_patch(self, initialized_work, tmp_path, capsys):
        """Apply with a set_field patch: book.json should be updated."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        block_uid = book.chapters[0].blocks[0].uid

        run_agent_output_begin(
            work=work,
            kind="supervisor",
            agent="supervisor-1",
            chapter=chapter_uid,
            cfg=_cfg(),
        )
        oid = json.loads(capsys.readouterr().out)["output_id"]

        patch_data = {
            "patch_id": str(uuid4()),
            "agent_id": "supervisor-1",
            "scope": {"chapter_uid": chapter_uid},
            "changes": [
                {
                    "op": "set_field",
                    "target_uid": block_uid,
                    "field": "text",
                    "old": "Hello world",
                    "new": "Hello world updated",
                }
            ],
            "rationale": "Update text",
        }
        patch_file = tmp_path / "patch.json"
        patch_file.write_text(json.dumps(patch_data), encoding="utf-8")

        run_agent_output_add_patch(
            work=work, output_id=oid, patch_file=patch_file, cfg=_cfg()
        )
        capsys.readouterr()

        run_agent_output_submit(
            work=work, output_id=oid, apply=True, stage=False, cfg=_cfg()
        )
        out = json.loads(capsys.readouterr().out)

        assert out["submitted"] is True
        assert out["patches_applied"] == 1

        # Verify book was updated
        paths = resolve_editor_paths(work)
        updated_book = load_book(paths.book_path)
        updated_block = updated_book.chapters[0].blocks[0]
        assert isinstance(updated_block, Paragraph)
        assert updated_block.text == "Hello world updated"

    def test_submit_apply_validation_fail_no_side_effects(self, initialized_work, capsys):
        """Validation failure should prevent any state changes."""
        work, book = initialized_work
        paths = resolve_editor_paths(work)

        # Create a scanner output with NO read_passes update (will fail validation)
        chapter_uid = book.chapters[0].uid
        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="scanner",
            agent_id="scanner-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
        )
        paths.agent_outputs_dir.mkdir(parents=True, exist_ok=True)
        save_agent_output(paths, output)

        book_mtime_before = paths.book_path.stat().st_mtime

        run_agent_output_submit(
            work=work,
            output_id=output.output_id,
            apply=True,
            stage=False,
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)

        assert out["submitted"] is False
        assert len(out["errors"]) > 0
        assert paths.book_path.stat().st_mtime == book_mtime_before

    def test_submit_apply_with_command_fails_without_archiving_or_saving(
        self,
        initialized_work,
        capsys,
    ):
        """Uncompiled commands must not be silently dropped during submit --apply."""
        work, book = initialized_work
        paths = resolve_editor_paths(work)
        output = _make_agent_output(
            kind="supervisor",
            agent_id="supervisor-1",
            commands=[
                PatchCommand(
                    command_id=str(uuid4()),
                    op="split_block",
                    agent_id="supervisor-1",
                    rationale="Split this block",
                )
            ],
        )
        save_agent_output(paths, output)
        book_before = load_book(paths.book_path).model_dump(mode="python")
        memory_before = load_editor_memory(paths).model_dump(mode="python")

        result = run_agent_output_submit(
            work=work,
            output_id=output.output_id,
            apply=True,
            stage=False,
            cfg=_cfg(),
        )
        out = json.loads(capsys.readouterr().out)

        assert result == 1
        assert out["submitted"] is False
        assert any("compilation is not implemented" in e for e in out["errors"])
        assert (paths.agent_outputs_dir / f"{output.output_id}.json").exists()
        assert list(paths.agent_outputs_archives_dir.glob(f"{output.output_id}_*.json")) == []
        assert load_book(paths.book_path).model_dump(mode="python") == book_before
        assert load_editor_memory(paths).model_dump(mode="python") == memory_before

    def test_submit_apply_archives_output(self, initialized_work, capsys):
        """After successful submit, in-progress output file moves to archives/."""
        work, book = initialized_work
        oid = self._begin_supervisor(work, book, capsys)
        paths = resolve_editor_paths(work)

        assert (paths.agent_outputs_dir / f"{oid}.json").exists()

        run_agent_output_submit(
            work=work, output_id=oid, apply=True, stage=False, cfg=_cfg()
        )
        capsys.readouterr()

        assert not (paths.agent_outputs_dir / f"{oid}.json").exists()
        # Archives directory should contain the file
        archives = list(paths.agent_outputs_archives_dir.glob(f"{oid}_*.json"))
        assert len(archives) == 1

    def test_submit_stage_placeholder(self, initialized_work, capsys):
        """--stage mode returns placeholder message and exits 0."""
        work, book = initialized_work
        oid = self._begin_supervisor(work, book, capsys)

        result = run_agent_output_submit(
            work=work, output_id=oid, apply=False, stage=True, cfg=_cfg()
        )
        out = json.loads(capsys.readouterr().out)

        assert result == 0
        assert out["staged"] is False
        assert "not yet implemented" in out["message"]


# ===========================================================================
# §8.10 Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_load_output_corrupted_json(self, initialized_work):
        """Corrupted output JSON should raise CommandError."""
        work, book = initialized_work
        paths = resolve_editor_paths(work)
        paths.agent_outputs_dir.mkdir(parents=True, exist_ok=True)

        corrupt_id = str(uuid4())
        (paths.agent_outputs_dir / f"{corrupt_id}.json").write_text(
            "{corrupt json!!!}", encoding="utf-8"
        )

        with pytest.raises(CommandError):
            load_agent_output(paths, corrupt_id)

    def test_add_note_duplicate_appended_not_deduplicated(self, initialized_work, capsys):
        """Adding the same note twice results in two entries (append semantics)."""
        work, book = initialized_work
        run_agent_output_begin(
            work=work, kind="supervisor", agent="sup-1", chapter=None, cfg=_cfg()
        )
        oid = json.loads(capsys.readouterr().out)["output_id"]

        run_agent_output_add_note(work=work, output_id=oid, text="Same note", cfg=_cfg())
        capsys.readouterr()
        run_agent_output_add_note(work=work, output_id=oid, text="Same note", cfg=_cfg())
        out = json.loads(capsys.readouterr().out)

        assert out["notes_count"] == 2

    def test_archive_target_already_exists_overwritten(self, initialized_work, capsys):
        """If archive target already exists, atomic_write_text should overwrite it."""
        work, book = initialized_work
        oid_run1 = None

        # Submit once to create an archive
        run_agent_output_begin(
            work=work, kind="supervisor", agent="sup-1", chapter=None, cfg=_cfg()
        )
        oid_run1 = json.loads(capsys.readouterr().out)["output_id"]
        run_agent_output_submit(
            work=work, output_id=oid_run1, apply=True, stage=False, cfg=_cfg()
        )
        capsys.readouterr()

        paths = resolve_editor_paths(work)
        archives = list(paths.agent_outputs_archives_dir.glob(f"{oid_run1}_*.json"))
        assert len(archives) == 1


# ===========================================================================
# Additional validate tests from §8.8
# ===========================================================================


class TestValidateExtended:
    def test_validate_scanner_with_read_pass_update(self, initialized_work):
        """Scanner output with read_passes > 0 should be valid."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid

        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="scanner",
            agent_id="scanner-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
            memory_patches=[
                MemoryPatch(
                    chapter_status=[ChapterStatus(chapter_uid=chapter_uid, read_passes=1)]
                )
            ],
        )

        errors = validate_agent_output(output, book)
        # Should not have read_passes error
        read_pass_errors = [e for e in errors if "read_passes" in e]
        assert len(read_pass_errors) == 0

    def test_validate_fixer_direct_topology_patch_rejected(self, initialized_work):
        """Fixer submitting insert_node BookPatch should fail validation."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid

        now = _now_ts()
        new_uid = str(uuid4())
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="fixer",
            agent_id="fixer-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
            patches=[
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="fixer-1",
                    scope=PatchScope(chapter_uid=chapter_uid),
                    changes=[
                        {
                            "op": "insert_node",
                            "parent_uid": chapter_uid,
                            "after_uid": None,
                            "node": {
                                "uid": new_uid,
                                "kind": "paragraph",
                                "text": "New",
                                "role": "body",
                                "provenance": {"page": 1, "bbox": None, "source": "passthrough"},
                            },
                        }
                    ],
                    rationale="Add block",
                )
            ],
        )

        errors = validate_agent_output(output, book)
        assert any("fixer may not submit topology" in e for e in errors)

    def test_validate_reviewer_topology_patch(self, initialized_work):
        """Reviewer submitting delete_node should fail validation."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        block_uid = book.chapters[0].blocks[0].uid

        # Get the actual block data for old_node snapshot
        block = book.chapters[0].blocks[0]

        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="reviewer",
            agent_id="reviewer-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
            patches=[
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="reviewer-1",
                    scope=PatchScope(chapter_uid=chapter_uid),
                    changes=[
                        {
                            "op": "delete_node",
                            "target_uid": block_uid,
                            "old_node": block.model_dump(mode="python"),
                        }
                    ],
                    rationale="Delete block",
                )
            ],
        )

        errors = validate_agent_output(output, book)
        assert any("reviewer may only submit set_field" in e for e in errors)

    def test_validate_scope_chapter_none_is_book_wide(self, initialized_work):
        """PatchScope with chapter_uid=None is book-wide; scanner submitting it errors."""
        work, book = initialized_work
        chapter_uid = book.chapters[0].uid
        block_uid = book.chapters[0].blocks[0].uid

        now = _now_ts()
        output = AgentOutput(
            output_id=str(uuid4()),
            kind="scanner",
            agent_id="scanner-1",
            chapter_uid=chapter_uid,
            created_at=now,
            updated_at=now,
            patches=[
                BookPatch(
                    patch_id=str(uuid4()),
                    agent_id="scanner-1",
                    scope=PatchScope(chapter_uid=None),  # book-wide
                    changes=[
                        {
                            "op": "set_field",
                            "target_uid": block_uid,
                            "field": "text",
                            "old": "Hello world",
                            "new": "Hello world updated",
                        }
                    ],
                    rationale="book-wide set_field",
                )
            ],
            memory_patches=[
                MemoryPatch(chapter_status=[ChapterStatus(chapter_uid=chapter_uid, read_passes=1)])
            ],
        )

        errors = validate_agent_output(output, book)
        # Should error: chapter-scoped output cannot have book-wide patch
        assert any("book-wide" in e or "scope.chapter_uid=None" in e for e in errors)
