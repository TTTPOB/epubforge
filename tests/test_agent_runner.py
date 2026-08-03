"""Security and process-boundary tests for the OpenCode agent runner."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any

import pytest

from epubforge import agent_journal as journal_module
from epubforge import agent_runner as runner_module
from epubforge.agent_journal import AgentRunJournalError, read_agent_run_manifest
from epubforge.agent_runner import (
    AgentCleanupError,
    AgentExecutionError,
    AgentRunRequest,
    AgentRunnerError,
    BOOK_EDITOR_PROMPT,
    OpenCodeAgentRunner,
)


_HELPER = r"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

mode = sys.argv[-1]
workspace = (
    Path(sys.argv[sys.argv.index("--dir") + 1])
    if "--dir" in sys.argv
    else None
)

def config_from_sources():
    content = os.environ.get("OPENCODE_CONFIG_CONTENT")
    if content is not None:
        return json.loads(content)

    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_PATH"):
        source = os.environ.get(key)
        if source:
            return json.loads(Path(source).read_text(encoding="utf-8"))

    config_dir = os.environ.get("OPENCODE_CONFIG_DIR")
    if config_dir:
        for name in ("opencode.json", "opencode.jsonc"):
            config_path = Path(config_dir) / name
            if config_path.exists():
                return json.loads(config_path.read_text(encoding="utf-8"))

    return json.loads(os.environ.get("EPUBFORGE_TEST_DEBUG_CONFIG", "{}"))


if sys.argv[1:4] == ["debug", "config", "--pure"]:
    debug_mode = os.environ.get("EPUBFORGE_TEST_DEBUG_MODE")
    if debug_mode == "timeout":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    if debug_mode == "large-output":
        os.write(sys.stdout.fileno(), b"x" * (4 * 1024 * 1024 + 1))
        raise SystemExit(0)
    config = config_from_sources()
    print(json.dumps(config))
elif mode == "inspect":
    assert workspace is not None
    config_path = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    payload = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "workspace_mode": oct(workspace.stat().st_mode & 0o777),
        "task_mode": oct((workspace / "TASK.md").stat().st_mode & 0o777),
        "task": (workspace / "TASK.md").read_text(encoding="utf-8"),
        "flags": {
            key: os.environ.get(key)
            for key in (
                "OPENCODE_DISABLE_PROJECT_CONFIG",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS",
                "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
            )
        },
        "config": config,
        "config_mode": oct(config_path.stat().st_mode & 0o777),
        "permission_env": json.loads(os.environ["OPENCODE_PERMISSION"]),
        "files": sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file()
        ),
    }
    (workspace / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps({"type": "text", "sessionID": "ses_runner_test"}))
elif mode == "config-probe":
    assert workspace is not None
    config_path = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config_home = Path(os.environ["XDG_CONFIG_HOME"])
    payload = {
        "config": config,
        "config_home": str(config_home),
        "config_home_entries": sorted(path.name for path in config_home.iterdir()),
        "config_overrides": {
            key: os.environ.get(key)
            for key in (
                "OPENCODE_CONFIG",
                "OPENCODE_CONFIG_DIR",
                "OPENCODE_CONFIG_PATH",
                "OPENCODE_CONFIG_CONTENT",
            )
        },
        "data_home": os.environ.get("XDG_DATA_HOME"),
        "project_config_present": (workspace / ".opencode" / "opencode.json").exists(),
    }
    (workspace / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps({"type": "text", "sessionID": "ses_config_probe"}))
elif mode == "timeout":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
elif mode == "session-timeout":
    print(json.dumps({"type": "text", "sessionID": "ses_interrupted"}), flush=True)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
elif mode == "session-stdout-limit":
    print(json.dumps({"type": "text", "sessionID": "ses_interrupted"}), flush=True)
    time.sleep(0.2)
    os.write(sys.stdout.fileno(), b"x" * 4096)
    while True:
        time.sleep(1)
elif mode == "session-single-chunk-total":
    # one atomic write delivers a complete event plus oversized data in a
    # single os.read chunk: the byte limit must keep the event prefix
    os.write(
        sys.stdout.fileno(),
        json.dumps({"type": "text", "sessionID": "ses_prefix"}).encode()
        + b"\n"
        + b"x" * 4096,
    )
    while True:
        time.sleep(1)
elif mode == "session-single-chunk-line":
    # one atomic write delivers a complete event plus an over-limit line in
    # a single os.read chunk: the line limit must keep the event prefix
    os.write(
        sys.stdout.fileno(),
        json.dumps({"type": "text", "sessionID": "ses_prefix"}).encode()
        + b"\n"
        + b"x" * 100,
    )
    while True:
        time.sleep(1)
elif mode == "session-cross-chunk-line":
    # sleeps longer than the runner's 0.25s select timeout force separate
    # os.read chunks, so the over-limit line spans chunk boundaries
    os.write(
        sys.stdout.fileno(),
        json.dumps({"type": "text", "sessionID": "ses_prefix"}).encode() + b"\n",
    )
    time.sleep(0.5)
    os.write(sys.stdout.fileno(), b"a" * 40)
    time.sleep(0.5)
    os.write(sys.stdout.fileno(), b"b" * 40)
    while True:
        time.sleep(1)
elif mode == "session-bad-tail":
    print(json.dumps({"type": "text", "sessionID": "ses_interrupted"}), flush=True)
    sys.stdout.write('{"type": "partial')
    sys.stdout.flush()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
elif mode == "session-malformed-then-valid":
    print("not-json")
    print(json.dumps({"type": "text", "sessionID": "ses_interrupted"}), flush=True)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
elif mode == "session-invalid-then-valid":
    print(json.dumps({"type": "text", "sessionID": ""}))
    print(json.dumps({"type": "text", "sessionID": "ses_interrupted"}), flush=True)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
elif mode == "session-conflict":
    print(json.dumps({"type": "text", "sessionID": "ses_first"}), flush=True)
    print(json.dumps({"type": "text", "sessionID": "ses_second"}), flush=True)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
elif mode == "orphan-timeout":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    Path(os.environ["EPUBFORGE_TEST_CHILD_PID"]).write_text(
        str(child.pid), encoding="utf-8"
    )
elif mode == "stdout-total":
    os.write(sys.stdout.fileno(), b"x" * 4096)
    while True:
        time.sleep(1)
elif mode == "stderr-total":
    os.write(sys.stderr.fileno(), b"x" * 4096)
    while True:
        time.sleep(1)
elif mode == "long-line":
    os.write(sys.stdout.fileno(), b"x" * 512 + b"\n")
    while True:
        time.sleep(1)
elif mode == "bad-json":
    (workspace / "result.json").write_text("{}", encoding="utf-8")
    print("not-json")
elif mode == "duplicate-json":
    (workspace / "result.json").write_text("{}", encoding="utf-8")
    print('{"type":"text","sessionID":"one","sessionID":"two"}')
elif mode == "nonfinite-json":
    (workspace / "result.json").write_text("{}", encoding="utf-8")
    print('{"type":"text","value":NaN}')
elif mode == "symlink-output":
    (workspace / "result.json").symlink_to(workspace / "TASK.md")
    print(json.dumps({"type": "text"}))
elif mode == "oversize-file":
    (workspace / "result.json").write_bytes(b"x" * 1024)
    print(json.dumps({"type": "text"}))
elif mode == "fifo-output":
    os.mkfifo(workspace / "result.json")
    print(json.dumps({"type": "text"}))
elif mode == "hardlink-output":
    backing = workspace / "backing.json"
    backing.write_bytes(b"{}")
    os.link(backing, workspace / "result.json")
    print(json.dumps({"type": "text"}))
elif mode == "aggregate-output":
    (workspace / "first.json").write_bytes(b"a" * 80)
    (workspace / "second.json").write_bytes(b"b" * 80)
    print(json.dumps({"type": "text"}))
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
    temp_root.mkdir()
    return OpenCodeAgentRunner(
        executable=_helper(tmp_path),
        temp_root=temp_root,
        timeout_seconds=2,
        terminate_grace_seconds=0.1,
        **kwargs,
    )


def _request(mode: str = "inspect") -> AgentRunRequest:
    return AgentRunRequest(
        title="runner contract test",
        prompt=mode,
        files={"TASK.md": b"test task\n", "nested/evidence.txt": b"evidence\n"},
        output_limits={"result.json": 64 * 1024},
    )


def _single_run_dir(root: Path) -> Path:
    runs = sorted(root.glob("*"))
    assert len(runs) == 1, f"expected one run dir, found {runs}"
    return runs[0]


def _agent_process(manifest: Any) -> Any:
    return next(process for process in manifest.processes if process.label == "agent")


def test_command_environment_permissions_session_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", recording_popen)
    runner = _runner(tmp_path, execution_profile="restricted")
    temp_root = tmp_path / "isolated"

    result = runner(_request())

    payload = json.loads(result.outputs["result.json"])
    argv = payload["argv"]
    assert argv[:2] == ["run", "--pure"]
    assert argv[argv.index("--agent") + 1] == "book-editor"
    assert argv[argv.index("--format") + 1] == "json"
    assert argv[argv.index("--title") + 1] == "runner contract test"
    assert argv[argv.index("--variant") + 1] == "medium"
    assert argv[-1] == "inspect"
    assert payload["cwd"] == argv[argv.index("--dir") + 1]
    assert payload["workspace_mode"] == "0o700"
    assert payload["task_mode"] == "0o400"
    assert payload["flags"] == {
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
    }
    assert not any(name.lower().endswith(".pdf") for name in payload["files"])

    config = payload["config"]
    agent = config["agent"]["book-editor"]
    assert config["mcp"] == {}
    assert config["plugin"] == []
    assert agent["model"] == "openai/gpt-5.6-luna"
    assert agent["variant"] == "medium"
    assert agent["permission"]["*"] == "deny"
    assert payload["permission_env"] == agent["permission"]
    for permission in ("read", "list", "glob", "grep", "edit"):
        assert agent["permission"][permission] == "allow"
    for permission in (
        "bash",
        "task",
        "external_directory",
        "webfetch",
        "websearch",
        "skill",
        "question",
    ):
        assert agent["permission"][permission] == "deny"
    assert result.session_id == "ses_runner_test"

    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == Path(payload["cwd"])
    assert list(temp_root.iterdir()) == []


def test_development_profile_allows_bash_only_inside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", recording_popen)
    runner = _runner(tmp_path, execution_profile="development")

    result = runner(_request())
    payload = json.loads(result.outputs["result.json"])
    agent = payload["config"]["agent"]["book-editor"]
    assert payload["permission_env"] == agent["permission"]
    assert agent["permission"]["bash"] == "allow"
    for permission in ("read", "list", "glob", "grep", "edit"):
        assert agent["permission"][permission] == "allow"
    for permission in (
        "task",
        "external_directory",
        "webfetch",
        "websearch",
        "skill",
        "question",
        "lsp",
    ):
        assert agent["permission"][permission] == "deny"
    assert agent["permission"]["*"] == "deny"


def test_execution_profiles_distinguish_identity_fingerprints() -> None:
    development = runner_module.book_editor_identity("development")
    restricted = runner_module.book_editor_identity("restricted")
    assert development != restricted
    assert development.prompt_sha256 == restricted.prompt_sha256
    assert development.fingerprint != restricted.fingerprint
    assert runner_module.book_editor_identity() == development


@pytest.mark.parametrize("profile", ["", "prod", "sandbox", None])
def test_invalid_execution_profile_is_rejected(
    tmp_path: Path, profile: str | None
) -> None:
    with pytest.raises(ValueError, match="execution profile"):
        _runner(tmp_path, execution_profile=profile)
    with pytest.raises(ValueError, match="execution profile"):
        runner_module.book_editor_identity(profile if profile is not None else "")


def test_private_config_probe_ignores_inherited_global_and_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config = tmp_path / "global-config"
    (global_config / "opencode").mkdir(parents=True)
    (global_config / "opencode" / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {"stolen": {"type": "remote", "url": "https://evil"}},
                "agent": {"stolen": {"model": "evil/model"}},
            }
        ),
        encoding="utf-8",
    )
    inherited_config = tmp_path / "inherited.json"
    inherited_config.write_text(
        json.dumps({"mcp": {"stolen": {}}, "agent": {"stolen": {}}}),
        encoding="utf-8",
    )
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("OPENCODE_CONFIG", str(inherited_config))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(global_config))
    monkeypatch.setenv("OPENCODE_CONFIG_PATH", str(inherited_config))

    runner = _runner(tmp_path)
    request = AgentRunRequest(
        title="config probe",
        prompt="config-probe",
        files={
            "TASK.md": b"task",
            ".opencode/opencode.json": json.dumps(
                {"mcp": {"project-stolen": {}}, "agent": {"project-stolen": {}}}
            ).encode("utf-8"),
        },
        output_limits={"result.json": 64 * 1024},
    )
    result = runner(request)
    payload = json.loads(result.outputs["result.json"])

    assert payload["config"]["mcp"] == {}
    assert set(payload["config"]["agent"]) == {"book-editor"}
    assert payload["config_overrides"] == {
        "OPENCODE_CONFIG": None,
        "OPENCODE_CONFIG_DIR": None,
        "OPENCODE_CONFIG_PATH": None,
        "OPENCODE_CONFIG_CONTENT": None,
    }
    assert payload["config_home"] != str(global_config)
    assert payload["config_home_entries"] == ["opencode"]
    assert payload["data_home"] == str(data_home)
    assert payload["project_config_present"]


def test_provider_config_probe_preserves_provider_only_and_private_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = {
        "custom": {
            "options": {"baseURL": "https://provider.example/v1"},
            "models": {"custom-model": {"name": "custom-model"}},
        }
    }
    monkeypatch.setenv(
        "EPUBFORGE_TEST_DEBUG_CONFIG",
        json.dumps(
            {
                "provider": provider,
                "enabled_providers": ["custom"],
                "disabled_providers": ["unsafe"],
                "mcp": {"malicious": {"url": "https://evil.example"}},
                "plugin": ["malicious-plugin"],
                "instructions": ["steal instructions"],
                "agent": {"malicious": {"model": "unsafe/model"}},
            }
        ),
    )

    result = _runner(tmp_path)(_request())
    payload = json.loads(result.outputs["result.json"])
    config = payload["config"]

    assert config["provider"] == provider
    assert payload["config_mode"] == "0o600"
    assert config["enabled_providers"] == ["custom"]
    assert config["disabled_providers"] == ["unsafe"]
    assert config["mcp"] == {}
    assert config["plugin"] == []
    assert set(config["agent"]) == {"book-editor"}
    assert "instructions" not in config
    assert config["agent"]["book-editor"]["model"] == "openai/gpt-5.6-luna"


@pytest.mark.parametrize("source", ["content", "path"])
def test_provider_endpoint_sources_are_allowlisted_before_main_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    user_config = {
        "provider": {
            "custom": {
                "options": {"baseURL": "https://provider.example/v1"},
                "models": {"custom-model": {"name": "custom-model"}},
            }
        },
        "mcp": {"malicious": {"type": "remote", "url": "https://evil"}},
        "plugin": ["malicious-plugin"],
        "instructions": ["do not pass through"],
        "agent": {"malicious": {"model": "unsafe/model"}},
    }
    if source == "content":
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(user_config))
    else:
        config_path = tmp_path / "user-opencode.json"
        config_path.write_text(json.dumps(user_config), encoding="utf-8")
        monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))

    result = _runner(tmp_path)(_request("config-probe"))
    payload = json.loads(result.outputs["result.json"])
    config = payload["config"]

    assert config["provider"]["custom"]["options"]["baseURL"] == (
        "https://provider.example/v1"
    )
    assert set(config) == {
        "$schema",
        "share",
        "permission",
        "mcp",
        "plugin",
        "agent",
        "provider",
    }
    assert config["mcp"] == {}
    assert config["plugin"] == []
    assert set(config["agent"]) == {"book-editor"}
    assert payload["config_overrides"] == {
        "OPENCODE_CONFIG": None,
        "OPENCODE_CONFIG_DIR": None,
        "OPENCODE_CONFIG_PATH": None,
        "OPENCODE_CONFIG_CONTENT": None,
    }


@pytest.mark.parametrize("debug_mode", ["timeout", "large-output"])
def test_config_probe_is_bounded_and_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug_mode: str
) -> None:
    monkeypatch.setenv("EPUBFORGE_TEST_DEBUG_MODE", debug_mode)
    # The probe keeps its own fixed timeout bound, independent of the main
    # agent task timeout; shrink the module bound for a fast deterministic
    # test.
    monkeypatch.setattr(runner_module, "_CONFIG_PROBE_TIMEOUT_SECONDS", 0.1)
    runner = _runner(tmp_path)

    with pytest.raises(AgentExecutionError, match="config probe"):
        runner(_request())

    assert list((tmp_path / "isolated").iterdir()) == []


@pytest.mark.parametrize(
    ("mode", "options", "match"),
    [
        ("stdout-total", {"max_stdout_bytes": 128}, "stdout exceeded its byte"),
        ("stderr-total", {"max_stderr_bytes": 128}, "stderr exceeded its byte"),
        (
            "long-line",
            {"max_stdout_bytes": 1024, "max_line_bytes": 64},
            "stdout exceeded its line",
        ),
    ],
)
def test_bounded_process_output_and_cleanup(
    tmp_path: Path,
    mode: str,
    options: dict[str, int],
    match: str,
) -> None:
    runner = _runner(tmp_path, **options)

    with pytest.raises(AgentExecutionError, match=match):
        runner(_request(mode))

    assert list((tmp_path / "isolated").iterdir()) == []


def test_timeout_terminates_process_group_and_cleans_workspace(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner._timeout_seconds = 0.1
    started = time.monotonic()

    with pytest.raises(AgentExecutionError, match="timed out"):
        runner(_request("timeout"))

    assert time.monotonic() - started < 2
    assert list((tmp_path / "isolated").iterdir()) == []


@pytest.mark.parametrize(
    ("mode", "match"),
    [
        ("session-timeout", "timed out"),
        ("session-stdout-limit", "stdout exceeded its byte"),
    ],
)
def test_interrupted_communication_persists_capture_and_session(
    tmp_path: Path, mode: str, match: str
) -> None:
    """A session event accepted before a timeout or byte limit survives."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        max_stdout_bytes=128,
        capture_raw_main_streams=True,
    )
    if mode == "session-timeout":
        runner._timeout_seconds = 0.1

    with pytest.raises(AgentExecutionError, match=match) as caught:
        runner(_request(mode))

    run_dir = _single_run_dir(tmp_path / "agent-runs")
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    # the original communication error is untouched and finalizes the run
    assert manifest.final_status == "failed"
    assert manifest.validation.status == "pending"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    assert manifest.runner_error.error_message == str(caught.value)
    # the early session ID survived through the error-only best-effort parse
    assert manifest.session_parsed is True
    assert manifest.session_parse_mode == "best-effort"
    assert manifest.session_id == "ses_interrupted"
    # capture is explicitly truncated with a bounded non-secret reason
    agent = _agent_process(manifest)
    assert agent.capture == "streams"
    assert agent.capture_complete is False
    assert agent.capture_error_type == "AgentExecutionError"
    assert agent.capture_error_message == str(caught.value)
    # streams hold exactly the bounded bytes accepted before the failure
    expected = b'{"type": "text", "sessionID": "ses_interrupted"}\n'
    if mode == "session-stdout-limit":
        # the byte limit rejects only the bytes past the accepted prefix,
        # which fills the exact remaining stdout byte budget
        expected = expected + b"x" * (128 - len(expected))
    stdout = (run_dir / agent.stdout.filename).read_bytes()
    assert stdout == expected
    assert agent.stdout.size_bytes == len(stdout)
    assert agent.stdout.sha256 == hashlib.sha256(stdout).hexdigest()
    assert agent.stderr is not None and agent.stderr.size_bytes == 0
    # no accepted/rejected semantic status anywhere
    assert manifest.final_status == "failed"
    assert manifest.validation.status == "pending"


@pytest.mark.parametrize(
    ("mode", "options", "match"),
    [
        (
            "session-single-chunk-total",
            {"max_stdout_bytes": 128},
            "stdout exceeded its byte",
        ),
        (
            "session-single-chunk-line",
            {"max_stdout_bytes": 1024, "max_line_bytes": 64},
            "stdout exceeded its line",
        ),
        (
            "session-cross-chunk-line",
            {"max_line_bytes": 64},
            "stdout exceeded its line",
        ),
    ],
)
def test_limit_error_keeps_maximal_valid_prefix_for_recovery(
    tmp_path: Path, mode: str, options: dict[str, int], match: str
) -> None:
    """A complete event followed by limit-violating data in the same chunk
    (or across chunks) stays available to best-effort recovery parsing."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        capture_raw_main_streams=True,
        **options,
    )
    if mode == "session-cross-chunk-line":
        # the helper sleeps 0.5s between writes to force separate reads
        runner._timeout_seconds = 5

    with pytest.raises(AgentExecutionError, match=match) as caught:
        runner(_request(mode))

    event = b'{"type": "text", "sessionID": "ses_prefix"}\n'
    accepted = {
        # byte limit: the prefix fills the exact remaining stdout budget
        "session-single-chunk-total": event + b"x" * (128 - len(event)),
        # line limit: bytes before the first byte that would push the line
        # over the limit; the stored line ends exactly at the limit
        "session-single-chunk-line": event + b"x" * 64,
        "session-cross-chunk-line": event + b"a" * 40 + b"b" * 24,
    }[mode]
    # the recovery parse reads exactly the accepted prefix from the error
    assert caught.value.partial_stdout == accepted

    run_dir = _single_run_dir(tmp_path / "agent-runs")
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    # the original limit error is untouched and finalizes the run
    assert manifest.final_status == "failed"
    assert manifest.validation.status == "pending"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    assert manifest.runner_error.error_message == str(caught.value)
    # the complete event inside the accepted prefix recovered the session
    assert manifest.session_parsed is True
    assert manifest.session_parse_mode == "best-effort"
    assert manifest.session_id == "ses_prefix"
    # capture is explicitly truncated with the original bounded reason
    agent = _agent_process(manifest)
    assert agent.capture == "streams"
    assert agent.capture_complete is False
    assert agent.capture_error_type == "AgentExecutionError"
    assert agent.capture_error_message == str(caught.value)
    # the persisted stream is byte-identical to the accepted prefix
    stdout = (run_dir / agent.stdout.filename).read_bytes()
    assert stdout == accepted
    assert agent.stdout.size_bytes == len(accepted)
    assert agent.stdout.sha256 == hashlib.sha256(accepted).hexdigest()
    assert agent.stderr is not None and agent.stderr.size_bytes == 0
    assert list((tmp_path / "isolated").iterdir()) == []


def test_bounded_bytes_carries_line_accounting_across_chunks() -> None:
    """Partial lines continue into the next chunk and reset at newlines."""
    collector = runner_module._BoundedBytes(
        "stdout", maximum=1024, maximum_line=64, data=bytearray()
    )
    # chunk 1 ends mid-line; its 40 bytes carry into the next chunk
    assert collector.add(b"a" * 40) == b"a" * 40
    assert collector.current_line == 40
    # chunk 2 pushes the carried line over the limit: keep bytes before the
    # first violating byte, never an over-limit stored line
    with pytest.raises(AgentExecutionError, match="line limit") as caught:
        collector.add(b"b" * 40)
    assert caught.value.accepted_prefix == b"b" * 24
    assert bytes(collector.data) == b"a" * 40 + b"b" * 24
    assert collector.current_line == 64
    assert len(collector.data) == 64


def test_bounded_bytes_line_resets_on_newline_across_chunks() -> None:
    collector = runner_module._BoundedBytes(
        "stdout", maximum=1024, maximum_line=64, data=bytearray()
    )
    # a newline closes the carried partial line and starts a fresh one
    assert collector.add(b"a" * 40 + b"\nb") == b"a" * 40 + b"\nb"
    assert collector.current_line == 1
    # chunk 3 pushes the fresh line over the limit after exactly 63 bytes
    with pytest.raises(AgentExecutionError, match="line limit") as caught:
        collector.add(b"x" * 70)
    assert caught.value.accepted_prefix == b"x" * 63
    assert bytes(collector.data) == b"a" * 40 + b"\nb" + b"x" * 63
    assert collector.current_line == 64


def test_bounded_bytes_rejects_chunk_starting_over_limit_line() -> None:
    collector = runner_module._BoundedBytes(
        "stdout", maximum=1024, maximum_line=64, data=bytearray()
    )
    # a partial line exactly at the limit is still accepted
    assert collector.add(b"a" * 64) == b"a" * 64
    assert collector.current_line == 64
    # the first byte of the next chunk is the first violating byte
    with pytest.raises(AgentExecutionError, match="line limit") as caught:
        collector.add(b"b")
    assert caught.value.accepted_prefix == b""
    assert bytes(collector.data) == b"a" * 64
    assert collector.current_line == 64


def test_bounded_bytes_total_budget_accepts_exact_remaining_bytes() -> None:
    collector = runner_module._BoundedBytes(
        "stdout", maximum=128, maximum_line=1024, data=bytearray()
    )
    assert collector.add(b"a" * 50) == b"a" * 50
    with pytest.raises(AgentExecutionError, match="byte limit") as caught:
        collector.add(b"b" * 100)
    assert caught.value.accepted_prefix == b"b" * 78
    assert bytes(collector.data) == b"a" * 50 + b"b" * 78
    assert len(collector.data) == 128
    # once the budget is exhausted the next chunk accepts nothing
    with pytest.raises(AgentExecutionError, match="byte limit") as caught:
        collector.add(b"c")
    assert caught.value.accepted_prefix == b""
    assert bytes(collector.data) == b"a" * 50 + b"b" * 78


@pytest.mark.parametrize(
    ("mode", "expected_session"),
    [
        ("session-bad-tail", "ses_interrupted"),
        ("session-malformed-then-valid", "ses_interrupted"),
        ("session-invalid-then-valid", "ses_interrupted"),
        ("session-conflict", None),
    ],
)
def test_interrupted_capture_best_effort_session_recovery(
    tmp_path: Path, mode: str, expected_session: str | None
) -> None:
    """Recovery parsing ignores fragments and malformed lines; never conflicts."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        capture_raw_main_streams=True,
    )
    runner._timeout_seconds = 0.1

    with pytest.raises(AgentExecutionError, match="timed out"):
        runner(_request(mode))

    run_dir = _single_run_dir(tmp_path / "agent-runs")
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.session_parsed is True
    assert manifest.session_parse_mode == "best-effort"
    assert manifest.session_id == expected_session
    agent = _agent_process(manifest)
    assert agent.capture_complete is False
    assert agent.capture_error_type == "AgentExecutionError"
    assert list((tmp_path / "isolated").iterdir()) == []


def test_interrupted_run_without_stream_capture_is_readable(tmp_path: Path) -> None:
    """Safe default: an interrupted run keeps best-effort session, no streams."""
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")
    runner._timeout_seconds = 0.1

    with pytest.raises(AgentExecutionError, match="timed out"):
        runner(_request("session-timeout"))

    run_dir = _single_run_dir(tmp_path / "agent-runs")
    assert sorted(path.name for path in run_dir.iterdir()) == ["manifest.json"]
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    assert manifest.session_parsed is True
    assert manifest.session_parse_mode == "best-effort"
    assert manifest.session_id == "ses_interrupted"
    agent = _agent_process(manifest)
    assert agent.capture == "disabled"
    assert agent.stdout is None and agent.stderr is None
    assert agent.capture_complete is None
    assert agent.capture_error_type is None
    assert agent.result == "error"
    assert agent.ended_at_utc is not None
    assert list((tmp_path / "isolated").iterdir()) == []


def test_timeout_kills_child_after_process_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "child.pid"
    monkeypatch.setenv("EPUBFORGE_TEST_CHILD_PID", str(pid_path))
    runner = _runner(tmp_path)
    runner._timeout_seconds = 0.1

    with pytest.raises(AgentExecutionError, match="timed out"):
        runner(_request("orphan-timeout"))

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    process_status = Path(f"/proc/{child_pid}/stat")
    for _ in range(100):
        if not process_status.exists():
            break
        fields = process_status.read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("child process remained alive after process-group termination")
    assert list((tmp_path / "isolated").iterdir()) == []


@pytest.mark.parametrize(
    "agent_request",
    [
        AgentRunRequest(
            title="pdf",
            prompt=BOOK_EDITOR_PROMPT,
            files={"source.pdf": b"pdf"},
            output_limits={"result.json": 10},
        ),
        AgentRunRequest(
            title="escape",
            prompt=BOOK_EDITOR_PROMPT,
            files={"../TASK.md": b"task"},
            output_limits={"result.json": 10},
        ),
        AgentRunRequest(
            title="disguised-pdf",
            prompt=BOOK_EDITOR_PROMPT,
            files={"source.bin": b"%PDF-1.7\n"},
            output_limits={"result.json": 10},
        ),
    ],
)
def test_rejects_pdf_and_path_escape_before_workspace_creation(
    tmp_path: Path, agent_request: AgentRunRequest
) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(AgentRunnerError):
        runner(agent_request)

    assert list((tmp_path / "isolated").iterdir()) == []


@pytest.mark.parametrize(
    ("mode", "match"),
    [
        ("bad-json", "invalid JSON"),
        ("duplicate-json", "invalid JSON"),
        ("nonfinite-json", "invalid JSON"),
        ("symlink-output", "regular result.json"),
    ],
)
def test_rejects_invalid_json_stream_and_symlink_output(
    tmp_path: Path, mode: str, match: str
) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(AgentRunnerError, match=match):
        runner(_request(mode))

    assert list((tmp_path / "isolated").iterdir()) == []


@pytest.mark.parametrize("mode", ["fifo-output", "hardlink-output"])
def test_rejects_non_regular_or_hardlinked_output(tmp_path: Path, mode: str) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(AgentRunnerError, match="regular|hard links"):
        runner(_request(mode))

    assert list((tmp_path / "isolated").iterdir()) == []


def test_output_reader_rejects_workspace_symlink_and_parent_symlink(
    tmp_path: Path,
) -> None:
    real_workspace = tmp_path / "workspace"
    real_workspace.mkdir()
    (real_workspace / "result.json").write_bytes(b"{}")
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(real_workspace, target_is_directory=True)

    with pytest.raises(AgentRunnerError):
        runner_module._read_regular_output(workspace_link, "result.json", max_bytes=64)

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AgentRunnerError):
        runner_module._read_regular_output(
            parent_link / "workspace", "result.json", max_bytes=64
        )


def test_output_reader_closes_workspace_fd_when_output_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    closed: set[int] = set()
    real_close = runner_module.os.close

    def record_close(fd: int) -> None:
        closed.add(fd)
        real_close(fd)

    monkeypatch.setattr(runner_module, "_open_workspace_fd", lambda _path: workspace_fd)

    def fail_open(*args: Any, **kwargs: Any) -> int:
        raise OSError("missing output")

    monkeypatch.setattr(runner_module.os, "open", fail_open)
    monkeypatch.setattr(runner_module.os, "close", record_close)

    with pytest.raises(AgentRunnerError, match="regular result.json"):
        runner_module._read_regular_output(workspace, "result.json", max_bytes=64)

    assert workspace_fd in closed


def test_aggregate_output_budget_is_enforced(tmp_path: Path) -> None:
    runner = _runner(tmp_path, max_output_bytes=100)
    request = AgentRunRequest(
        title="aggregate output",
        prompt="aggregate-output",
        files={"TASK.md": b"task"},
        output_limits={"first.json": 128, "second.json": 128},
    )

    with pytest.raises(AgentRunnerError, match="total byte limit"):
        runner(request)

    assert list((tmp_path / "isolated").iterdir()) == []


def test_rejects_nested_declared_output_before_workspace_creation(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    request = AgentRunRequest(
        title="nested output",
        prompt=BOOK_EDITOR_PROMPT,
        files={"TASK.md": b"task"},
        output_limits={"nested/result.json": 64},
    )

    with pytest.raises(AgentRunnerError, match="direct workspace child"):
        runner(request)

    assert list((tmp_path / "isolated").iterdir()) == []


def test_rejects_oversized_output_file_and_nonzero_exit(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    request = AgentRunRequest(
        title="output limit",
        prompt="oversize-file",
        files={"TASK.md": b"task"},
        output_limits={"result.json": 32},
    )

    with pytest.raises(AgentRunnerError, match="output exceeds its byte limit"):
        runner(request)
    with pytest.raises(AgentExecutionError) as caught:
        runner(_request("exit-failure"))

    assert caught.value.returncode == 9
    assert list((tmp_path / "isolated").iterdir()) == []


def test_workspace_cannot_be_created_inside_forbidden_book_root(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    request = AgentRunRequest(
        title="forbidden",
        prompt=BOOK_EDITOR_PROMPT,
        files={"TASK.md": b"task"},
        output_limits={"result.json": 10},
        forbidden_roots=(tmp_path,),
    )

    with pytest.raises(AgentRunnerError, match="forbidden project"):
        runner(request)

    assert list((tmp_path / "isolated").iterdir()) == []


def test_temp_parent_symlink_is_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    runner = OpenCodeAgentRunner(
        executable=_helper(tmp_path),
        temp_root=linked_parent,
        timeout_seconds=2,
        terminate_grace_seconds=0.1,
    )

    with pytest.raises(AgentRunnerError, match="temp root"):
        runner(_request())


def _assert_process_reaped(pid: int) -> None:
    """Poll /proc until the pid is gone or a zombie, then return."""
    stat_path = Path(f"/proc/{pid}/stat")
    for _ in range(200):
        if not stat_path.exists():
            return
        fields = stat_path.read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} remained alive after process-group termination")


def _fail_process_pid_for(captured: dict[str, int], target_label: str) -> Any:
    """Return a process_pid that records and fails for one label only."""
    real_process_pid = journal_module.AgentRunJournal.process_pid

    def failing_process_pid(self: Any, label: str, pid: int) -> None:
        if label != target_label:
            real_process_pid(self, label, pid)
            return
        captured["pid"] = pid
        raise AgentRunJournalError("injected journal failure")

    return failing_process_pid


def test_recorder_failure_after_agent_popen_terminates_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal failure right after the agent Popen must not orphan it."""
    captured: dict[str, int] = {}
    monkeypatch.setattr(
        journal_module.AgentRunJournal,
        "process_pid",
        _fail_process_pid_for(captured, "agent"),
    )
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")

    with pytest.raises(AgentRunJournalError, match="injected journal failure"):
        runner(_request("timeout"))

    assert "pid" in captured
    _assert_process_reaped(captured["pid"])
    assert list((tmp_path / "isolated").iterdir()) == []


def test_recorder_failure_after_probe_popen_terminates_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal failure right after the probe Popen must not orphan it."""
    monkeypatch.setenv("EPUBFORGE_TEST_DEBUG_MODE", "timeout")
    captured: dict[str, int] = {}
    monkeypatch.setattr(
        journal_module.AgentRunJournal,
        "process_pid",
        _fail_process_pid_for(captured, "config-probe"),
    )
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")

    with pytest.raises(AgentRunJournalError, match="injected journal failure"):
        runner(_request())

    assert "pid" in captured
    _assert_process_reaped(captured["pid"])
    assert list((tmp_path / "isolated").iterdir()) == []


def test_popen_failure_records_error_without_claiming_a_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed Popen records an error and never claims a process PID."""

    def failing_popen(*args: Any, **kwargs: Any) -> Any:
        raise OSError("cannot execute opencode")

    monkeypatch.setattr(runner_module.subprocess, "Popen", failing_popen)
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")

    with pytest.raises(AgentExecutionError, match="cannot start"):
        runner(_request())

    run_dirs = sorted((tmp_path / "agent-runs").glob("*"))
    assert len(run_dirs) == 1
    manifest = read_agent_run_manifest(run_dirs[0] / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    (probe,) = manifest.processes
    assert probe.label == "config-probe"
    assert probe.result == "error"
    assert probe.pid is None
    assert probe.returncode is None
    assert list((tmp_path / "isolated").iterdir()) == []


def _exited_process() -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(9)"],
        start_new_session=True,
    )
    process.wait()
    return process


def test_terminate_process_group_raises_when_group_cannot_be_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reap failure must raise, never warn and return."""
    process = _exited_process()
    monkeypatch.setattr(runner_module, "_signal_process_group", lambda *a, **k: True)
    monkeypatch.setattr(runner_module, "_process_group_exists", lambda *a, **k: True)

    with pytest.raises(AgentExecutionError, match="cannot guarantee") as caught:
        runner_module._terminate_process_group(process, grace_seconds=0.01)

    assert "leader_reaped=True" in str(caught.value)
    assert "group_remaining=True" in str(caught.value)


def test_terminate_process_group_returns_when_leader_reaped_and_group_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _exited_process()
    monkeypatch.setattr(runner_module, "_signal_process_group", lambda *a, **k: False)
    monkeypatch.setattr(runner_module, "_process_group_exists", lambda *a, **k: False)

    runner_module._terminate_process_group(process, grace_seconds=0.01)


def test_termination_failure_raises_cleanup_error_and_retains_accessible_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group that cannot be cleared keeps every directory the process can reach."""
    captured: dict[str, int] = {}
    monkeypatch.setattr(
        journal_module.AgentRunJournal,
        "process_pid",
        _fail_process_pid_for(captured, "agent"),
    )
    # Pretend signals are delivered but the group never clears: termination
    # cannot be guaranteed, so the workspace and config home must stay for
    # manual cleanup instead of being deleted under a possibly-live process.
    monkeypatch.setattr(runner_module, "_signal_process_group", lambda *a, **k: True)
    monkeypatch.setattr(runner_module, "_process_group_exists", lambda *a, **k: True)
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")

    with pytest.raises(AgentCleanupError, match="cannot guarantee cleanup") as caught:
        runner(_request("inspect"))

    assert isinstance(caught.value.__cause__, AgentRunJournalError)
    assert "pid" in captured
    retained = sorted(path.name for path in (tmp_path / "isolated").iterdir())
    assert any(name.startswith("epubforge-agent-") for name in retained)
    assert any(name.startswith("epubforge-opencode-config-") for name in retained)
    # the successfully completed probe directory was still removed
    assert not any(name.startswith("epubforge-opencode-probe-") for name in retained)

    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentCleanupError"
    # the possibly-live agent was never recorded as finished; the failed
    # process_pid journal call also never persisted the pid
    agent = _agent_process(manifest)
    assert agent.result == "not_run"
    assert agent.pid is None
    assert agent.ended_at_utc is None


def test_successful_termination_preserves_original_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean termination re-raises the original timeout untouched."""
    runner = _runner(tmp_path)
    runner._timeout_seconds = 0.1
    terminated: list[int] = []
    real_terminate = runner_module._terminate_process_group

    def recording_terminate(process: Any, grace_seconds: float) -> None:
        terminated.append(process.pid)
        real_terminate(process, grace_seconds)

    monkeypatch.setattr(runner_module, "_terminate_process_group", recording_terminate)

    with pytest.raises(AgentExecutionError, match=r"timed out after 0\.1s") as caught:
        runner(_request("timeout"))

    assert terminated
    assert caught.value.returncode is None
    assert not isinstance(caught.value, AgentCleanupError)
    assert getattr(caught.value, "__notes__", None) is None
    assert list((tmp_path / "isolated").iterdir()) == []


def test_cleanup_failure_adds_note_and_preserves_primary_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace removal failure never replaces the active timeout."""
    runner = _runner(tmp_path)
    runner._timeout_seconds = 0.1
    real_remove = runner_module._remove_workspace

    def failing_remove(path: Any) -> None:
        if Path(path).name.startswith("epubforge-agent-"):
            raise OSError("injected removal failure")
        real_remove(path)

    monkeypatch.setattr(runner_module, "_remove_workspace", failing_remove)

    with pytest.raises(AgentExecutionError, match="timed out") as caught:
        runner(_request("timeout"))

    assert caught.value.returncode is None
    assert not isinstance(caught.value, AgentCleanupError)
    notes = getattr(caught.value, "__notes__", [])
    assert any("cannot remove isolated agent workspace" in note for note in notes)


def test_standalone_cleanup_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an active run error the workspace removal failure raises."""
    runner = _runner(tmp_path)
    real_remove = runner_module._remove_workspace

    def failing_remove(path: Any) -> None:
        if Path(path).name.startswith("epubforge-agent-"):
            raise OSError("injected removal failure")
        real_remove(path)

    monkeypatch.setattr(runner_module, "_remove_workspace", failing_remove)

    with pytest.raises(
        AgentRunnerError, match="cannot remove isolated agent workspace"
    ):
        runner(_request())


def test_success_path_cleanup_failure_marks_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup failure on the otherwise-successful path fails the run.

    The workspace removal is the sole failure, so the journal must finalize
    the run as failed before the cleanup error propagates: no started
    manifest and no open run/root descriptors may survive.
    """
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")
    real_remove = runner_module._remove_workspace

    def failing_remove(path: Any) -> None:
        if Path(path).name.startswith("epubforge-agent-"):
            raise OSError("injected removal failure")
        real_remove(path)

    monkeypatch.setattr(runner_module, "_remove_workspace", failing_remove)

    with pytest.raises(
        AgentRunnerError, match="cannot remove isolated agent workspace"
    ):
        runner(_request())

    # the run was finalized failed with the cleanup error, never left started
    run_dir = _single_run_dir(tmp_path / "agent-runs")
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.validation.status == "pending"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentRunnerError"
    assert manifest.finalization is not None
    assert manifest.finalization.status == "completed"
    assert manifest.finalization.target_status == "failed"
    # a completed finalization released every descriptor; the run directory
    # holds only the manifest and no crash-left temporary files
    assert sorted(path.name for path in run_dir.iterdir()) == ["manifest.json"]
    # the workspace could not be removed and stays for manual cleanup
    assert any(
        path.name.startswith("epubforge-agent-")
        for path in (tmp_path / "isolated").iterdir()
    )
    assert not any(
        path.name.startswith("epubforge-opencode-config-")
        or path.name.startswith("epubforge-opencode-probe-")
        for path in (tmp_path / "isolated").iterdir()
    )


def test_main_timeout_reaches_main_agent_while_probe_succeeds(tmp_path: Path) -> None:
    """A tiny task timeout never shortens the independent config probe window."""
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")
    runner._timeout_seconds = 0.01

    with pytest.raises(AgentExecutionError, match=r"timed out after 0\.01s"):
        runner(_request("timeout"))

    run_dir = _single_run_dir(tmp_path / "agent-runs")
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    (probe,) = [p for p in manifest.processes if p.label == "config-probe"]
    assert probe.result == "success"
    assert probe.returncode == 0
    assert manifest.final_status == "failed"
    assert list((tmp_path / "isolated").iterdir()) == []


# -- workspace retention policies ----------------------------------------------
def _retained_workspaces(temp_root: Path) -> list[Path]:
    return [
        path for path in temp_root.iterdir() if path.name.startswith("epubforge-agent-")
    ]


def _workspace_identity(workspace: Path) -> tuple[int, int] | None:
    """Current (st_dev, st_ino) of *workspace*, or None when it is gone."""
    try:
        info = os.lstat(workspace)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino)


def test_workspace_retention_defaults_are_never_for_direct_runners(
    tmp_path: Path,
) -> None:
    """Direct runners keep the legacy eager-cleanup default."""
    assert runner_module.DEFAULT_WORKSPACE_RETENTION == "never"
    assert runner_module.WORKSPACE_RETENTION_VALUES == (
        "never",
        "on-failure",
        "always",
    )
    runner = _runner(tmp_path)
    assert runner._workspace_retention == "never"
    result = runner(_request())
    assert list((tmp_path / "isolated").iterdir()) == []
    assert result.journal is None


@pytest.mark.parametrize("value", ["", "sometimes", "NEVER", None])
def test_invalid_workspace_retention_is_rejected(tmp_path: Path, value: Any) -> None:
    with pytest.raises(ValueError, match="workspace retention"):
        _runner(tmp_path, workspace_retention=value)


def test_never_retention_records_deleted_disposition_after_runner_failure(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, journal_root=tmp_path / "agent-runs")
    runner._timeout_seconds = 0.1

    with pytest.raises(AgentExecutionError, match="timed out"):
        runner(_request("timeout"))

    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "failed"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert manifest.workspace_disposition.deleted_at_utc is not None
    assert manifest.workspace_disposition.retained_at_utc is None
    assert list((tmp_path / "isolated").iterdir()) == []


@pytest.mark.parametrize("policy", ["on-failure", "always"])
def test_retention_keeps_workspace_after_runner_failure(
    tmp_path: Path, policy: str
) -> None:
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention=policy,
    )
    runner._timeout_seconds = 0.1

    with pytest.raises(AgentExecutionError, match="timed out"):
        runner(_request("timeout"))

    run_dir = _single_run_dir(tmp_path / "agent-runs")
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "failed"
    assert manifest.runner_error is not None
    assert manifest.runner_error.error_type == "AgentExecutionError"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.retained_at_utc is not None
    assert manifest.workspace_disposition.deleted_at_utc is None
    assert manifest.workspace_disposition.reason is not None
    assert "retention" in manifest.workspace_disposition.reason

    workspace = Path(manifest.workspace_path)
    retained = _retained_workspaces(tmp_path / "isolated")
    assert retained == [workspace]
    assert workspace.is_dir()
    # the retained workspace stays mode 0700, keeps restrictive file modes,
    # and never contains a source PDF
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert not list(workspace.rglob("*.pdf"))
    task = workspace / "TASK.md"
    assert task.is_file()
    assert stat.S_IMODE(task.stat().st_mode) == 0o400
    # config home and probe directories keep their unconditional cleanup rules
    assert not any(
        path.name.startswith("epubforge-opencode-")
        for path in (tmp_path / "isolated").iterdir()
    )


def test_on_failure_retention_defers_cleanup_until_accepted(
    tmp_path: Path,
) -> None:
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    run_dir = _single_run_dir(tmp_path / "agent-runs")
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    assert workspace.is_dir()
    # the runner returned with the manifest still running and disposition
    # pending: the caller owns the cleanup decision
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "started"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "pending"

    result.journal.mark_accepted()
    assert not workspace.exists()
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    assert manifest.final_status == "accepted"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert manifest.workspace_disposition.deleted_at_utc is not None


def test_on_failure_retention_keeps_workspace_after_rejection(
    tmp_path: Path,
) -> None:
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert workspace.is_dir()
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert not list(workspace.rglob("*.pdf"))
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.validation.status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.retained_at_utc is not None
    assert manifest.workspace_disposition.reason is not None
    assert "on-failure" in manifest.workspace_disposition.reason


def test_always_retention_keeps_workspace_after_acceptance(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="always",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]

    result.journal.mark_accepted()

    assert workspace.is_dir()
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert not list(workspace.rglob("*.pdf"))
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "accepted"
    assert manifest.validation.status == "accepted"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.retained_at_utc is not None
    assert manifest.workspace_disposition.deleted_at_utc is None
    assert manifest.workspace_disposition.reason is not None
    assert "always" in manifest.workspace_disposition.reason


def test_unfinalized_lifecycle_handle_keeps_workspace_and_pending_disposition(
    tmp_path: Path,
) -> None:
    """A caller crash without finalization keeps evidence, never deletes it."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    assert workspace.is_dir()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "started"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "pending"
    assert manifest.workspace_disposition.deleted_at_utc is None
    assert manifest.workspace_disposition.retained_at_utc is None


def test_on_failure_accepted_cleanup_failure_retains_and_stays_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed deletion after acceptance never misreports: retained + reason."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    real_remove = runner_module._remove_workspace

    def failing_remove(path: Any) -> None:
        if Path(path).name.startswith("epubforge-agent-"):
            raise OSError("injected cleanup failure")
        real_remove(path)

    monkeypatch.setattr(runner_module, "_remove_workspace", failing_remove)
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]

    result.journal.mark_accepted()

    assert workspace.is_dir()
    run_dir = _single_run_dir(tmp_path / "agent-runs")
    manifest = read_agent_run_manifest(run_dir / "manifest.json")
    # publication stays accepted; the durable record carries the truth
    assert manifest.final_status == "accepted"
    assert manifest.validation.status == "accepted"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.retained_at_utc is not None
    assert manifest.workspace_disposition.deleted_at_utc is None
    assert manifest.workspace_disposition.reason is not None
    assert "cleanup failed" in manifest.workspace_disposition.reason


def test_on_failure_without_journal_still_defers_cleanup(tmp_path: Path) -> None:
    """Deferred cleanup also works for journal-less direct runners."""
    runner = _runner(tmp_path, workspace_retention="on-failure")
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    assert workspace.is_dir()

    result.journal.mark_accepted()

    assert not workspace.exists()
    assert list((tmp_path / "isolated").iterdir()) == []


def test_concurrent_on_failure_runs_keep_distinct_retained_workspaces(
    tmp_path: Path,
) -> None:
    """Concurrent runs keep unique run dirs and retained workspaces.

    Each thread gets its own helper executable and temp root: concurrently
    exec-ing a single shared helper script from four threads intermittently
    produces a silently empty config-probe subprocess in the pytest process,
    which is unrelated to the runner and would make this test flaky.
    """
    from concurrent.futures import ThreadPoolExecutor

    journal_root = tmp_path / "agent-runs"
    errors: list[BaseException] = []

    def run_one(index: int) -> None:
        try:
            isolated = tmp_path / f"iso-{index}"
            isolated.mkdir()
            runner = OpenCodeAgentRunner(
                executable=_helper(isolated),
                temp_root=isolated,
                timeout_seconds=10,
                terminate_grace_seconds=0.1,
                journal_root=journal_root,
                workspace_retention="always",
            )
            result = runner(_request())
            assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
            result.journal.mark_accepted()
        except BaseException as exc:  # pragma: no cover - failure diagnosis
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_one, range(8)))

    assert errors == []
    runs = sorted(journal_root.glob("*"))
    assert len(runs) == 8
    workspaces: list[Path] = []
    for run_dir in runs:
        manifest = read_agent_run_manifest(run_dir / "manifest.json")
        assert manifest.final_status == "accepted"
        assert manifest.workspace_path is not None
        assert manifest.workspace_disposition is not None
        assert manifest.workspace_disposition.disposition == "retained"
        assert Path(manifest.workspace_path).is_dir()
        workspaces.append(Path(manifest.workspace_path))
    assert len(set(workspaces)) == 8


# -- retention verification and truthful disposition ---------------------------


def test_no_journal_on_failure_retains_and_logs_workspace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a journal the retained workspace is still logged and located."""
    runner = _runner(tmp_path, workspace_retention="on-failure")
    runner._timeout_seconds = 0.1
    caplog.set_level(logging.INFO, logger="epubforge.agent_runner")

    with pytest.raises(AgentExecutionError, match="timed out"):
        runner(_request("timeout"))

    workspaces = _retained_workspaces(tmp_path / "isolated")
    assert len(workspaces) == 1
    assert workspaces[0].is_dir()
    assert stat.S_IMODE(workspaces[0].stat().st_mode) == 0o700
    # the log names the exact path with the bounded reason
    assert any(
        "retaining agent workspace for manual cleanup" in record.message
        and str(workspaces[0]) in record.message
        for record in caplog.records
    )


def test_deferred_retention_logs_path_before_caller_finalizes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The retained path is logged at runner return, before any finalize."""
    runner = _runner(tmp_path, workspace_retention="on-failure")
    caplog.set_level(logging.INFO, logger="epubforge.agent_runner")
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    assert any(
        "keeping agent workspace for deferred cleanup" in record.message
        and str(workspace) in record.message
        for record in caplog.records
    )


def test_agent_deleted_workspace_records_deleted_after_runner_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A workspace the agent already removed is never claimed retained."""
    monkeypatch.setenv("EPUBFORGE_TEST_AGENT_MODE", "success")

    def delete_then_fail(
        self: Any, workspace: Path, request: AgentRunRequest, journal: Any
    ) -> tuple[bytes, bytes]:
        shutil.rmtree(workspace)
        raise AgentExecutionError("agent removed its own workspace")

    monkeypatch.setattr(runner_module.OpenCodeAgentRunner, "_invoke", delete_then_fail)
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    caplog.set_level(logging.INFO, logger="epubforge.agent_runner")

    with pytest.raises(AgentExecutionError, match="removed its own workspace"):
        runner(_request())

    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "failed"
    assert manifest.workspace_path is not None
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert not Path(manifest.workspace_path).exists()
    assert any("no longer exists" in record.message for record in caplog.records)


def test_agent_deleted_workspace_records_deleted_on_rejection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A workspace deleted between runner return and rejection: deleted."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    shutil.rmtree(workspace)
    caplog.set_level(logging.INFO, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert not workspace.exists()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert manifest.workspace_disposition.deleted_at_utc is not None
    assert any("no longer exists" in record.message for record in caplog.records)


def test_chmod_mutated_workspace_is_restored_before_retention(
    tmp_path: Path,
) -> None:
    """A mutated root mode is restored to 0700 before retention is claimed."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    os.chmod(workspace, 0o777)
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o777

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert workspace.is_dir()
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"


def test_resecure_failure_removes_unsecurable_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unsecurable workspace is safely removed instead of retained."""
    monkeypatch.setattr(
        runner_module,
        "_inspect_workspace_for_retention",
        lambda workspace: (
            "insecure",
            "injected re-secure failure",
            _workspace_identity(workspace),
        ),
    )
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    caplog.set_level(logging.WARNING, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert not workspace.exists()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert any(
        "removed unsecurable agent workspace" in record.message
        for record in caplog.records
    )


def test_resecure_and_removal_failure_keeps_truthful_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Both failures keep the truthful existing state with a security error."""
    monkeypatch.setattr(
        runner_module,
        "_inspect_workspace_for_retention",
        lambda workspace: (
            "insecure",
            "injected re-secure failure",
            _workspace_identity(workspace),
        ),
    )

    def failing_remove(workspace: Path, original_identity: Any) -> None:
        raise OSError("injected removal failure")

    monkeypatch.setattr(runner_module, "_remove_unverified_workspace", failing_remove)
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    caplog.set_level(logging.ERROR, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert workspace.is_dir()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.reason is not None
    assert "injected re-secure failure" in manifest.workspace_disposition.reason
    assert any(
        "cannot securely keep or remove agent workspace" in record.message
        for record in caplog.records
    )


def test_symlink_at_workspace_path_is_removed_not_retained(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A symlink planted at the workspace path is never claimed retained.

    The no-follow open fails on the symlink before any mode restore, so the
    symlink target is never chmodded and keeps its data.
    """
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    shutil.rmtree(workspace)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    marker = decoy / "marker.txt"
    marker.write_text("precious data", encoding="utf-8")
    os.chmod(decoy, 0o755)
    workspace.symlink_to(decoy, target_is_directory=True)
    caplog.set_level(logging.WARNING, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert not workspace.is_symlink()
    assert not workspace.exists()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    # the symlink target was never chmodded and keeps its data
    assert stat.S_IMODE(decoy.stat().st_mode) == 0o755
    assert marker.read_text(encoding="utf-8") == "precious data"


def test_resecure_after_path_replacement_classifies_current_path_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A swap after the fd opens is classified by the current path, truthfully.

    The directory descriptor pins the original inode and the mode restore
    runs through ``fchmod`` on it, so the symlink target is never chmodded.
    The identity re-check then sees that the path now holds a symlink, which
    is not the original directory: the workspace is recorded ``deleted`` and
    the symlink entry is removed — the original path is never claimed as a
    secure retained workspace.
    """
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    # force the mode-restore branch: the workspace root is world-accessible
    os.chmod(workspace, 0o777)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    marker = decoy / "marker.txt"
    marker.write_text("precious data", encoding="utf-8")
    os.chmod(decoy, 0o755)
    swapped = {"done": False}
    real_open = runner_module.os.open

    def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped["done"] and str(path) == str(workspace):
            # replace the path right after the directory descriptor opened
            swapped["done"] = True
            shutil.rmtree(workspace)
            workspace.symlink_to(decoy, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(runner_module.os, "open", swapping_open)
    caplog.set_level(logging.WARNING, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    # the identity re-check classified the CURRENT path truthfully: the
    # workspace is gone from the path, the symlink entry was removed, and the
    # original path is never recorded as a secure retained workspace
    assert not workspace.is_symlink()
    assert not workspace.exists()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert any(
        "removed unsecurable agent workspace" in record.message
        for record in caplog.records
    )
    # the symlink target was never chmodded and keeps its data
    assert stat.S_IMODE(decoy.stat().st_mode) == 0o755
    assert marker.read_text(encoding="utf-8") == "precious data"


def test_replaced_directory_after_open_is_never_deleted_or_claimed_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A different-directory replacement is kept and recorded truthfully.

    The identity re-check sees a different ``(st_dev, st_ino)`` at the path;
    the removal refuses to delete a directory not proven to be the original
    workspace, so the replacement and its data survive and the record names
    the mismatch.
    """
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    swapped = {"done": False}
    real_open = runner_module.os.open

    def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped["done"] and str(path) == str(workspace):
            swapped["done"] = True
            shutil.rmtree(workspace)
            workspace.mkdir()
            (workspace / "marker.txt").write_text("other user's data", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(runner_module.os, "open", swapping_open)
    caplog.set_level(logging.ERROR, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert workspace.is_dir()
    assert (workspace / "marker.txt").read_text(encoding="utf-8") == "other user's data"
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.reason is not None
    assert "different directory" in manifest.workspace_disposition.reason
    assert any(
        "cannot securely keep or remove agent workspace" in record.message
        for record in caplog.records
    )


def test_path_removed_after_open_records_deleted_not_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A path that vanishes after the open is recorded deleted, never retained."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    swapped = {"done": False}
    real_open = runner_module.os.open

    def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped["done"] and str(path) == str(workspace):
            swapped["done"] = True
            shutil.rmtree(workspace)
        return descriptor

    monkeypatch.setattr(runner_module.os, "open", swapping_open)
    caplog.set_level(logging.INFO, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert not workspace.exists()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert any("no longer exists" in record.message for record in caplog.records)


def test_removal_raise_after_partial_delete_records_deleted_when_path_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A removal that raises after deleting the path records deleted, never retained.

    The secure-delete fallback is first classified insecure, then actually
    removes the workspace before raising; the resolver must re-inspect and
    record the truthful missing state instead of blaming the raise.
    """
    real_inspect = runner_module._inspect_workspace_for_retention
    calls = {"count": 0}

    def flaky_inspect(workspace: Path) -> tuple[str, str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return ("insecure", "injected re-secure failure", None)
        return real_inspect(workspace)

    monkeypatch.setattr(
        runner_module, "_inspect_workspace_for_retention", flaky_inspect
    )

    def remove_then_raise(workspace: Path, original_identity: Any) -> None:
        shutil.rmtree(workspace)
        raise OSError("injected removal failure after partial work")

    monkeypatch.setattr(
        runner_module, "_remove_unverified_workspace", remove_then_raise
    )
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    caplog.set_level(logging.WARNING, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert not workspace.exists()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert any(
        "was removed despite a removal error" in record.message
        for record in caplog.records
    )


def test_removal_raise_after_partial_delete_records_replaced_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A removal that raises after replacing the path records the replacement.

    The partial deletion swapped the workspace for a symlink before raising;
    the resolver must re-inspect, recognize the non-directory replacement,
    and record the truthful retained state with the bounded final-state
    reason rather than claiming deleted.
    """
    real_inspect = runner_module._inspect_workspace_for_retention
    calls = {"count": 0}

    def flaky_inspect(workspace: Path) -> tuple[str, str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return ("insecure", "injected re-secure failure", None)
        return real_inspect(workspace)

    monkeypatch.setattr(
        runner_module, "_inspect_workspace_for_retention", flaky_inspect
    )

    def replace_then_raise(workspace: Path, original_identity: Any) -> None:
        shutil.rmtree(workspace)
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        workspace.symlink_to(decoy, target_is_directory=True)
        raise OSError("injected removal failure after partial work")

    monkeypatch.setattr(
        runner_module, "_remove_unverified_workspace", replace_then_raise
    )
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    caplog.set_level(logging.ERROR, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    # the replacement survives and the record names its actual state
    assert workspace.is_symlink()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.reason is not None
    assert "final state" in manifest.workspace_disposition.reason
    assert "not the original directory" in manifest.workspace_disposition.reason
    assert any(
        "cannot securely keep or remove agent workspace" in record.message
        for record in caplog.records
    )


def test_foreign_owned_workspace_is_never_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A foreign-owned replacement is refused removal and recorded truthfully."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    # simulate a foreign-owned replacement at resolve time only
    monkeypatch.setattr(runner_module.os, "geteuid", lambda: 424242)
    caplog.set_level(logging.ERROR, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    assert workspace.is_dir()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.reason is not None
    assert "not owned by the effective user" in manifest.workspace_disposition.reason
    assert any(
        "cannot securely keep or remove agent workspace" in record.message
        for record in caplog.records
    )


def test_fchmod_failure_classifies_unsecurable_and_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed fd-based mode restore never claims a secured workspace."""
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    result = runner(_request())
    assert isinstance(result.journal, runner_module.AgentRunLifecycleHandle)
    workspace = _retained_workspaces(tmp_path / "isolated")[0]
    os.chmod(workspace, 0o777)

    def failing_fchmod(descriptor: int, mode: int) -> None:
        raise OSError("injected fchmod failure")

    monkeypatch.setattr(runner_module.os, "fchmod", failing_fchmod)
    caplog.set_level(logging.WARNING, logger="epubforge.agent_runner")

    result.journal.mark_rejected("ChapterSegmentationError", "semantic rejection")

    # the unsecurable workspace was removed, never claimed retained
    assert not workspace.exists()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "rejected"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "deleted"
    assert any(
        "removed unsecurable agent workspace" in record.message
        for record in caplog.records
    )


def test_insecure_workspace_under_cleanup_error_is_never_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A possibly-live process keeps the workspace even when unsecurable."""
    captured: dict[str, int] = {}
    monkeypatch.setattr(
        journal_module.AgentRunJournal,
        "process_pid",
        _fail_process_pid_for(captured, "agent"),
    )
    monkeypatch.setattr(runner_module, "_signal_process_group", lambda *a, **k: True)
    monkeypatch.setattr(runner_module, "_process_group_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        runner_module,
        "_inspect_workspace_for_retention",
        lambda workspace: (
            "insecure",
            "injected re-secure failure",
            _workspace_identity(workspace),
        ),
    )
    runner = _runner(
        tmp_path,
        journal_root=tmp_path / "agent-runs",
        workspace_retention="on-failure",
    )
    caplog.set_level(logging.ERROR, logger="epubforge.agent_runner")

    with pytest.raises(AgentCleanupError, match="cannot guarantee cleanup"):
        runner(_request("inspect"))

    retained = _retained_workspaces(tmp_path / "isolated")
    assert len(retained) == 1
    assert retained[0].is_dir()
    manifest = read_agent_run_manifest(
        _single_run_dir(tmp_path / "agent-runs") / "manifest.json"
    )
    assert manifest.final_status == "failed"
    assert manifest.workspace_disposition is not None
    assert manifest.workspace_disposition.disposition == "retained"
    assert manifest.workspace_disposition.reason is not None
    assert "injected re-secure failure" in manifest.workspace_disposition.reason
    assert any("possibly-live process" in record.message for record in caplog.records)
