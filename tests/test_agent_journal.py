"""Durable agent run journal tests: streams, manifests, and stage integration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Any

import pytest

from epubforge import agent_journal as journal_module
from epubforge import agent_runner as runner_module
from epubforge.agent_journal import (
    AGENT_RUN_SCHEMA,
    AGENT_RUN_SCHEMA_VERSION,
    AgentRunJournalError,
    create_agent_run_journal,
    read_agent_run_manifest,
)
from epubforge.agent_runner import (
    AgentExecutionError,
    AgentRunRequest,
    AgentRunnerError,
    OpenCodeAgentRunner,
    book_editor_identity,
)
from epubforge.chapter_revision import (
    ChapterRevisionError,
    revise_chapter,
)
from epubforge.chapter_segmentation import (
    ChapterSegmentationError,
    segment_chapters,
)
from epubforge.chapter_workspace import build_chapter_workspace


_seg_fixture_spec = importlib.util.spec_from_file_location(
    "chapter_seg_fixture", Path(__file__).with_name("test_chapter_segmentation.py")
)
assert _seg_fixture_spec is not None and _seg_fixture_spec.loader is not None
_seg_fixture = importlib.util.module_from_spec(_seg_fixture_spec)
_seg_fixture_spec.loader.exec_module(_seg_fixture)

_ws_fixture_spec = importlib.util.spec_from_file_location(
    "workspace_fixture", Path(__file__).with_name("test_chapter_workspace.py")
)
assert _ws_fixture_spec is not None and _ws_fixture_spec.loader is not None
_ws_fixture = importlib.util.module_from_spec(_ws_fixture_spec)
_ws_fixture_spec.loader.exec_module(_ws_fixture)

_HELPER = r"""
import json
import os
import sys
from pathlib import Path

workspace = (
    Path(sys.argv[sys.argv.index("--dir") + 1])
    if "--dir" in sys.argv
    else None
)
# Stage integration tests set EPUBFORGE_TEST_AGENT_MODE because they pass the
# packaged prompt instead of a mode string as the final argv argument.
mode = os.environ.get("EPUBFORGE_TEST_AGENT_MODE", sys.argv[-1])

if sys.argv[1:4] == ["debug", "config", "--pure"]:
    print(json.dumps(json.loads(os.environ.get("EPUBFORGE_TEST_DEBUG_CONFIG", "{}"))))
    inherited = os.environ.get("OPENCODE_CONFIG_CONTENT")
    if inherited is not None:
        sys.stderr.write(inherited + "\n")
elif mode == "success":
    (workspace / "result.json").write_text("{}", encoding="utf-8")
    print(json.dumps({"type": "text", "sessionID": "ses_journal_test"}))
elif mode == "stderr-writer":
    (workspace / "result.json").write_text("{}", encoding="utf-8")
    sys.stderr.write("probe warning line\n")
    print(json.dumps({"type": "text", "sessionID": "ses_journal_test"}))
elif mode == "secret-echo":
    (workspace / "result.json").write_text("{}", encoding="utf-8")
    secret = os.environ.get("EPUBFORGE_TEST_AGENT_SECRET", "")
    # The secret reaches both raw streams inside valid events so the run can
    # still complete and be accepted; only stream persistence would leak it.
    print(json.dumps({"type": "text", "sessionID": "ses_secret_echo", "secret": secret}))
    sys.stderr.write("stderr secret echo: " + secret + "\n")
elif mode == "exit-failure":
    print(json.dumps({"type": "text", "sessionID": "ses_exit_failure"}))
    sys.stderr.write("agent failure detail\n")
    raise SystemExit(9)
elif mode == "bad-json":
    print("not-json")
elif mode == "no-output":
    print(json.dumps({"type": "text", "sessionID": "ses_no_output"}))
elif mode == "segmentation-ok":
    projection = json.loads(
        (workspace / "content-projection.json").read_text(encoding="utf-8")
    )
    coverage = projection["coverage"]
    items = [
        {
            "text": "".join(item["text_chunks"]),
            "content_idx": item["content_idx"],
            "page_idx": item["page_idx"],
        }
        for item in projection["items"]
    ]
    boundaries = [
        {
            "title": items[0]["text"],
            "kind": "frontmatter",
            "start_content_idx": items[0]["content_idx"],
            "start_page_idx": items[0]["page_idx"],
            "confidence": 0.9,
            "evidence": "opening contents section",
        },
        {
            "title": items[1]["text"],
            "kind": "chapter",
            "start_content_idx": items[1]["content_idx"],
            "start_page_idx": items[1]["page_idx"],
            "confidence": 0.9,
            "evidence": "first chapter heading",
        },
    ]
    (workspace / "boundaries.json").write_text(
        json.dumps(
            {"status": "success", "coverage": coverage, "boundaries": boundaries}
        ),
        encoding="utf-8",
    )
    print(json.dumps({"type": "text", "sessionID": "ses_segment_ok"}))
elif mode == "segmentation-bad":
    (workspace / "boundaries.json").write_text(
        json.dumps(
            {
                "status": "success",
                "coverage": {
                    "item_count": 1,
                    "first_content_idx": 0,
                    "last_content_idx": 0,
                    "first_page_idx": 0,
                    "last_page_idx": 0,
                    "items_sha256": "0" * 64,
                },
                "boundaries": [],
            }
        ),
        encoding="utf-8",
    )
    print(json.dumps({"type": "text", "sessionID": "ses_segment_bad"}))
elif mode == "revision-ok":
    (workspace / "bbox-repairs.json").write_text(
        json.dumps({"schema_version": 1, "repairs": []}), encoding="utf-8"
    )
    (workspace / "paddle-evidence.json").write_text(
        json.dumps({"schema_version": 1, "pages": []}), encoding="utf-8"
    )
    (workspace / "asset-claims.json").write_text(
        json.dumps({"schema_version": 1, "claims": []}), encoding="utf-8"
    )
    print(json.dumps({"type": "text", "sessionID": "ses_revision_ok"}))
elif mode == "revision-bad":
    (workspace / "corrected.html").write_text(
        "<!DOCTYPE html><html><head><title>t</title></head><body></body></html>",
        encoding="utf-8",
    )
    (workspace / "bbox-repairs.json").write_text(
        json.dumps({"schema_version": 1, "repairs": []}), encoding="utf-8"
    )
    (workspace / "paddle-evidence.json").write_text(
        json.dumps({"schema_version": 1, "pages": []}), encoding="utf-8"
    )
    (workspace / "asset-claims.json").write_text(
        json.dumps({"schema_version": 1, "claims": []}), encoding="utf-8"
    )
    print(json.dumps({"type": "text", "sessionID": "ses_revision_bad"}))
else:
    raise SystemExit(9)
"""


@pytest.fixture(autouse=True)
def _clear_inherited_opencode_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_PATH",
        "OPENCODE_CONFIG_CONTENT",
    ):
        monkeypatch.delenv(key, raising=False)


def _helper(tmp_path: Path) -> Path:
    executable = tmp_path / "fake opencode"
    executable.write_text(f"#!{sys.executable}\n{_HELPER}", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _runner(tmp_path: Path, **kwargs: Any) -> OpenCodeAgentRunner:
    temp_root = tmp_path / "isolated"
    temp_root.mkdir(parents=True, exist_ok=True)
    return OpenCodeAgentRunner(
        executable=_helper(tmp_path),
        temp_root=temp_root,
        timeout_seconds=10,
        terminate_grace_seconds=0.1,
        **kwargs,
    )


def _request(mode: str = "success") -> AgentRunRequest:
    return AgentRunRequest(
        title="journal contract test",
        prompt=mode,
        files={"TASK.md": b"task\n", "nested/evidence.txt": b"evidence\n"},
        output_limits={"result.json": 64 * 1024},
    )


def _journal_root(tmp_path: Path) -> Path:
    return tmp_path / "book" / "logs" / "agent-runs"


def _single_run_dir(root: Path) -> Path:
    runs = sorted(root.glob("*"))
    assert len(runs) == 1, f"expected one run dir, found {runs}"
    return runs[0]


def _agent_process(manifest: Any) -> Any:
    return next(process for process in manifest.processes if process.label == "agent")


def _drive_success(
    journal: Any,
    *,
    session_id: str | None = "ses_test",
    workspace: str = "/tmp/journal-test-workspace",
) -> None:
    """Replay the runner's recorder calls for one fully successful run."""
    journal.record_workspace(workspace)
    journal.record_input("TASK.md", 5, hashlib.sha256(b"task\n").hexdigest())
    journal.process_started("config-probe", capture_streams=False)
    journal.process_pid("config-probe", 1001)
    journal.process_finished("config-probe", 0)
    journal.process_started("agent", capture_streams=True)
    journal.process_pid("agent", 1002)
    journal.write_chunk(
        "agent", "stdout", f'{{"type": "text", "sessionID": "{session_id}"}}\n'.encode()
    )
    journal.write_chunk("agent", "stderr", b"")
    journal.process_finished("agent", 0)
    journal.session_parsed(session_id, mode="strict")
    journal.record_output("result.json", 2, hashlib.sha256(b"{}").hexdigest())
    journal.output_collection_complete()


def _seg_content(work: Path) -> Path:
    work.mkdir(parents=True)
    return _seg_fixture._write_content(work)


def _workspace(tmp_path: Path) -> Path:
    return build_chapter_workspace(_ws_fixture._write_inputs(tmp_path))


def test_successful_run_journals_streams_manifest_and_acceptance(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="epubforge.agent_journal")
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )
    result = runner(_request())
    assert result.journal is not None
    result.journal.mark_accepted()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.schema_name == AGENT_RUN_SCHEMA
    assert manifest.schema_version == AGENT_RUN_SCHEMA_VERSION
    assert manifest.run_id == run_dir.name
    assert manifest.title == "journal contract test"
    assert manifest.agent.name == "book-editor"
    assert manifest.agent.variant == "medium"
    assert manifest.agent.fingerprint == book_editor_identity().fingerprint
    assert manifest.agent.execution_profile == "development"
    assert manifest.started_at_utc
    assert manifest.ended_at_utc
    assert manifest.session_id == "ses_journal_test"
    assert manifest.validation.status == "accepted"
    assert manifest.validation.published is True
    assert manifest.validation.published_at_utc is not None
    assert manifest.output_collection.status == "complete"
    assert manifest.final_status == "accepted"
    assert manifest.runner_error is None

    assert [process.label for process in manifest.processes] == [
        "config-probe",
        "agent",
    ]
    probe, agent = manifest.processes
    assert probe.result == "success" and probe.returncode == 0
    assert agent.result == "success" and agent.returncode == 0
    assert agent.pid is not None and agent.ended_at_utc is not None
    assert agent.stdout is not None and agent.stderr is not None

    stdout = (run_dir / agent.stdout.filename).read_bytes()
    assert stdout == b'{"type": "text", "sessionID": "ses_journal_test"}\n'
    assert agent.stdout.size_bytes == len(stdout)
    assert agent.stdout.sha256 == hashlib.sha256(stdout).hexdigest()
    assert (run_dir / agent.stderr.filename).read_bytes() == b""
    assert agent.stderr.size_bytes == 0

    (output,) = manifest.outputs
    assert output.name == "result.json"
    assert output.limit == 64 * 1024
    assert output.status == "collected"
    assert output.sha256 == hashlib.sha256(b"{}").hexdigest()
    assert {entry.name for entry in manifest.inputs} == {
        "TASK.md",
        "nested/evidence.txt",
    }
    for entry in manifest.inputs:
        assert entry.size_bytes >= 0 and len(entry.sha256) == 64
    assert manifest.workspace_path is not None
    # the isolated workspace is removed as before; only the journal remains
    assert not Path(manifest.workspace_path).exists()

    assert "agent run" in caplog.text
    assert "session_id=ses_journal_test" in caplog.text
    assert str(run_dir / "manifest.json") in caplog.text


def test_nonzero_exit_fails_run_and_persists_streams(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )
    with pytest.raises(AgentExecutionError) as caught:
        runner(_request("exit-failure"))
    assert caught.value.returncode == 9

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    agent = _agent_process(manifest)
    assert agent.returncode == 9
    assert agent.result == "nonzero"
    assert manifest.session_id == "ses_exit_failure"
    assert (run_dir / agent.stdout.filename).read_bytes() == (
        b'{"type": "text", "sessionID": "ses_exit_failure"}\n'
    )
    assert (run_dir / agent.stderr.filename).read_bytes() == b"agent failure detail\n"
    assert manifest.validation.status == "pending"


def test_invalid_session_json_fails_run_and_persists_streams(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )
    with pytest.raises(AgentExecutionError, match="invalid JSON"):
        runner(_request("bad-json"))

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    assert manifest.session_id is None
    agent = _agent_process(manifest)
    assert agent.result == "success"
    assert (run_dir / agent.stdout.filename).read_bytes() == b"not-json\n"


def test_missing_output_fails_collection_and_run(tmp_path: Path) -> None:
    runner = _runner(tmp_path, journal_root=_journal_root(tmp_path))
    with pytest.raises(AgentRunnerError, match="did not produce"):
        runner(_request("no-output"))

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.output_collection.status == "failed"
    assert manifest.output_collection.error_type == "AgentRunnerError"
    (output,) = manifest.outputs
    assert output.status == "uncollected"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentRunnerError"
    assert manifest.session_id == "ses_no_output"


def test_stdout_and_stderr_hashes_match_persisted_streams(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )
    result = runner(_request("stderr-writer"))
    assert result.journal is not None
    result.journal.mark_accepted()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    agent = _agent_process(manifest)
    assert agent.capture == "streams"
    assert agent.capture_complete is True
    for stream in ("stdout", "stderr"):
        record = getattr(agent, stream)
        data = (run_dir / record.filename).read_bytes()
        assert record.size_bytes == len(data)
        assert record.sha256 == hashlib.sha256(data).hexdigest()
    assert (run_dir / "stderr.bin").read_bytes() == b"probe warning line\n"


def test_segment_semantic_rejection_marks_run_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-bad")
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
    )
    with pytest.raises(ChapterSegmentationError):
        segment_chapters(content_path, work / "03_chapters", runner)

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "rejected"
    assert manifest.validation.status == "rejected"
    assert manifest.validation.published is False
    assert manifest.validation.error_type == "ChapterSegmentationError"
    assert manifest.validation.error_message
    assert manifest.output_collection.status == "complete"
    assert manifest.session_id == "ses_segment_bad"
    assert not (work / "03_chapters" / "chapters.json").exists()


def test_segment_accepted_publication_marks_run_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-ok")
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
    )
    output_path = segment_chapters(content_path, work / "03_chapters", runner)
    assert output_path == work / "03_chapters" / "chapters.json"
    assert output_path.is_file()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "accepted"
    assert manifest.validation.status == "accepted"
    assert manifest.validation.published is True
    assert manifest.agent.execution_profile == "restricted"
    assert manifest.session_id == "ses_segment_ok"


def test_revision_accepted_publication_marks_run_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "revision-ok")
    workspace = _workspace(tmp_path)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="development",
    )
    chapter_dir = workspace / "chapters/0002"
    corrected = revise_chapter(chapter_dir, runner)
    assert corrected == chapter_dir / "corrected.html"
    assert (chapter_dir / "revision.json").is_file()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "accepted"
    assert manifest.validation.status == "accepted"
    assert manifest.validation.published is True
    assert manifest.title == "epubforge chapter 0002"
    assert manifest.session_id == "ses_revision_ok"
    assert manifest.output_collection.status == "complete"


def test_revision_semantic_rejection_marks_run_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "revision-bad")
    workspace = _workspace(tmp_path)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="development",
    )
    chapter_dir = workspace / "chapters/0002"
    with pytest.raises(ChapterRevisionError):
        revise_chapter(chapter_dir, runner)

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "rejected"
    assert manifest.validation.status == "rejected"
    assert manifest.validation.published is False
    assert manifest.validation.error_type == "ChapterRevisionError"
    assert manifest.session_id == "ses_revision_bad"


def test_concurrent_runs_get_unique_directories(tmp_path: Path) -> None:
    journal_root = _journal_root(tmp_path)
    errors: list[BaseException] = []

    def run_one(index: int) -> None:
        try:
            runner = _runner(tmp_path / f"iso-{index}", journal_root=journal_root)
            result = runner(_request())
            assert result.journal is not None
            result.journal.mark_accepted()
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run_one, range(2)))

    assert errors == []
    runs = sorted(journal_root.glob("*"))
    assert len(runs) == 2
    assert len({path.name for path in runs}) == 2
    manifests = [read_agent_run_manifest(path / "manifest.json") for path in runs]
    assert {manifest.run_id for manifest in manifests} == {path.name for path in runs}
    assert all(manifest.final_status == "accepted" for manifest in manifests)


def test_journal_dirs_and_files_have_restrictive_modes(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )
    result = runner(_request())
    assert result.journal is not None
    result.journal.mark_accepted()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    for name in ("manifest.json", "stdout.jsonl", "stderr.bin"):
        assert stat.S_IMODE((run_dir / name).stat().st_mode) == 0o600
    # config-probe streams are never captured; only probe metadata is recorded.
    assert not (run_dir / "config-probe.stdout.jsonl").exists()
    assert not (run_dir / "config-probe.stderr.bin").exists()


def test_probe_streams_and_inherited_secrets_never_reach_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "PROBE-SECRET-" + secrets.token_hex(8)
    provider = {
        "custom": {
            "options": {"baseURL": "https://provider.example/v1", "apiKey": secret},
            "models": {"custom-model": {"name": "custom-model"}},
        }
    }
    # The secret reaches the probe through every sensitive channel: its stdout
    # (the debug config payload it prints), its stderr, and the provider
    # config environment it inherits (echoed to stderr by the probe helper).
    monkeypatch.setenv(
        "EPUBFORGE_TEST_DEBUG_CONFIG", json.dumps({"provider": provider})
    )
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"provider": provider}))

    runner = _runner(tmp_path, journal_root=_journal_root(tmp_path))
    result = runner(_request())
    assert result.journal is not None
    result.journal.mark_accepted()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    assert not (run_dir / "config-probe.stdout.jsonl").exists()
    assert not (run_dir / "config-probe.stderr.bin").exists()
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode("utf-8") not in path.read_bytes(), path
    assert secret not in caplog.text

    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    probe = next(
        process for process in manifest.processes if process.label == "config-probe"
    )
    assert probe.capture == "disabled"
    assert probe.stdout is None and probe.stderr is None
    assert probe.returncode == 0 and probe.result == "success"
    assert probe.pid is not None and probe.ended_at_utc is not None
    # the main agent also runs capture-disabled under the safe default
    agent = _agent_process(manifest)
    assert agent.capture == "disabled"
    assert agent.stdout is None and agent.stderr is None
    assert agent.capture_complete is None
    assert not (run_dir / "stdout.jsonl").exists()
    assert not (run_dir / "stderr.bin").exists()
    assert manifest.session_id == "ses_journal_test"


def test_main_agent_secret_echo_never_reaches_safe_default_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Safe-default journaling never persists main-agent raw stream bytes.

    The main agent echoes a distinctive inherited-environment secret into
    both of its raw streams; with capture disabled, the secret must not reach
    the journal directory, the manifest, or the Python logs, while the run
    still completes, parses its session, and is accepted.
    """
    caplog.set_level(logging.DEBUG)
    secret = "MAIN-SECRET-" + secrets.token_hex(8)
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_SECRET", secret)

    runner = _runner(tmp_path, journal_root=_journal_root(tmp_path))
    result = runner(_request("secret-echo"))
    assert result.journal is not None
    result.journal.mark_accepted()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    # no stream files at all under the safe default
    assert sorted(path.name for path in run_dir.iterdir()) == ["manifest.json"]
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode("utf-8") not in path.read_bytes(), path
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "accepted"
    assert manifest.session_id == "ses_secret_echo"
    agent = _agent_process(manifest)
    assert agent.capture == "disabled"
    assert agent.stdout is None and agent.stderr is None
    assert agent.capture_complete is None
    assert agent.result == "success"
    assert secret not in caplog.text


def test_opt_in_capture_persists_bounded_streams_with_hashes(tmp_path: Path) -> None:
    """The dangerous debug opt-in persists bounded streams with real hashes."""
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )
    result = runner(_request("stderr-writer"))
    assert result.journal is not None
    result.journal.mark_accepted()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    agent = _agent_process(manifest)
    assert agent.capture == "streams"
    assert agent.capture_complete is True
    assert agent.capture_error_type is None
    stdout = (run_dir / "stdout.jsonl").read_bytes()
    assert stdout == b'{"type": "text", "sessionID": "ses_journal_test"}\n'
    assert agent.stdout is not None
    assert agent.stdout.size_bytes == len(stdout)
    assert agent.stdout.sha256 == hashlib.sha256(stdout).hexdigest()
    stderr = (run_dir / "stderr.bin").read_bytes()
    assert stderr == b"probe warning line\n"
    assert agent.stderr is not None
    assert agent.stderr.size_bytes == len(stderr)
    assert agent.stderr.sha256 == hashlib.sha256(stderr).hexdigest()


def test_running_stream_capture_manifest_is_strictly_readable(
    tmp_path: Path,
) -> None:
    """A crash between start and finish leaves a valid running capture."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="running capture",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.process_pid("agent", 1002)
    journal.write_chunk("agent", "stdout", b'{"type": "text", "sessionID": "s1"}\n')

    manifest = read_agent_run_manifest(journal.manifest_path)
    agent = _agent_process(manifest)
    assert agent.capture == "streams"
    assert agent.capture_complete is None
    assert agent.capture_error_type is None and agent.capture_error_message is None
    assert agent.result == "not_run"
    assert agent.returncode is None and agent.ended_at_utc is None
    assert agent.pid == 1002
    assert agent.stdout is not None and agent.stderr is not None
    assert agent.stdout.size_bytes == 0
    assert agent.stderr.size_bytes == 0
    assert agent.stdout.sha256 == hashlib.sha256(b"").hexdigest()
    assert manifest.final_status == "started"
    assert manifest.validation.status == "pending"
    assert manifest.finalization is None
    journal.close()

    # the same running shape holds for a disabled-capture process
    disabled_journal = create_agent_run_journal(
        tmp_path / "logs2" / "agent-runs",
        title="running disabled capture",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    disabled_journal.process_started("agent", capture_streams=False)
    disabled_journal.process_pid("agent", 1003)
    disabled_manifest = read_agent_run_manifest(disabled_journal.manifest_path)
    disabled_agent = _agent_process(disabled_manifest)
    assert disabled_agent.capture == "disabled"
    assert disabled_agent.stdout is None and disabled_agent.stderr is None
    assert disabled_agent.capture_complete is None
    assert disabled_agent.result == "not_run"
    assert disabled_agent.pid == 1003
    disabled_journal.close()


def test_accepted_manifest_with_disabled_agent_capture_is_valid(
    tmp_path: Path,
) -> None:
    """Capture-disabled successful runs can be accepted and strictly read."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="disabled capture acceptance",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("config-probe", capture_streams=False)
    journal.process_pid("config-probe", 1001)
    journal.process_finished("config-probe", 0)
    journal.process_started("agent", capture_streams=False)
    journal.process_pid("agent", 1002)
    journal.process_finished("agent", 0)
    journal.session_parsed("ses_disabled", mode="strict")
    journal.record_output("result.json", 2, hashlib.sha256(b"{}").hexdigest())
    journal.output_collection_complete()
    journal.mark_accepted()

    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "accepted"
    assert manifest.validation.status == "accepted"
    assert manifest.session_id == "ses_disabled"
    assert manifest.session_parse_mode == "strict"
    agent = _agent_process(manifest)
    assert agent.capture == "disabled"
    assert agent.stdout is None and agent.stderr is None
    assert agent.capture_complete is None
    assert agent.result == "success"
    assert agent.returncode == 0
    assert not (journal.manifest_path.parent / "stdout.jsonl").exists()


def test_symlinked_journal_root_fails_the_run(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "logs"
    link.symlink_to(target, target_is_directory=True)
    runner = _runner(tmp_path, journal_root=link / "agent-runs")

    with pytest.raises(AgentRunnerError, match="journaling failed"):
        runner(_request())


def test_journal_root_that_is_a_file_fails_the_run(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    runner = _runner(tmp_path, journal_root=blocker / "logs" / "agent-runs")

    with pytest.raises(AgentRunnerError, match="journaling failed"):
        runner(_request())


def test_runners_without_journal_root_do_not_journal(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    result = runner(_request())
    assert result.journal is None


def test_rejected_error_message_is_bounded(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="bounded message test",
        identity=book_editor_identity("restricted"),
        execution_profile="restricted",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)
    journal.mark_rejected("ChapterSegmentationError", "x" * 5000)
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "rejected"
    assert manifest.validation.error_type == "ChapterSegmentationError"
    assert manifest.validation.error_message is not None
    assert len(manifest.validation.error_message) == 2000


def test_double_finalize_is_rejected(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="double finalize",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)
    journal.mark_accepted()
    with pytest.raises(AgentRunJournalError, match="already finalized"):
        journal.mark_rejected("ChapterSegmentationError", "late")


def test_manifest_reader_rejects_unknown_schema_version(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="strict manifest",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    journal.manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(AgentRunJournalError, match="invalid"):
        read_agent_run_manifest(journal.manifest_path)
    journal.mark_accepted()
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "accepted"


def test_workspace_creation_failure_leaves_failed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace creation failure must not leave the run ``started``."""

    def fail_workspace(self: Any, forbidden_roots: tuple[Path, ...]) -> Path:
        raise AgentRunnerError("injected workspace failure")

    monkeypatch.setattr(
        runner_module.OpenCodeAgentRunner, "_create_workspace", fail_workspace
    )
    runner = _runner(tmp_path, journal_root=_journal_root(tmp_path))

    with pytest.raises(AgentRunnerError, match="injected workspace failure"):
        runner(_request())

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentRunnerError"
    assert manifest.runner_error.error_message == "injected workspace failure"
    assert manifest.workspace_path is None
    assert manifest.processes == []
    assert manifest.validation.status == "pending"


def test_workspace_failure_preserves_error_when_journaling_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secondary journal failure must not mask the workspace error."""

    def fail_workspace(self: Any, forbidden_roots: tuple[Path, ...]) -> Path:
        raise AgentRunnerError("injected workspace failure")

    def fail_mark_runner_failed(self: Any, error_type: str, message: str) -> None:
        raise AgentRunJournalError("injected journal failure")

    closed: list[Any] = []
    real_close = journal_module.AgentRunJournal.close

    def recording_close(self: Any) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(
        runner_module.OpenCodeAgentRunner, "_create_workspace", fail_workspace
    )
    monkeypatch.setattr(
        journal_module.AgentRunJournal, "mark_runner_failed", fail_mark_runner_failed
    )
    monkeypatch.setattr(journal_module.AgentRunJournal, "close", recording_close)
    runner = _runner(tmp_path, journal_root=_journal_root(tmp_path))

    with pytest.raises(AgentRunnerError, match="injected workspace failure"):
        runner(_request())

    assert len(closed) == 1


def test_never_run_recorder_cannot_accept_or_reject(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="never run",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    with pytest.raises(AgentRunJournalError, match="no main agent process"):
        journal.mark_accepted()
    with pytest.raises(AgentRunJournalError, match="no main agent process"):
        journal.mark_rejected("ChapterSegmentationError", "never ran")


def test_accepted_requires_successful_agent_process(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="agent failed",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.write_chunk("agent", "stdout", b'{"type": "text", "sessionID": "s"}\n')
    journal.write_chunk("agent", "stderr", b"")
    journal.process_finished("agent", 1)
    journal.session_parsed("s", mode="strict")
    journal.record_output("result.json", 2, hashlib.sha256(b"{}").hexdigest())
    journal.output_collection_complete()
    with pytest.raises(AgentRunJournalError, match="did not succeed"):
        journal.mark_accepted()


def test_accepted_requires_session_parsing(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="session not parsed",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.write_chunk("agent", "stdout", b'{"type": "text"}\n')
    journal.write_chunk("agent", "stderr", b"")
    journal.process_finished("agent", 0)
    journal.record_output("result.json", 2, hashlib.sha256(b"{}").hexdigest())
    journal.output_collection_complete()
    with pytest.raises(AgentRunJournalError, match="session parsing"):
        journal.mark_accepted()


def test_accepted_requires_output_collection(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="collection pending",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.write_chunk("agent", "stdout", b'{"type": "text", "sessionID": "s"}\n')
    journal.write_chunk("agent", "stderr", b"")
    journal.process_finished("agent", 0)
    journal.session_parsed("s", mode="strict")
    journal.record_output("result.json", 2, hashlib.sha256(b"{}").hexdigest())
    with pytest.raises(AgentRunJournalError, match="output collection"):
        journal.mark_accepted()


def test_accepted_requires_complete_capture(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="capture truncated",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.write_chunk("agent", "stdout", b'{"type": "text", "sessionID": "s"}\n')
    journal.write_chunk("agent", "stderr", b"")
    journal.capture_interrupted("agent", "AgentExecutionError", "truncated")
    journal.process_finished("agent", 0)
    journal.session_parsed("s", mode="strict")
    journal.record_output("result.json", 2, hashlib.sha256(b"{}").hexdigest())
    journal.output_collection_complete()
    with pytest.raises(AgentRunJournalError, match="capture"):
        journal.mark_accepted()


def test_accepted_requires_strict_session_parsing(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="best-effort session",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.write_chunk("agent", "stdout", b'{"type": "text", "sessionID": "s"}\n')
    journal.write_chunk("agent", "stderr", b"")
    journal.process_finished("agent", 0)
    journal.session_parsed("s", mode="best-effort")
    journal.record_output("result.json", 2, hashlib.sha256(b"{}").hexdigest())
    journal.output_collection_complete()
    with pytest.raises(AgentRunJournalError, match="strict"):
        journal.mark_accepted()


def test_capture_interrupted_requires_stream_capture(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="capture interrupt",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("config-probe", capture_streams=False)
    with pytest.raises(AgentRunJournalError, match="no stream capture"):
        journal.capture_interrupted("config-probe", "AgentExecutionError", "boom")
    with pytest.raises(AgentRunJournalError, match="was not started"):
        journal.capture_interrupted("agent", "AgentExecutionError", "boom")


def test_interrupted_capture_failed_manifest_is_coherent(tmp_path: Path) -> None:
    """A truncated capture with best-effort session parsing stays readable."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="interrupted run",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.write_chunk("agent", "stdout", b'{"type": "text", "sessionID": "s1"}\n')
    journal.capture_interrupted(
        "agent", "AgentExecutionError", "OpenCode timed out after 0.1s"
    )
    journal.process_finished("agent", None)
    journal.session_parsed("s1", mode="best-effort")
    journal.mark_runner_failed("AgentExecutionError", "OpenCode timed out after 0.1s")

    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    assert manifest.validation.status == "pending"
    assert manifest.session_parsed is True
    assert manifest.session_parse_mode == "best-effort"
    assert manifest.session_id == "s1"
    agent = _agent_process(manifest)
    assert agent.capture == "streams"
    assert agent.capture_complete is False
    assert agent.capture_error_type == "AgentExecutionError"
    assert agent.capture_error_message == "OpenCode timed out after 0.1s"
    assert agent.stdout is not None and agent.stderr is not None
    assert agent.result == "error"
    stdout = (journal.manifest_path.parent / agent.stdout.filename).read_bytes()
    assert stdout == b'{"type": "text", "sessionID": "s1"}\n'
    assert agent.stdout.size_bytes == len(stdout)
    assert agent.stdout.sha256 == hashlib.sha256(stdout).hexdigest()


def _track_stream_fds(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Track every agent stream descriptor the journal opens and closes.

    A stream's name mapping is dropped when its descriptor closes, so a
    later reuse of the same fd number (for example the temporary manifest
    inside ``_write_manifest``) can never be mistaken for the stream.
    """
    real_open = journal_module.os.open
    real_close = journal_module.os.close
    tracked: dict[str, Any] = {"opened": set(), "closed": set(), "names": {}}

    def tracking_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.endswith(("stdout.jsonl", "stderr.bin")):
            tracked["opened"].add(descriptor)
            tracked["names"][descriptor] = path
        return descriptor

    def tracking_close(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor in tracked["opened"]:
            tracked["closed"].add(descriptor)
            tracked["names"].pop(descriptor, None)

    monkeypatch.setattr(journal_module.os, "open", tracking_open)
    monkeypatch.setattr(journal_module.os, "close", tracking_close)
    return tracked


def _fail_stream_fsync(monkeypatch: pytest.MonkeyPatch, stream: str) -> dict[str, Any]:
    """Fail fsync for one agent stream file while tracking every stream fd."""
    tracked = _track_stream_fds(monkeypatch)
    real_fsync = journal_module.os.fsync

    def tracking_fsync(descriptor: int) -> None:
        name = tracked["names"].get(descriptor)
        if name is not None and name.endswith(stream):
            raise OSError("injected fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(journal_module.os, "fsync", tracking_fsync)
    return tracked


def _fail_stream_close(monkeypatch: pytest.MonkeyPatch, stream: str) -> dict[str, Any]:
    """Fail close for one agent stream file while tracking every stream fd.

    The descriptor is still released so the run's remaining cleanup stays
    real; the injected OSError exercises the recorded-failure path.
    """
    tracked = _track_stream_fds(monkeypatch)
    real_close = journal_module.os.close

    def tracking_close(descriptor: int) -> None:
        name = tracked["names"].get(descriptor)
        real_close(descriptor)
        if descriptor in tracked["opened"]:
            tracked["closed"].add(descriptor)
            tracked["names"].pop(descriptor, None)
        if name is not None and name.endswith(stream):
            raise OSError("injected close failure")

    monkeypatch.setattr(journal_module.os, "close", tracking_close)
    return tracked


def test_second_stream_fsync_failure_fails_run_and_keeps_readable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream fsync failure on the normal finish path fails the run.

    The first stream's metadata stays recorded, every stream descriptor is
    closed, the capture is marked incomplete with a bounded reason, and the
    persisted manifest stays strictly readable and never accepted.
    """
    tracked = _fail_stream_fsync(monkeypatch, "stderr.bin")
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )

    with pytest.raises(
        AgentRunJournalError, match="cannot sync agent run stream agent/stderr"
    ):
        runner(_request())

    # every stream fd the journal opened was closed despite the failure
    assert tracked["opened"] == tracked["closed"]

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.validation.status == "pending"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentRunJournalError"
    agent = _agent_process(manifest)
    assert agent.capture == "streams"
    assert agent.capture_complete is False
    assert agent.capture_error_type == "AgentRunJournalError"
    assert "cannot sync agent run stream" in agent.capture_error_message
    # the process itself succeeded; only the journal finish failed
    assert agent.result == "success"
    assert agent.returncode == 0
    # the first stream kept its real metadata; the second stream kept its
    # actual accepted bytes (empty here) even though its sync failed
    expected_stdout = b'{"type": "text", "sessionID": "ses_journal_test"}\n'
    assert agent.stdout is not None
    assert agent.stdout.size_bytes == len(expected_stdout)
    assert agent.stdout.sha256 == hashlib.sha256(expected_stdout).hexdigest()
    assert agent.stderr is not None
    assert agent.stderr.size_bytes == 0
    assert agent.stderr.sha256 == hashlib.sha256(b"").hexdigest()
    # no crash-left temporaries and the workspace was still cleaned
    assert sorted(path.name for path in run_dir.iterdir()) == [
        "manifest.json",
        "stderr.bin",
        "stdout.jsonl",
    ]
    assert list((tmp_path / "isolated").iterdir()) == []
    # never accepted anywhere
    assert manifest.validation.status != "accepted"


def test_stream_close_failure_fails_run_and_keeps_readable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream close failure on the normal finish path fails the run."""
    tracked = _fail_stream_close(monkeypatch, "stderr.bin")
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )

    with pytest.raises(
        AgentRunJournalError, match="cannot close agent run stream agent/stderr"
    ):
        runner(_request())

    assert tracked["opened"] == tracked["closed"]
    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.validation.status == "pending"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentRunJournalError"
    agent = _agent_process(manifest)
    assert agent.capture_complete is False
    assert agent.capture_error_type == "AgentRunJournalError"
    assert "cannot close agent run stream" in agent.capture_error_message
    assert agent.result == "success"
    assert list((tmp_path / "isolated").iterdir()) == []


def test_finish_metadata_write_failure_fails_run_and_keeps_readable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest metadata failure inside process_finished fails the run.

    The finish record never claims the process ended, every descriptor is
    released, and the durable manifest stays a strictly readable running
    record that is never accepted.
    """
    tracked = _track_stream_fds(monkeypatch)
    real_write = journal_module.AgentRunJournal._write_manifest

    def failing_write(self: Any) -> None:
        agent = self._processes.get("agent")
        if (
            agent is not None
            and agent["result"] != "not_run"
            and agent["capture_complete"] is not None
        ):
            raise AgentRunJournalError("injected finish metadata write failure")
        real_write(self)

    monkeypatch.setattr(
        journal_module.AgentRunJournal, "_write_manifest", failing_write
    )
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        capture_raw_main_streams=True,
    )

    with pytest.raises(AgentRunJournalError, match="injected finish metadata"):
        runner(_request())

    assert tracked["opened"] == tracked["closed"]
    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status != "accepted"
    assert manifest.validation.status == "pending"
    agent = _agent_process(manifest)
    assert agent.result == "not_run"
    assert agent.capture_complete is None
    assert list((tmp_path / "isolated").iterdir()) == []


def test_segment_stream_sync_failure_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal-path stream sync failure fails the run and never publishes.

    Stage 3 must not write ``chapters.json`` when the run journal's finish
    failed: the runner error propagates through the stage and the manifest
    stays readable and failed, never accepted.
    """
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-ok")
    _fail_stream_fsync(monkeypatch, "stderr.bin")
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
        capture_raw_main_streams=True,
    )

    with pytest.raises(ChapterSegmentationError):
        segment_chapters(content_path, work / "03_chapters", runner)

    # no published artifact
    assert not (work / "03_chapters" / "chapters.json").exists()
    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.validation.status == "pending"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentRunJournalError"


def test_accepted_requires_every_declared_output_collected(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="output missing",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024, "other.json": 512},
    )
    _drive_success(journal)
    with pytest.raises(AgentRunJournalError, match="uncollected"):
        journal.mark_accepted()


def _accepted_journal(tmp_path: Path) -> Any:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="cross-field test",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal, session_id="ses_cross")
    journal.mark_accepted()
    return journal


def _rewrite_manifest(journal: Any, mutate: Any) -> None:
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    mutate(payload)
    journal.manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda payload: payload.update(
                {
                    "runner_error": {
                        "error_type": "AgentRunnerError",
                        "error_message": "late failure",
                    }
                }
            ),
            id="accepted-with-runner-error",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    }
                }
            ),
            id="accepted-with-pending-validation",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "validation": {
                        "status": "rejected",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": "ChapterSegmentationError",
                        "error_message": "late",
                    }
                }
            ),
            id="accepted-with-rejected-validation",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "processes": [
                        process
                        for process in payload["processes"]
                        if process["label"] != "agent"
                    ]
                }
            ),
            id="accepted-without-agent-process",
        ),
        pytest.param(
            lambda payload: next(
                process
                for process in payload["processes"]
                if process["label"] == "agent"
            ).update({"returncode": 1, "result": "nonzero"}),
            id="accepted-with-nonzero-agent",
        ),
        pytest.param(
            lambda payload: next(
                process
                for process in payload["processes"]
                if process["label"] == "agent"
            ).update({"stdout": None}),
            id="accepted-with-incomplete-streams",
        ),
        pytest.param(
            lambda payload: payload.update({"session_parsed": False}),
            id="accepted-without-session-parsing",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "output_collection": {
                        "status": "pending",
                        "error_type": None,
                        "error_message": None,
                    }
                }
            ),
            id="accepted-with-pending-collection",
        ),
        pytest.param(
            lambda payload: payload["outputs"][0].update(
                {
                    "status": "uncollected",
                    "size_bytes": None,
                    "sha256": None,
                }
            ),
            id="accepted-with-uncollected-output",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "failed",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": None,
                }
            ),
            id="failed-without-runner-error",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "failed",
                    "validation": {
                        "status": "rejected",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": "ChapterSegmentationError",
                        "error_message": "late",
                    },
                }
            ),
            id="failed-with-semantic-validation",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "started",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": {
                        "error_type": "AgentRunnerError",
                        "error_message": "late",
                    },
                }
            ),
            id="started-with-runner-error",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "validation": {
                        "status": "accepted",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    }
                }
            ),
            id="accepted-validation-without-publication",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "validation": {
                        "status": "rejected",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    }
                }
            ),
            id="rejected-validation-without-error",
        ),
        pytest.param(
            lambda payload: next(
                process
                for process in payload["processes"]
                if process["label"] == "agent"
            ).update({"capture": "disabled", "stdout": None, "stderr": None}),
            id="accepted-with-disabled-capture-and-stale-completeness",
        ),
        pytest.param(
            lambda payload: next(
                process
                for process in payload["processes"]
                if process["label"] == "agent"
            ).update({"capture_complete": None}),
            id="accepted-with-pending-stream-capture",
        ),
        pytest.param(
            lambda payload: next(
                process
                for process in payload["processes"]
                if process["label"] == "agent"
            ).update(
                {
                    "capture_complete": False,
                    "capture_error_type": "AgentExecutionError",
                    "capture_error_message": "truncated stream",
                }
            ),
            id="accepted-with-truncated-capture",
        ),
        pytest.param(
            lambda payload: payload.update({"session_parse_mode": "best-effort"}),
            id="accepted-with-best-effort-parse",
        ),
        pytest.param(
            lambda payload: payload.update({"session_parse_mode": None}),
            id="accepted-without-parse-mode",
        ),
        pytest.param(
            lambda payload: payload.update(
                {"session_parsed": False, "session_parse_mode": "strict"}
            ),
            id="parse-mode-without-completed-parsing",
        ),
    ],
)
def test_manifest_reader_rejects_incoherent_cross_field_combinations(
    tmp_path: Path, mutate: Any
) -> None:
    journal = _accepted_journal(tmp_path)
    _rewrite_manifest(journal, mutate)
    with pytest.raises(AgentRunJournalError, match="invalid"):
        read_agent_run_manifest(journal.manifest_path)


def _accepted_intent_payload() -> dict[str, Any]:
    return {
        "status": "completed",
        "target_status": "accepted",
        "attempted_at_utc": "2026-01-01T00:00:00+00:00",
        "completed_at_utc": "2026-01-01T00:00:01+00:00",
        "validation": {
            "status": "accepted",
            "published": True,
            "published_at_utc": "2026-01-01T00:00:01+00:00",
            "error_type": None,
            "error_message": None,
        },
        "runner_error": None,
    }


def _rejected_validation_payload() -> dict[str, Any]:
    return {
        "status": "rejected",
        "published": False,
        "published_at_utc": None,
        "error_type": "ChapterSegmentationError",
        "error_message": "late",
    }


def _pending_accepted_intent_payload() -> dict[str, Any]:
    intent = _accepted_intent_payload()
    intent["status"] = "pending"
    intent["completed_at_utc"] = None
    return intent


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "started",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": None,
                    "finalization": _accepted_intent_payload(),
                }
            ),
            id="started-with-completed-intent",
        ),
        pytest.param(
            lambda payload: payload.update(
                {"finalization": _pending_accepted_intent_payload()}
            ),
            id="accepted-with-pending-intent",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "finalization": {
                        "status": "completed",
                        "target_status": "rejected",
                        "attempted_at_utc": "2026-01-01T00:00:00+00:00",
                        "completed_at_utc": "2026-01-01T00:00:01+00:00",
                        "validation": _rejected_validation_payload(),
                        "runner_error": None,
                    }
                }
            ),
            id="accepted-with-mismatched-completed-target",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "failed",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": {
                        "error_type": "AgentExecutionError",
                        "error_message": "boom",
                    },
                    "finalization": _pending_accepted_intent_payload(),
                }
            ),
            id="failed-with-pending-intent",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "started",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": None,
                    "finalization": {
                        "status": "pending",
                        "target_status": "accepted",
                        "attempted_at_utc": "2026-01-01T00:00:00+00:00",
                        "completed_at_utc": "2026-01-01T00:00:01+00:00",
                        "validation": {
                            "status": "accepted",
                            "published": True,
                            "published_at_utc": "2026-01-01T00:00:00+00:00",
                            "error_type": None,
                            "error_message": None,
                        },
                        "runner_error": None,
                    },
                }
            ),
            id="started-with-completed-pending-intent",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "started",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": None,
                    "finalization": {
                        "status": "pending",
                        "target_status": "accepted",
                        "attempted_at_utc": "2026-01-01T00:00:00+00:00",
                        "completed_at_utc": None,
                        "validation": _rejected_validation_payload(),
                        "runner_error": None,
                    },
                }
            ),
            id="pending-intent-with-mismatched-validation",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "finalization": {
                        "status": "completed",
                        "target_status": "accepted",
                        "attempted_at_utc": "2026-01-01T00:00:00+00:00",
                        "completed_at_utc": "2026-01-01T00:00:01+00:00",
                        "validation": {
                            "status": "accepted",
                            "published": True,
                            "published_at_utc": "1999-01-01T00:00:00+00:00",
                            "error_type": None,
                            "error_message": None,
                        },
                        "runner_error": None,
                    }
                }
            ),
            id="accepted-with-divergent-intent-validation",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "failed",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": {
                        "error_type": "AgentExecutionError",
                        "error_message": "boom",
                    },
                    "finalization": {
                        "status": "completed",
                        "target_status": "failed",
                        "attempted_at_utc": "2026-01-01T00:00:00+00:00",
                        "completed_at_utc": "2026-01-01T00:00:01+00:00",
                        "validation": None,
                        "runner_error": {
                            "error_type": "AgentExecutionError",
                            "error_message": "different boom",
                        },
                    },
                }
            ),
            id="failed-with-divergent-intent-runner-error",
        ),
        pytest.param(
            lambda payload: payload.update(
                {
                    "final_status": "failed",
                    "validation": {
                        "status": "pending",
                        "published": False,
                        "published_at_utc": None,
                        "error_type": None,
                        "error_message": None,
                    },
                    "runner_error": {
                        "error_type": "AgentExecutionError",
                        "error_message": "boom",
                    },
                    "finalization": {
                        "status": "completed",
                        "target_status": "failed",
                        "attempted_at_utc": "2026-01-01T00:00:00+00:00",
                        "completed_at_utc": None,
                        "validation": None,
                        "runner_error": {
                            "error_type": "AgentExecutionError",
                            "error_message": "boom",
                        },
                    },
                }
            ),
            id="completed-intent-without-completion-timestamp",
        ),
    ],
)
def test_manifest_reader_rejects_incoherent_finalization_intents(
    tmp_path: Path, mutate: Any
) -> None:
    journal = _accepted_journal(tmp_path)
    _rewrite_manifest(journal, mutate)
    with pytest.raises(AgentRunJournalError, match="invalid"):
        read_agent_run_manifest(journal.manifest_path)


def test_terminal_manifest_with_cleared_intent_stays_valid(tmp_path: Path) -> None:
    """A terminal manifest whose intent was cleared (or predates intents)."""
    journal = _accepted_journal(tmp_path)
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    del payload["finalization"]
    journal.manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "accepted"
    assert manifest.finalization is None


def test_pending_intent_manifest_is_strictly_readable(tmp_path: Path) -> None:
    """A crash-left pending intent stays readable and clearly non-terminal."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="pending intent",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    payload["finalization"] = _pending_accepted_intent_payload()
    payload["ended_at_utc"] = "2026-01-01T00:00:00+00:00"
    journal.manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "started"
    assert manifest.finalization is not None
    assert manifest.finalization.status == "pending"
    assert manifest.finalization.target_status == "accepted"
    assert manifest.finalization.validation is not None
    assert manifest.finalization.validation.status == "accepted"
    assert manifest.finalization.completed_at_utc is None
    journal.close()


def test_finalize_manifest_write_failure_closes_all_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="write failure",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)

    def failing_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected manifest replace failure")

    monkeypatch.setattr(journal_module.os, "replace", failing_replace)
    with pytest.raises(
        AgentRunJournalError, match="cannot update the agent run manifest"
    ):
        journal.mark_accepted()

    # finalization is not claimed, every descriptor is released, and close
    # stays idempotent; the persisted manifest never claims acceptance and
    # carries no intent because the intent write itself failed.
    assert journal._finalized is False
    assert journal._streams == {}
    assert journal._run_fd is None
    assert journal._root_fd is None
    journal.close()
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status != "accepted"
    assert manifest.validation.status != "accepted"
    assert manifest.finalization is None


def _fail_second_finalize_write(journal: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the terminal manifest write inside the next finalization fail.

    The first ``_write_manifest`` call after installation is the durable
    pending-intent write; the second is the terminal write.  The flaky second
    call raises so the terminal transition cannot complete.
    """
    calls = {"count": 0}
    real_write = journal_module.AgentRunJournal._write_manifest

    def flaky_write(self: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise AgentRunJournalError("injected terminal manifest write failure")
        real_write(self)

    monkeypatch.setattr(journal_module.AgentRunJournal, "_write_manifest", flaky_write)


@pytest.mark.parametrize(
    "finalize, expected_target",
    [
        pytest.param(
            lambda journal: journal.mark_accepted(),
            "accepted",
            id="accepted",
        ),
        pytest.param(
            lambda journal: journal.mark_rejected(
                "ChapterSegmentationError", "semantic rejection"
            ),
            "rejected",
            id="rejected",
        ),
        pytest.param(
            lambda journal: journal.mark_runner_failed(
                "AgentExecutionError", "runner failure"
            ),
            "failed",
            id="failed",
        ),
    ],
)
def test_terminal_write_failure_leaves_pending_finalization_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finalize: Any, expected_target: str
) -> None:
    """A failed terminal write must leave a strictly readable pending intent.

    The last durable manifest stays non-terminal (``started`` with pending
    validation) but exposes the finalization intent with its target status,
    attempt timestamp, and bounded transition payload, so it never reads like
    an ordinary running run.
    """
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="terminal write failure",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)
    _fail_second_finalize_write(journal, monkeypatch)

    with pytest.raises(AgentRunJournalError, match="injected terminal"):
        finalize(journal)

    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "started"
    assert manifest.validation.status == "pending"
    assert manifest.runner_error is None
    assert manifest.finalization is not None
    assert manifest.finalization.status == "pending"
    assert manifest.finalization.target_status == expected_target
    assert manifest.finalization.attempted_at_utc
    assert manifest.finalization.completed_at_utc is None
    if expected_target == "accepted":
        assert manifest.finalization.validation is not None
        assert manifest.finalization.validation.status == "accepted"
        assert manifest.finalization.validation.published is True
        assert manifest.finalization.validation.published_at_utc is not None
        assert manifest.finalization.runner_error is None
    elif expected_target == "rejected":
        assert manifest.finalization.validation is not None
        assert manifest.finalization.validation.status == "rejected"
        assert manifest.finalization.validation.published is False
        assert manifest.finalization.validation.error_type == "ChapterSegmentationError"
        assert manifest.finalization.validation.error_message == "semantic rejection"
        assert manifest.finalization.runner_error is None
    else:
        assert manifest.finalization.validation is None
        assert manifest.finalization.runner_error is not None
        assert manifest.finalization.runner_error.error_type == "AgentExecutionError"
        assert manifest.finalization.runner_error.error_message == "runner failure"

    # the record is closed on every failure path and never claims completion
    assert journal._finalized is False
    assert journal._streams == {}
    assert journal._run_fd is None
    assert journal._root_fd is None
    journal.close()


def test_semantic_rejection_preserves_error_when_journaling_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-bad")

    def fail_mark_rejected(self: Any, error_type: str, message: str) -> None:
        raise AgentRunJournalError("injected rejection failure")

    monkeypatch.setattr(
        journal_module.AgentRunJournal, "mark_rejected", fail_mark_rejected
    )
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
    )
    with pytest.raises(ChapterSegmentationError) as caught:
        segment_chapters(content_path, work / "03_chapters", runner)
    notes = getattr(caught.value, "__notes__", [])
    assert any("journal rejection also failed" in note for note in notes)


def test_publication_success_with_failed_accepted_mark_never_claims_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-ok")

    def fail_mark_accepted(self: Any) -> None:
        raise AgentRunJournalError("injected accepted-mark failure")

    monkeypatch.setattr(
        journal_module.AgentRunJournal, "mark_accepted", fail_mark_accepted
    )
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
    )
    with pytest.raises(AgentRunJournalError, match="injected accepted-mark failure"):
        segment_chapters(content_path, work / "03_chapters", runner)

    # the artifact was published, but the durable record never claims accepted
    assert (work / "03_chapters" / "chapters.json").is_file()
    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status != "accepted"
    assert manifest.validation.status != "accepted"


def test_publication_success_with_failed_terminal_write_keeps_pending_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publication success with a failed accepted mark keeps an explicit intent.

    The artifact is already published when the terminal accepted write fails;
    the durable manifest must then stay non-terminal while exposing the
    pending accepted intent, so a later reader sees exactly what happened:
    publication completed, the accepted marking did not.
    """
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-ok")

    real_mark_accepted = journal_module.AgentRunJournal.mark_accepted

    def flaky_mark_accepted(self: Any) -> None:
        calls = {"count": 0}
        real_write = journal_module.AgentRunJournal._write_manifest

        def flaky_write(self_: Any) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise AgentRunJournalError("injected terminal accepted write failure")
            real_write(self_)

        monkeypatch.setattr(
            journal_module.AgentRunJournal, "_write_manifest", flaky_write
        )
        real_mark_accepted(self)

    monkeypatch.setattr(
        journal_module.AgentRunJournal, "mark_accepted", flaky_mark_accepted
    )
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
    )
    with pytest.raises(AgentRunJournalError, match="injected terminal"):
        segment_chapters(content_path, work / "03_chapters", runner)

    # the artifact was published before the accepted marking was attempted
    assert (work / "03_chapters" / "chapters.json").is_file()
    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "started"
    assert manifest.validation.status == "pending"
    assert manifest.finalization is not None
    assert manifest.finalization.status == "pending"
    assert manifest.finalization.target_status == "accepted"
    assert manifest.finalization.validation is not None
    assert manifest.finalization.validation.status == "accepted"
    assert manifest.finalization.validation.published is True
    assert manifest.finalization.validation.published_at_utc is not None


def test_runner_error_preserved_when_failure_mark_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_mark_runner_failed(self: Any, error_type: str, message: str) -> None:
        raise AgentRunJournalError("injected failure-mark failure")

    monkeypatch.setattr(
        journal_module.AgentRunJournal,
        "mark_runner_failed",
        fail_mark_runner_failed,
    )
    runner = _runner(tmp_path, journal_root=_journal_root(tmp_path))

    with pytest.raises(AgentExecutionError) as caught:
        runner(_request("exit-failure"))
    assert caught.value.returncode == 9
    notes = getattr(caught.value, "__notes__", [])
    assert any("journaling also failed" in note for note in notes)


# -- directory permissions and ownership --------------------------------------


def test_existing_journal_root_mode_is_corrected_ancestors_untouched(
    tmp_path: Path,
) -> None:
    """A pre-existing journal root is forced to 0700; ancestors stay as-is."""
    root = tmp_path / "book" / "logs" / "agent-runs"
    root.mkdir(parents=True)
    ancestor = root.parent
    os.chmod(root, 0o755)
    os.chmod(ancestor, 0o755)
    journal = create_agent_run_journal(
        root,
        title="existing root",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    # unrelated ancestor directories are never required to be 0700
    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o755
    run_dir = journal.manifest_path.parent
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.manifest_path.stat().st_mode) == 0o600
    journal.close()


def test_journal_root_with_wrong_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal root owned by someone else is rejected, never chmodded."""
    root = tmp_path / "book" / "logs" / "agent-runs"
    root.mkdir(parents=True)
    original_mode = stat.S_IMODE(root.stat().st_mode)
    # Any non-owner euid fails the ownership check; the real file owner is
    # never equal to -1.
    monkeypatch.setattr(journal_module.os, "geteuid", lambda: -1)
    with pytest.raises(AgentRunJournalError, match="not owned by the effective user"):
        create_agent_run_journal(
            root,
            title="foreign root",
            identity=book_editor_identity(),
            execution_profile="development",
            declared_outputs={"result.json": 1024},
        )
    # the foreign root was never touched
    assert stat.S_IMODE(root.stat().st_mode) == original_mode
    assert sorted(path.name for path in root.iterdir()) == []


def test_run_directory_and_files_have_restrictive_modes(tmp_path: Path) -> None:
    """Run directory 0700, every journal-created file 0600, no temp litter."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="mode check",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=True)
    journal.write_chunk("agent", "stdout", b'{"type": "text", "sessionID": "s"}\n')
    journal.write_chunk("agent", "stderr", b"warn")
    run_dir = journal.manifest_path.parent
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    for name in ("manifest.json", "stdout.jsonl", "stderr.bin"):
        assert stat.S_IMODE((run_dir / name).stat().st_mode) == 0o600, name
    journal.close()
    assert sorted(path.name for path in run_dir.iterdir()) == [
        "manifest.json",
        "stderr.bin",
        "stdout.jsonl",
    ]


# -- strict reader: stream filenames and run ids -------------------------------


def _agent_stdout_mutation(filename: str) -> Any:
    def mutate(payload: dict[str, Any]) -> None:
        next(
            process for process in payload["processes"] if process["label"] == "agent"
        )["stdout"]["filename"] = filename

    return mutate


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("../escape.jsonl", id="parent-dot-component"),
        pytest.param("sub/stdout.jsonl", id="nested-separator"),
        pytest.param("/absolute.jsonl", id="absolute-path"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dotdot"),
        pytest.param("stdout.bin", id="unknown-stream-shape"),
        pytest.param("unknown.jsonl", id="unknown-filename"),
        pytest.param("config-probe.stderr.jsonl", id="unknown-label-stream-shape"),
        pytest.param("stdout.jsonl/..", id="trailing-separator"),
    ],
)
def test_manifest_reader_rejects_unsafe_stream_filenames(
    tmp_path: Path, filename: str
) -> None:
    journal = _accepted_journal(tmp_path)
    _rewrite_manifest(journal, _agent_stdout_mutation(filename))
    with pytest.raises(AgentRunJournalError, match="invalid"):
        read_agent_run_manifest(journal.manifest_path)


def test_manifest_reader_rejects_mismatched_stream_filename(tmp_path: Path) -> None:
    """A valid name from another label is rejected for this process."""
    journal = _accepted_journal(tmp_path)
    _rewrite_manifest(journal, _agent_stdout_mutation("config-probe.stdout.jsonl"))
    with pytest.raises(AgentRunJournalError, match="invalid"):
        read_agent_run_manifest(journal.manifest_path)


@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("../escape", id="parent-dot-component"),
        pytest.param("2026-08-03-120000-0123456789abcdef", id="dashed-date"),
        pytest.param("20260803-120000-0123456789ABCDEF", id="uppercase-hex"),
        pytest.param("202608031-120000-0123456789abcdef", id="long-date"),
        pytest.param("20260803-120000-0123456789abcde", id="short-token"),
        pytest.param("abc", id="short"),
        pytest.param("", id="empty"),
    ],
)
def test_manifest_reader_rejects_invalid_run_ids(tmp_path: Path, run_id: str) -> None:
    journal = _accepted_journal(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        payload["run_id"] = run_id

    _rewrite_manifest(journal, mutate)
    with pytest.raises(AgentRunJournalError, match="invalid"):
        read_agent_run_manifest(journal.manifest_path)


# -- symlink and hardlink defenses --------------------------------------------


def test_manifest_target_symlink_is_rejected(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="symlink target",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=False)
    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    journal.manifest_path.unlink()
    journal.manifest_path.symlink_to(victim)
    with pytest.raises(AgentRunJournalError, match="symlink"):
        journal.process_pid("agent", 1002)
    # the symlink was never followed or replaced
    assert journal.manifest_path.is_symlink()
    assert victim.read_text(encoding="utf-8") == "{}"
    journal.close()


def test_manifest_target_hardlink_is_replaced_atomically(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="hardlink target",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.process_started("agent", capture_streams=False)
    extra = journal.manifest_path.with_name("manifest-extra.json")
    os.link(journal.manifest_path, extra)
    old_inode = journal.manifest_path.stat().st_ino
    old_bytes = journal.manifest_path.read_bytes()

    # A hard-linked old manifest does not block the atomic directory-entry
    # replacement; the external hard link keeps the old inode and snapshot.
    journal.process_pid("agent", 1002)

    assert journal.manifest_path.stat().st_ino != old_inode
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.processes[0].pid == 1002
    assert extra.stat().st_ino == old_inode
    assert extra.read_bytes() == old_bytes
    os.unlink(extra)
    journal.close()


def test_stream_placeholder_symlink_and_hardlink_are_rejected(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="stream placeholder",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    run_dir = journal.manifest_path.parent
    other = tmp_path / "other.bin"
    other.write_bytes(b"x")
    (run_dir / "stdout.jsonl").symlink_to(other)
    with pytest.raises(AgentRunJournalError, match="cannot create agent run stream"):
        journal.process_started("agent", capture_streams=True)
    assert (run_dir / "stdout.jsonl").is_symlink()
    assert not (run_dir / "stderr.bin").exists()

    (run_dir / "stdout.jsonl").unlink()
    os.link(other, run_dir / "stdout.jsonl")
    with pytest.raises(AgentRunJournalError, match="cannot create agent run stream"):
        journal.process_started("agent", capture_streams=True)
    (run_dir / "stdout.jsonl").unlink()
    journal.close()


# -- initialization failure cleanup -------------------------------------------


def test_initial_manifest_write_failure_leaves_no_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed initial manifest write removes the reserved run directory."""
    root = tmp_path / "logs" / "agent-runs"

    def failing_write(self: Any) -> None:
        # simulate a crash-left temporary manifest next to the initial one
        placeholder = self.manifest_path.parent / (
            f".manifest.json.{secrets.token_hex(8)}.tmp"
        )
        placeholder.write_bytes(b"partial")
        raise AgentRunJournalError("injected initial manifest write failure")

    monkeypatch.setattr(
        journal_module.AgentRunJournal, "_write_manifest", failing_write
    )
    with pytest.raises(AgentRunJournalError, match="injected initial manifest"):
        create_agent_run_journal(
            root,
            title="init failure",
            identity=book_editor_identity(),
            execution_profile="development",
            declared_outputs={"result.json": 1024},
        )
    # no orphan run directory, no temporary manifest, no manifest survives
    assert root.is_dir()
    assert sorted(root.iterdir()) == []


def test_initialization_cleanup_failure_preserves_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup failure becomes a bounded note on the primary error."""
    root = tmp_path / "logs" / "agent-runs"
    monkeypatch.setattr(
        journal_module,
        "_new_run_id",
        lambda: "20260803-120000-0123456789abcdef",
    )

    def failing_write(self: Any) -> None:
        # an unknown file must keep the run directory from being removed
        (self.manifest_path.parent / "unexpected.bin").write_bytes(b"x")
        raise AgentRunJournalError("injected initial manifest write failure")

    monkeypatch.setattr(
        journal_module.AgentRunJournal, "_write_manifest", failing_write
    )
    with pytest.raises(
        AgentRunJournalError, match="injected initial manifest"
    ) as caught:
        create_agent_run_journal(
            root,
            title="init failure",
            identity=book_editor_identity(),
            execution_profile="development",
            declared_outputs={"result.json": 1024},
        )
    notes = getattr(caught.value, "__notes__", [])
    assert any("cleanup also failed" in note for note in notes)
    # the degraded state is visible: the unknown file blocks removal
    orphan = root / "20260803-120000-0123456789abcdef"
    assert orphan.is_dir()
    assert (orphan / "unexpected.bin").is_file()


# -- workspace disposition ------------------------------------------------------


def test_workspace_disposition_starts_pending_and_resolves(tmp_path: Path) -> None:
    """The disposition lifecycle: none, pending, then deleted/retained."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="disposition lifecycle",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    assert payload["workspace_path"] is None
    assert payload["workspace_disposition"] is None

    recorded_workspace = tmp_path / "recorded-workspace"
    journal.record_workspace(recorded_workspace)
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    assert payload["workspace_path"] == str(recorded_workspace)
    assert payload["workspace_disposition"] == {
        "disposition": "pending",
        "deleted_at_utc": None,
        "retained_at_utc": None,
        "reason": None,
    }

    journal.record_workspace_disposition("deleted")
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    assert payload["workspace_disposition"]["disposition"] == "deleted"
    assert payload["workspace_disposition"]["deleted_at_utc"] is not None
    assert payload["workspace_disposition"]["retained_at_utc"] is None
    assert payload["workspace_disposition"]["reason"] is None
    journal.close()

    retained = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="disposition retained",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    retained.record_workspace(recorded_workspace)
    retained.record_workspace_disposition(
        "retained", reason="manual cleanup required after debugging"
    )
    payload = json.loads(retained.manifest_path.read_text(encoding="utf-8"))
    assert payload["workspace_disposition"]["disposition"] == "retained"
    assert payload["workspace_disposition"]["retained_at_utc"] is not None
    assert payload["workspace_disposition"]["deleted_at_utc"] is None
    assert (
        payload["workspace_disposition"]["reason"]
        == "manual cleanup required after debugging"
    )
    retained.close()


def test_workspace_disposition_reason_is_bounded(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="bounded disposition reason",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.record_workspace(tmp_path / "recorded-workspace")
    journal.record_workspace_disposition("retained", reason="x" * 5000)
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    assert len(payload["workspace_disposition"]["reason"]) == 2000
    journal.close()


def test_workspace_disposition_requires_recorded_workspace(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="no workspace",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    with pytest.raises(AgentRunJournalError, match="no workspace"):
        journal.record_workspace_disposition("deleted")
    journal.close()


def test_workspace_disposition_after_finalize_raises(tmp_path: Path) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="late disposition",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)
    journal.mark_accepted()
    with pytest.raises(AgentRunJournalError, match="already finalized"):
        journal.record_workspace_disposition("deleted")


@pytest.mark.parametrize(
    "disposition",
    ["sometimes", "RETAINED", "Deleted", "", None, 5, ["deleted"]],
)
def test_workspace_disposition_rejects_invalid_values(
    tmp_path: Path, disposition: Any
) -> None:
    """Only deleted and retained are accepted; nothing else maps to retained."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="invalid disposition",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.record_workspace(tmp_path / "recorded-workspace")
    with pytest.raises(AgentRunJournalError, match="invalid workspace disposition"):
        journal.record_workspace_disposition(disposition)
    # the pending state survives the rejection
    payload = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
    assert payload["workspace_disposition"]["disposition"] == "pending"
    journal.close()


@pytest.mark.parametrize("reason", [123, "", "   ", ["bounded"]])
def test_workspace_disposition_rejects_incoherent_reasons(
    tmp_path: Path, reason: Any
) -> None:
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="invalid disposition reason",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    journal.record_workspace(tmp_path / "recorded-workspace")
    with pytest.raises(AgentRunJournalError, match="reason"):
        journal.record_workspace_disposition("retained", reason=reason)
    journal.close()


def test_unresolved_disposition_symlink_is_recorded_deleted(tmp_path: Path) -> None:
    """A symlink at the workspace path is not the original directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    workspace.rmdir()
    workspace.symlink_to(decoy, target_is_directory=True)
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="symlink disposition",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal, session_id="ses_symlink", workspace=str(workspace))
    journal.mark_accepted()
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "accepted"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert manifest.workspace_disposition.reason is not None
    assert "no longer the original directory" in manifest.workspace_disposition.reason


def test_unresolved_disposition_plain_file_is_recorded_deleted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.write_text("not a directory", encoding="utf-8")
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="file disposition",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal, session_id="ses_file", workspace=str(workspace))
    journal.mark_accepted()
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"


def test_unresolved_disposition_is_resolved_conservatively_at_finalize(
    tmp_path: Path,
) -> None:
    """A finalize with a pending disposition never produces an invalid manifest.

    A workspace that still exists is recorded as retained; a vanished one as
    deleted; a run without a recorded workspace carries no disposition.
    """
    kept = tmp_path / "kept-workspace"
    kept.mkdir()
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="unresolved kept",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal, session_id="ses_kept", workspace=str(kept))
    journal.mark_accepted()
    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "accepted"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.retained_at_utc is not None
    assert manifest.workspace_disposition.reason is not None
    assert "conservatively" in manifest.workspace_disposition.reason

    vanished = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="unresolved vanished",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    vanished.record_workspace(tmp_path / "never-created-workspace")
    _drive_success(vanished, session_id="ses_vanished")
    vanished.mark_rejected("ChapterSegmentationError", "semantic rejection")
    manifest = read_agent_run_manifest(vanished.manifest_path)
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert manifest.workspace_disposition.deleted_at_utc is not None

    none_ws = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="unresolved none",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    none_ws.mark_runner_failed("AgentRunnerError", "no workspace ever existed")
    manifest = read_agent_run_manifest(none_ws.manifest_path)
    assert manifest.workspace_path is None
    assert manifest.workspace_disposition is None


def test_unresolved_disposition_at_finalize_never_writes_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending-intent write already carries the resolved disposition."""
    journal = create_agent_run_journal(
        tmp_path / "logs" / "agent-runs",
        title="pending intent disposition",
        identity=book_editor_identity(),
        execution_profile="development",
        declared_outputs={"result.json": 1024},
    )
    _drive_success(journal)
    _fail_second_finalize_write(journal, monkeypatch)

    with pytest.raises(AgentRunJournalError, match="injected terminal"):
        journal.mark_accepted()

    manifest = read_agent_run_manifest(journal.manifest_path)
    assert manifest.final_status == "started"
    assert manifest.finalization is not None
    assert manifest.finalization.status == "pending"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition != "pending"
    journal.close()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda payload: payload["workspace_disposition"].update(
                {"disposition": "pending"}
            ),
            id="terminal-with-pending-disposition",
        ),
        pytest.param(
            lambda payload: payload["workspace_disposition"].update(
                {"disposition": "retained"}
            ),
            id="retained-without-timestamp",
        ),
        pytest.param(
            lambda payload: payload["workspace_disposition"].update(
                {"deleted_at_utc": None}
            ),
            id="deleted-without-timestamp",
        ),
        pytest.param(
            lambda payload: payload["workspace_disposition"].update(
                {
                    "disposition": "retained",
                    "retained_at_utc": "2026-01-01T00:00:00+00:00",
                    "deleted_at_utc": "2026-01-01T00:00:00+00:00",
                }
            ),
            id="retained-with-deleted-timestamp",
        ),
        pytest.param(
            lambda payload: payload["workspace_disposition"].update(
                {
                    "disposition": "pending",
                    "retained_at_utc": "2026-01-01T00:00:00+00:00",
                }
            ),
            id="pending-with-timestamp",
        ),
        pytest.param(
            lambda payload: payload.update({"workspace_path": None}),
            id="disposition-without-path",
        ),
        pytest.param(
            lambda payload: payload.pop("workspace_disposition"),
            id="path-without-disposition",
        ),
    ],
)
def test_manifest_reader_rejects_incoherent_workspace_dispositions(
    tmp_path: Path, mutate: Any
) -> None:
    journal = _accepted_journal(tmp_path)
    _rewrite_manifest(journal, mutate)
    with pytest.raises(AgentRunJournalError, match="invalid"):
        read_agent_run_manifest(journal.manifest_path)


# -- retention policy through the production stages -----------------------------


def test_segment_on_failure_rejection_retains_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-bad")
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
        workspace_retention="on-failure",
    )
    with pytest.raises(ChapterSegmentationError):
        segment_chapters(content_path, work / "03_chapters", runner)

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "rejected"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.retained_at_utc is not None
    assert manifest.workspace_disposition.reason is not None
    assert "on-failure" in manifest.workspace_disposition.reason
    assert Path(manifest.workspace_path).is_dir()
    assert not (work / "03_chapters" / "chapters.json").exists()


def test_segment_on_failure_acceptance_deletes_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-ok")
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
        workspace_retention="on-failure",
    )
    output_path = segment_chapters(content_path, work / "03_chapters", runner)
    assert output_path == work / "03_chapters" / "chapters.json"
    assert output_path.is_file()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "accepted"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert manifest.workspace_disposition.deleted_at_utc is not None
    assert not Path(manifest.workspace_path).exists()
    assert list((tmp_path / "isolated").iterdir()) == []


def test_segment_always_acceptance_retains_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "segmentation-ok")
    work = tmp_path / "book"
    content_path = _seg_content(work)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="restricted",
        workspace_retention="always",
    )
    output_path = segment_chapters(content_path, work / "03_chapters", runner)
    assert output_path.is_file()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "accepted"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert Path(manifest.workspace_path).is_dir()
    assert manifest.workspace_disposition.reason is not None
    assert "always" in manifest.workspace_disposition.reason


def test_revision_on_failure_acceptance_deletes_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "revision-ok")
    workspace = _workspace(tmp_path)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="development",
        workspace_retention="on-failure",
    )
    chapter_dir = workspace / "chapters/0002"
    corrected = revise_chapter(chapter_dir, runner)
    assert corrected == chapter_dir / "corrected.html"
    assert (chapter_dir / "revision.json").is_file()

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "accepted"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert manifest.workspace_disposition.deleted_at_utc is not None
    assert not Path(manifest.workspace_path).exists()


def test_revision_on_failure_rejection_retains_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "revision-bad")
    workspace = _workspace(tmp_path)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="development",
        workspace_retention="on-failure",
    )
    chapter_dir = workspace / "chapters/0002"
    with pytest.raises(ChapterRevisionError):
        revise_chapter(chapter_dir, runner)

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "rejected"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert Path(manifest.workspace_path).is_dir()


def test_revision_on_failure_runner_failure_retains_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "exit-failure")
    workspace = _workspace(tmp_path)
    runner = _runner(
        tmp_path,
        journal_root=_journal_root(tmp_path),
        execution_profile="development",
        workspace_retention="on-failure",
    )
    chapter_dir = workspace / "chapters/0002"
    with pytest.raises(ChapterRevisionError, match="agent failed"):
        revise_chapter(chapter_dir, runner)

    run_dir = _single_run_dir(_journal_root(tmp_path))
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert Path(manifest.workspace_path).is_dir()
