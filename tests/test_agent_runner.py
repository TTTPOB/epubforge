"""Security and process-boundary tests for the OpenCode agent runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import pytest

from epubforge import agent_runner as runner_module
from epubforge.agent_runner import (
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
    runner = _runner(tmp_path)
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
    runner = _runner(tmp_path)
    runner._timeout_seconds = 0.1

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
