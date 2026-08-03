"""Run the packaged book editor in a restricted OpenCode workspace."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import logging
import os
from pathlib import Path, PurePosixPath
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from types import MappingProxyType
from typing import Any, Protocol


BOOK_EDITOR_AGENT_NAME = "book-editor"
BOOK_EDITOR_MODEL = "openai/gpt-5.6-luna"
BOOK_EDITOR_VARIANT = "medium"
BOOK_EDITOR_PROMPT = "Read TASK.md and complete the requested book-editing task."

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_TERMINATE_GRACE_SECONDS = 3.0
DEFAULT_MAX_STDOUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 1024 * 1024
DEFAULT_MAX_LINE_BYTES = 1024 * 1024
DEFAULT_MAX_INPUT_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_INPUT_FILES = 20_000
DEFAULT_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_CONFIG_PROBE_TIMEOUT_SECONDS = 10.0
_MAX_CONFIG_PROBE_STDOUT_BYTES = 4 * 1024 * 1024
_MAX_CONFIG_PROBE_STDERR_BYTES = 256 * 1024

_RUNNER_CONTRACT_VERSION = 1
_READ_CHUNK_BYTES = 64 * 1024
_SESSION_KEYS = frozenset({"sessionID", "sessionId", "session_id"})
_OPENCODE_PROVIDER_CONFIG_ENV_KEYS = frozenset(
    {
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_PATH",
        "OPENCODE_CONFIG_CONTENT",
    }
)
_OPENCODE_CONTROLLED_ENV_KEYS = frozenset(
    {
        *_OPENCODE_PROVIDER_CONFIG_ENV_KEYS,
        "OPENCODE_PERMISSION",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
    }
)
_log = logging.getLogger(__name__)

_PERMISSIONS: dict[str, str] = {
    "*": "deny",
    "read": "allow",
    "list": "allow",
    "glob": "allow",
    "grep": "allow",
    "edit": "allow",
    "bash": "deny",
    "task": "deny",
    "external_directory": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "skill": "deny",
    "question": "deny",
    "lsp": "deny",
    "todowrite": "deny",
}


def _controlled_opencode_environment() -> dict[str, str]:
    return {
        "OPENCODE_PERMISSION": json.dumps(
            _PERMISSIONS, ensure_ascii=True, separators=(",", ":")
        ),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    }


def _agent_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    environment = dict(inherited)
    for key in _OPENCODE_CONTROLLED_ENV_KEYS:
        environment.pop(key, None)
    environment.update(_controlled_opencode_environment())
    return environment


def _provider_probe_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    environment = _agent_environment(inherited)
    for key in _OPENCODE_PROVIDER_CONFIG_ENV_KEYS:
        value = inherited.get(key)
        if value is not None:
            environment[key] = value
    return environment


class AgentRunnerError(RuntimeError):
    """Raised when an isolated agent run cannot produce a trusted output."""


class AgentExecutionError(AgentRunnerError):
    """Raised when OpenCode fails, times out, or violates output limits."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        session_id: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.session_id = session_id
        super().__init__(message)


@dataclass(frozen=True)
class AgentIdentity:
    """Stable identity used by stage freshness and publication metadata."""

    name: str
    model: str
    variant: str
    prompt_sha256: str
    fingerprint: str

    def __post_init__(self) -> None:
        for label, value in (
            ("agent name", self.name),
            ("agent model", self.model),
            ("agent variant", self.variant),
        ):
            if not value or "\x00" in value:
                raise ValueError(f"{label} must be a non-empty safe string")
        for label, value in (
            ("agent prompt hash", self.prompt_sha256),
            ("agent fingerprint", self.fingerprint),
        ):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"{label} must be a lowercase SHA-256")


@dataclass(frozen=True)
class AgentRunRequest:
    """Complete isolated input and the output files Python will accept."""

    title: str
    prompt: str
    files: Mapping[str, bytes]
    output_limits: Mapping[str, int]
    forbidden_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class AgentRunResult:
    """Bounded output snapshots collected before workspace cleanup."""

    outputs: Mapping[str, bytes]
    session_id: str | None = None


class AgentRunner(Protocol):
    """Callable boundary used by production OpenCode runs and test fakes."""

    identity: AgentIdentity

    def __call__(self, request: AgentRunRequest) -> AgentRunResult: ...


@dataclass(frozen=True)
class _AgentDefinition:
    description: str
    prompt: str
    markdown_sha256: str
    identity: AgentIdentity


@dataclass
class _BoundedBytes:
    label: str
    maximum: int
    maximum_line: int
    data: bytearray
    current_line: int = 0

    def add(self, chunk: bytes) -> None:
        if len(self.data) + len(chunk) > self.maximum:
            raise AgentExecutionError(f"OpenCode {self.label} exceeded its byte limit")
        parts = chunk.split(b"\n")
        if len(parts) == 1:
            self.current_line += len(parts[0])
            self._check_line()
        else:
            self.current_line += len(parts[0])
            self._check_line()
            for part in parts[1:-1]:
                if len(part) > self.maximum_line:
                    raise AgentExecutionError(
                        f"OpenCode {self.label} exceeded its line limit"
                    )
            self.current_line = len(parts[-1])
            self._check_line()
        self.data.extend(chunk)

    def _check_line(self) -> None:
        if self.current_line > self.maximum_line:
            raise AgentExecutionError(f"OpenCode {self.label} exceeded its line limit")


class OpenCodeAgentRunner:
    """Invoke OpenCode without exposing the repository or book workspace."""

    def __init__(
        self,
        *,
        executable: str | os.PathLike[str] = "opencode",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
        max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
        max_input_files: int = DEFAULT_MAX_INPUT_FILES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        temp_root: Path | None = None,
    ) -> None:
        if timeout_seconds <= 0 or terminate_grace_seconds <= 0:
            raise ValueError("agent timeouts must be positive")
        for label, value in (
            ("stdout byte limit", max_stdout_bytes),
            ("stderr byte limit", max_stderr_bytes),
            ("line byte limit", max_line_bytes),
            ("input byte limit", max_input_bytes),
            ("input file limit", max_input_files),
            ("output byte limit", max_output_bytes),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be positive")
        executable_text = os.fspath(executable)
        if not executable_text or "\x00" in executable_text:
            raise ValueError("OpenCode executable must be a safe non-empty path")

        definition = _load_agent_definition()
        self.identity = definition.identity
        self._definition = definition
        self._executable = executable_text
        self._timeout_seconds = timeout_seconds
        self._terminate_grace_seconds = terminate_grace_seconds
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._max_line_bytes = max_line_bytes
        self._max_input_bytes = max_input_bytes
        self._max_input_files = max_input_files
        self._max_output_bytes = max_output_bytes
        self._temp_root = temp_root

    def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        normalized_files, normalized_outputs = self._validate_request(request)
        workspace = self._create_workspace(request.forbidden_roots)
        active_error: BaseException | None = None
        try:
            self._write_inputs(workspace, normalized_files, normalized_outputs)
            stdout, stderr = self._invoke(workspace, request)
            session_id = _parse_session_id(stdout)
            outputs: dict[str, bytes] = {}
            total_output_bytes = 0
            for relative, maximum in normalized_outputs.items():
                remaining = self._max_output_bytes - total_output_bytes
                if remaining <= 0:
                    raise AgentRunnerError("agent outputs exceed the total byte limit")
                try:
                    data = _read_regular_output(
                        workspace,
                        relative,
                        max_bytes=min(maximum, remaining),
                    )
                except AgentRunnerError as exc:
                    if remaining < maximum and "exceeds its byte limit" in str(exc):
                        raise AgentRunnerError(
                            "agent outputs exceed the total byte limit"
                        ) from exc
                    raise
                total_output_bytes += len(data)
                if total_output_bytes > self._max_output_bytes:
                    raise AgentRunnerError("agent outputs exceed the total byte limit")
                outputs[relative] = data
            if stderr:
                _log.debug("OpenCode book-editor emitted %d stderr bytes", len(stderr))
            return AgentRunResult(
                outputs=MappingProxyType(outputs),
                session_id=session_id,
            )
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            try:
                _remove_workspace(workspace)
            except Exception as cleanup_error:
                message = f"cannot remove isolated agent workspace: {workspace}"
                error = AgentRunnerError(message)
                if active_error is not None:
                    error.add_note(
                        f"The agent run also failed with {type(active_error).__name__}."
                    )
                raise error from cleanup_error

    def _validate_request(
        self, request: AgentRunRequest
    ) -> tuple[dict[str, bytes], dict[str, int]]:
        if (
            not isinstance(request.title, str)
            or not request.title.strip()
            or len(request.title) > 200
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in request.title)
        ):
            raise AgentRunnerError(
                "agent title must be a safe string of at most 200 chars"
            )
        if (
            not isinstance(request.prompt, str)
            or not request.prompt.strip()
            or len(request.prompt.encode("utf-8")) > 16 * 1024
            or "\x00" in request.prompt
        ):
            raise AgentRunnerError("agent prompt must be a safe bounded string")
        if not request.files or len(request.files) > self._max_input_files:
            raise AgentRunnerError("agent input file count is outside its limit")
        if not request.output_limits:
            raise AgentRunnerError("agent request must declare at least one output")
        if len(set(request.files) | set(request.output_limits)) > self._max_input_files:
            raise AgentRunnerError("agent workspace file count is outside its limit")

        normalized_files: dict[str, bytes] = {}
        total_bytes = 0
        for raw_path, raw_data in request.files.items():
            relative = _safe_relative_path(raw_path, label="agent input")
            if PurePosixPath(relative).suffix.lower() == ".pdf":
                raise AgentRunnerError("PDF files cannot enter an agent workspace")
            if not isinstance(raw_data, bytes):
                raise AgentRunnerError(f"agent input must be bytes: {relative}")
            if raw_data.startswith(b"%PDF-"):
                raise AgentRunnerError("PDF content cannot enter an agent workspace")
            total_bytes += len(raw_data)
            if total_bytes > self._max_input_bytes:
                raise AgentRunnerError("agent inputs exceed the total byte limit")
            if relative in normalized_files:
                raise AgentRunnerError(f"duplicate agent input path: {relative}")
            normalized_files[relative] = raw_data

        normalized_outputs: dict[str, int] = {}
        for raw_path, maximum in request.output_limits.items():
            relative = _safe_relative_path(raw_path, label="agent output")
            if len(PurePosixPath(relative).parts) != 1:
                raise AgentRunnerError(
                    f"agent output must be a direct workspace child: {relative}"
                )
            if (
                not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or maximum <= 0
            ):
                raise AgentRunnerError(f"invalid output byte limit: {relative}")
            normalized_outputs[relative] = maximum

        all_paths = set(normalized_files) | set(normalized_outputs)
        for relative in all_paths:
            path = PurePosixPath(relative)
            for parent in path.parents:
                if parent == PurePosixPath("."):
                    continue
                if parent.as_posix() in all_paths:
                    raise AgentRunnerError(
                        "agent file paths contain a file/directory clash"
                    )
        return normalized_files, normalized_outputs

    def _create_workspace(self, forbidden_roots: tuple[Path, ...]) -> Path:
        parent = _safe_temp_parent(self._temp_root)
        workspace = Path(tempfile.mkdtemp(prefix="epubforge-agent-", dir=parent))
        try:
            os.chmod(workspace, 0o700)
            mode = stat.S_IMODE(os.lstat(workspace).st_mode)
            if mode != 0o700 or workspace.is_symlink():
                raise AgentRunnerError("isolated agent workspace is not mode 0700")
            roots = tuple(_resolved_root(root) for root in forbidden_roots)
            repository_root = _repository_root(Path.cwd())
            if repository_root is not None:
                roots += (repository_root,)
            resolved_workspace = workspace.resolve(strict=True)
            if any(_is_within(resolved_workspace, root) for root in roots):
                raise AgentRunnerError(
                    "isolated agent workspace is inside a forbidden project directory"
                )
            return workspace
        except BaseException:
            _remove_workspace(workspace)
            raise

    def _write_inputs(
        self,
        workspace: Path,
        files: Mapping[str, bytes],
        outputs: Mapping[str, int],
    ) -> None:
        for relative in outputs:
            output_path = workspace / PurePosixPath(relative)
            output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for relative, data in files.items():
            path = workspace / PurePosixPath(relative)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_new_file(path, data)
            if relative not in outputs:
                os.chmod(path, 0o400)

    def _invoke(self, workspace: Path, request: AgentRunRequest) -> tuple[bytes, bytes]:
        argv = [
            self._executable,
            "run",
            "--pure",
            "--agent",
            self.identity.name,
            "--dir",
            str(workspace),
            "--format",
            "json",
            "--title",
            request.title,
            "--variant",
            self.identity.variant,
            request.prompt,
        ]
        inherited_environment = os.environ.copy()
        environment = _agent_environment(inherited_environment)
        provider_probe_environment = _provider_probe_environment(inherited_environment)
        provider_config = self._resolve_provider_config(
            workspace.parent, provider_probe_environment
        )
        config_home: Path | None = None
        try:
            config_home = Path(
                tempfile.mkdtemp(
                    prefix="epubforge-opencode-config-", dir=workspace.parent
                )
            )
            os.chmod(config_home, 0o700)
            config_dir = config_home / "opencode"
            config_dir.mkdir(mode=0o700)
            os.chmod(config_dir, 0o700)
            config_path = config_dir / "opencode.json"
            config_data = _opencode_config_content(
                self._definition, provider_config
            ).encode("utf-8")
            _write_new_file(config_path, config_data)
        except OSError as exc:
            raise AgentExecutionError(
                "cannot create private OpenCode config home"
            ) from exc
        except AgentRunnerError as exc:
            raise AgentExecutionError(
                "cannot create private OpenCode config file"
            ) from exc

        try:
            environment["XDG_CONFIG_HOME"] = str(config_home)
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=workspace,
                    env=environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                raise AgentExecutionError(f"cannot start OpenCode: {exc}") from exc

            try:
                stdout, stderr = self._communicate_bounded(process)
            except BaseException:
                _terminate_process_group(process, self._terminate_grace_seconds)
                raise
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        finally:
            if config_home is not None:
                _remove_workspace(config_home)

        session_id = _parse_session_id(stdout)
        if process.returncode != 0:
            raise AgentExecutionError(
                f"OpenCode exited with status {process.returncode} "
                f"and emitted {len(stderr)} stderr bytes",
                returncode=process.returncode,
                session_id=session_id,
            )
        return stdout, stderr

    def _resolve_provider_config(
        self, parent: Path, environment: Mapping[str, str]
    ) -> dict[str, Any]:
        """Read only provider settings before entering the private config home."""
        try:
            probe_dir = Path(
                tempfile.mkdtemp(prefix="epubforge-opencode-probe-", dir=parent)
            )
            os.chmod(probe_dir, 0o700)
        except OSError as exc:
            raise AgentExecutionError(
                "cannot create neutral OpenCode config probe directory"
            ) from exc

        process: subprocess.Popen[bytes] | None = None
        try:
            try:
                process = subprocess.Popen(
                    [self._executable, "debug", "config", "--pure"],
                    cwd=probe_dir,
                    env=dict(environment),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                raise AgentExecutionError("cannot start OpenCode config probe") from exc

            try:
                stdout, _ = self._communicate_bounded(
                    process,
                    timeout_seconds=min(
                        self._timeout_seconds, _CONFIG_PROBE_TIMEOUT_SECONDS
                    ),
                    max_stdout_bytes=_MAX_CONFIG_PROBE_STDOUT_BYTES,
                    max_stderr_bytes=_MAX_CONFIG_PROBE_STDERR_BYTES,
                )
            except BaseException:
                _terminate_process_group(process, self._terminate_grace_seconds)
                raise AgentExecutionError("OpenCode config probe failed") from None
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

            if process.returncode != 0:
                raise AgentExecutionError("OpenCode config probe exited unsuccessfully")
            return _parse_provider_config(stdout)
        finally:
            try:
                _remove_workspace(probe_dir)
            except Exception as exc:
                raise AgentExecutionError(
                    "cannot remove neutral OpenCode config probe directory"
                ) from exc

    def _communicate_bounded(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
    ) -> tuple[bytes, bytes]:
        if process.stdout is None or process.stderr is None:
            raise AgentExecutionError("OpenCode output pipes are unavailable")
        collectors = {
            "stdout": _BoundedBytes(
                "stdout",
                max_stdout_bytes or self._max_stdout_bytes,
                self._max_line_bytes,
                bytearray(),
            ),
            "stderr": _BoundedBytes(
                "stderr",
                max_stderr_bytes or self._max_stderr_bytes,
                self._max_line_bytes,
                bytearray(),
            ),
        }
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        effective_timeout = timeout_seconds or self._timeout_seconds
        deadline = time.monotonic() + effective_timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentExecutionError(
                        f"OpenCode timed out after {effective_timeout:g}s"
                    )
                events = selector.select(min(remaining, 0.25))
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, _READ_CHUNK_BYTES)
                    except OSError as exc:
                        raise AgentExecutionError(
                            f"cannot read OpenCode {key.data}: {exc}"
                        ) from exc
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    collectors[str(key.data)].add(chunk)
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise AgentExecutionError(
                    f"OpenCode timed out after {effective_timeout:g}s"
                ) from exc
        finally:
            selector.close()
        return bytes(collectors["stdout"].data), bytes(collectors["stderr"].data)


def book_editor_identity() -> AgentIdentity:
    """Return the packaged identity without constructing a process runner."""

    return _load_agent_definition().identity


def _load_agent_definition() -> _AgentDefinition:
    try:
        markdown = (
            resources.files("epubforge")
            .joinpath("agents", "book-editor.md")
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError) as exc:
        raise AgentRunnerError("cannot load packaged book-editor agent") from exc
    if not markdown.startswith("---\n"):
        raise AgentRunnerError("packaged book-editor agent has no frontmatter")
    try:
        frontmatter, prompt = markdown[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise AgentRunnerError("packaged book-editor frontmatter is malformed") from exc
    metadata: dict[str, str] = {}
    permissions: dict[str, str] = {}
    section: str | None = None
    for line in frontmatter.splitlines():
        if not line or ":" not in line:
            raise AgentRunnerError("packaged book-editor frontmatter is malformed")
        if line.startswith("  "):
            if section != "permission":
                raise AgentRunnerError("packaged book-editor frontmatter is malformed")
            key, value = line.strip().split(":", 1)
            permissions[key.strip().strip("\"'")] = value.strip()
            continue
        if line[:1].isspace():
            raise AgentRunnerError("packaged book-editor frontmatter is malformed")
        key, value = line.split(":", 1)
        normalized_key = key.strip()
        normalized_value = value.strip()
        if normalized_key == "permission":
            if normalized_value:
                raise AgentRunnerError("packaged book-editor frontmatter is malformed")
            section = "permission"
            continue
        section = None
        metadata[normalized_key] = normalized_value
    expected = {
        "description",
        "mode",
        "model",
        "variant",
    }
    if set(metadata) != expected:
        raise AgentRunnerError("packaged book-editor frontmatter has unknown fields")
    if (
        metadata["mode"] != "primary"
        or metadata["model"] != BOOK_EDITOR_MODEL
        or metadata["variant"] != BOOK_EDITOR_VARIANT
        or not metadata["description"]
        or permissions != _PERMISSIONS
        or not prompt.strip()
    ):
        raise AgentRunnerError("packaged book-editor identity is invalid")

    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    markdown_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    fingerprint_payload = {
        "contract_version": _RUNNER_CONTRACT_VERSION,
        "name": BOOK_EDITOR_AGENT_NAME,
        "model": BOOK_EDITOR_MODEL,
        "variant": BOOK_EDITOR_VARIANT,
        "markdown_sha256": markdown_sha256,
        "permissions": _PERMISSIONS,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    identity = AgentIdentity(
        name=BOOK_EDITOR_AGENT_NAME,
        model=BOOK_EDITOR_MODEL,
        variant=BOOK_EDITOR_VARIANT,
        prompt_sha256=prompt_sha256,
        fingerprint=fingerprint,
    )
    return _AgentDefinition(
        description=metadata["description"],
        prompt=prompt,
        markdown_sha256=markdown_sha256,
        identity=identity,
    )


def _opencode_config_content(
    definition: _AgentDefinition,
    provider_config: Mapping[str, Any] | None = None,
) -> str:
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "permission": dict(_PERMISSIONS),
        "mcp": {},
        "plugin": [],
        "agent": {
            definition.identity.name: {
                "description": definition.description,
                "mode": "primary",
                "model": definition.identity.model,
                "variant": definition.identity.variant,
                "prompt": definition.prompt,
                "permission": dict(_PERMISSIONS),
            }
        },
    }
    if provider_config is not None:
        config.update(provider_config)
    return json.dumps(config, ensure_ascii=True, separators=(",", ":"))


def _parse_provider_config(stdout: bytes) -> dict[str, Any]:
    if not stdout:
        raise AgentExecutionError("OpenCode config probe returned no JSON")
    if len(stdout) > _MAX_CONFIG_PROBE_STDOUT_BYTES:
        raise AgentExecutionError("OpenCode config probe exceeded its byte limit")
    try:
        payload = json.loads(
            stdout.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise AgentExecutionError(
            "OpenCode config probe returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise AgentExecutionError("OpenCode config probe returned a non-object")

    provider_config: dict[str, Any] = {}
    if "provider" in payload:
        providers = payload["provider"]
        if not isinstance(providers, dict):
            raise AgentExecutionError(
                "OpenCode config probe returned invalid providers"
            )
        for name, settings in providers.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 256
                or "\x00" in name
                or not isinstance(settings, dict)
            ):
                raise AgentExecutionError(
                    "OpenCode config probe returned invalid provider settings"
                )
        provider_config["provider"] = providers

    for key in ("enabled_providers", "disabled_providers"):
        if key not in payload:
            continue
        providers = payload[key]
        if not isinstance(providers, list) or any(
            not isinstance(name, str) or not name or len(name) > 256 or "\x00" in name
            for name in providers
        ):
            raise AgentExecutionError(f"OpenCode config probe returned invalid {key}")
        provider_config[key] = providers
    return provider_config


def _safe_relative_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise AgentRunnerError(f"{label} path is unsafe")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise AgentRunnerError(f"{label} path is unsafe: {value!r}")
    return path.as_posix()


def _safe_temp_parent(configured: Path | None) -> Path:
    candidate = configured if configured is not None else Path(tempfile.gettempdir())
    resolved = Path(os.path.abspath(os.fspath(candidate.expanduser())))
    descriptor: int | None = None
    try:
        descriptor = _open_workspace_fd(resolved)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise AgentRunnerError(f"agent temp root is unavailable: {candidate}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise AgentRunnerError(
            f"agent temp root is not a regular directory: {resolved}"
        )
    return resolved


def _resolved_root(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError as exc:
        raise AgentRunnerError(f"cannot resolve forbidden agent root: {path}") from exc


def _repository_root(start: Path) -> Path | None:
    try:
        current = start.resolve(strict=True)
    except OSError:
        return None
    for candidate in (current, *current.parents):
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            return None
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_new_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise AgentRunnerError(f"cannot write isolated input: {path.name}")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as exc:
        raise AgentRunnerError(f"cannot write isolated input: {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_output(workspace: Path, relative: str, *, max_bytes: int) -> bytes:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
    ):
        raise AgentRunnerError("cannot safely open agent output on this platform")
    workspace_fd: int | None = None
    descriptor: int | None = None
    try:
        workspace_fd = _open_workspace_fd(workspace)
        descriptor = os.open(
            relative,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=workspace_fd,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AgentRunnerError(f"agent output is not a regular file: {relative}")
        if before.st_nlink != 1:
            raise AgentRunnerError(f"agent output has multiple hard links: {relative}")
        if before.st_size > max_bytes:
            raise AgentRunnerError(f"agent output exceeds its byte limit: {relative}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise AgentRunnerError(
                    f"agent output exceeds its byte limit: {relative}"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or total != before.st_size:
            raise AgentRunnerError(f"agent output changed while read: {relative}")
        return b"".join(chunks)
    except OSError as exc:
        if descriptor is None:
            raise AgentRunnerError(
                f"agent did not produce a regular {relative}"
            ) from exc
        raise AgentRunnerError(f"cannot read agent output: {relative}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if workspace_fd is not None:
            os.close(workspace_fd)


def _open_workspace_fd(workspace: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(workspace)))
    components = absolute.parts[1:]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if not components or any(part in {"", ".", ".."} for part in components):
        raise OSError("unsafe workspace path")
    directory_fd = os.open("/", flags)
    try:
        for component in components:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _terminate_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> None:
    process_group = process.pid
    group_found = _signal_process_group(process_group, signal.SIGTERM, "terminate")
    if process.poll() is None:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    if group_found:
        deadline = time.monotonic() + grace_seconds
        while _process_group_exists(process_group) and time.monotonic() < deadline:
            time.sleep(0.01)
        if _process_group_exists(process_group):
            _signal_process_group(process_group, signal.SIGKILL, "kill")
    if process.poll() is None:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            _log.warning("OpenCode process leader did not exit after SIGKILL")


def _signal_process_group(process_group: int, signal_number: int, action: str) -> bool:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        return False
    except OSError as exc:
        _log.warning("could not %s OpenCode process group: %s", action, exc)
        return False
    return True


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_session_id(stdout: bytes) -> str | None:
    if not stdout:
        return None
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentExecutionError("OpenCode JSON output is not UTF-8") from exc
    session_ids: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(
                line,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise AgentExecutionError(
                f"OpenCode emitted invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise AgentExecutionError("OpenCode JSON events must be objects")
        if event.get("type") == "error":
            raise AgentExecutionError("OpenCode emitted an error event")
        pending: list[object] = [event]
        visited = 0
        while pending:
            current = pending.pop()
            visited += 1
            if visited > 20_000:
                raise AgentExecutionError("OpenCode JSON event is too complex")
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in _SESSION_KEYS and isinstance(value, str):
                        if (
                            not value
                            or len(value) > 256
                            or any(ord(ch) < 32 for ch in value)
                            or not value.isprintable()
                        ):
                            raise AgentExecutionError(
                                "OpenCode emitted an invalid session ID"
                            )
                        session_ids.add(value)
                    elif isinstance(value, (dict, list)):
                        pending.append(value)
            elif isinstance(current, list):
                pending.extend(current)
    if len(session_ids) > 1:
        raise AgentExecutionError("OpenCode emitted conflicting session IDs")
    return next(iter(session_ids), None)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _remove_workspace(workspace: Path) -> None:
    if not workspace.exists() and not workspace.is_symlink():
        return

    def make_writable(function: Any, path: str, _error: object) -> None:
        os.chmod(path, 0o700)
        function(path)

    shutil.rmtree(workspace, onexc=make_writable)


__all__ = [
    "AgentExecutionError",
    "AgentIdentity",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRunner",
    "AgentRunnerError",
    "BOOK_EDITOR_AGENT_NAME",
    "BOOK_EDITOR_MODEL",
    "BOOK_EDITOR_PROMPT",
    "BOOK_EDITOR_VARIANT",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "OpenCodeAgentRunner",
    "book_editor_identity",
]
