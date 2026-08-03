"""Run the packaged book editor in an isolated OpenCode workspace."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import errno
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
import sys
import tempfile
import time
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import TypeAdapter


BOOK_EDITOR_AGENT_NAME = "book-editor"
BOOK_EDITOR_MODEL = "openai/gpt-5.6-luna"
BOOK_EDITOR_VARIANT = "medium"
BOOK_EDITOR_PROMPT = "Read TASK.md and complete the requested book-editing task."

# Execution profiles select the agent permission map.  The default workflow
# runs both Stage 3 segmentation and Stage 5 revision under "development",
# which opens local bash inside the isolated workspace so the book editor
# can run the local Paddle page CLI.  "restricted" is an explicit opt-in
# reserved path that keeps the legacy command-denied profile; the default
# workflow does not require it.
EXECUTION_PROFILE_VALUES: tuple[Literal["development", "restricted"], ...] = (
    "development",
    "restricted",
)
_EXECUTION_PROFILE_ADAPTER: TypeAdapter[Literal["development", "restricted"]] = (
    TypeAdapter(Literal["development", "restricted"])
)
DEFAULT_EXECUTION_PROFILE: Literal["development", "restricted"] = "development"

# Workspace retention policies.  ``never`` (the direct-runner default) removes
# the isolated agent workspace on every outcome exactly as the legacy runner
# did.  ``on-failure`` (the production pipeline default) keeps the workspace
# after runner failures and semantic rejections and removes it only after an
# accepted publication.  ``always`` keeps it for accepted, rejected, and
# runner-failed runs.  Retained workspaces stay mode 0700, contain no source
# PDF, and are logged so users can remove them after debugging.
WORKSPACE_RETENTION_VALUES: tuple[Literal["never", "on-failure", "always"], ...] = (
    "never",
    "on-failure",
    "always",
)
_WORKSPACE_RETENTION_ADAPTER: TypeAdapter[Literal["never", "on-failure", "always"]] = (
    TypeAdapter(Literal["never", "on-failure", "always"])
)
DEFAULT_WORKSPACE_RETENTION: Literal["never", "on-failure", "always"] = "never"

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

_RUNNER_CONTRACT_VERSION = 2
_READ_CHUNK_BYTES = 64 * 1024
_SESSION_KEYS = frozenset({"sessionID", "sessionId", "session_id"})
_PROBE_LABEL = "config-probe"
_AGENT_LABEL = "agent"
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

# The packaged book-editor frontmatter declares this baseline.  Profiles only
# widen it (development additionally allows bash); the baseline stays the
# locked-down legacy contract.
_RESTRICTED_PERMISSIONS: dict[str, str] = {
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

_DEVELOPMENT_PERMISSIONS: dict[str, str] = {
    **_RESTRICTED_PERMISSIONS,
    # Local commands inside the isolated workspace, e.g. the Paddle page CLI.
    "bash": "allow",
}


def _permissions_for(execution_profile: str) -> dict[str, str]:
    if execution_profile == "development":
        return dict(_DEVELOPMENT_PERMISSIONS)
    if execution_profile == "restricted":
        return dict(_RESTRICTED_PERMISSIONS)
    raise ValueError(f"unsupported execution profile: {execution_profile!r}")


def _controlled_opencode_environment(
    permissions: Mapping[str, str],
) -> dict[str, str]:
    return {
        "OPENCODE_PERMISSION": json.dumps(
            permissions, ensure_ascii=True, separators=(",", ":")
        ),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    }


def _agent_environment(
    inherited: Mapping[str, str], permissions: Mapping[str, str]
) -> dict[str, str]:
    environment = dict(inherited)
    for key in _OPENCODE_CONTROLLED_ENV_KEYS:
        environment.pop(key, None)
    environment.update(_controlled_opencode_environment(permissions))
    return environment


def _provider_probe_environment(
    inherited: Mapping[str, str], permissions: Mapping[str, str]
) -> dict[str, str]:
    environment = _agent_environment(inherited, permissions)
    for key in _OPENCODE_PROVIDER_CONFIG_ENV_KEYS:
        value = inherited.get(key)
        if value is not None:
            environment[key] = value
    return environment


class AgentRunnerError(RuntimeError):
    """Raised when an isolated agent run cannot produce a trusted output."""


class AgentExecutionError(AgentRunnerError):
    """Raised when OpenCode fails, times out, or violates output limits."""

    # Set by _communicate_bounded when communication is interrupted: the
    # bounded stdout bytes accepted before the failure, so recovery parsing
    # reads only already-complete JSONL lines.
    partial_stdout: bytes | None = None

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        session_id: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.session_id = session_id
        # Set by _BoundedBytes.add when a byte or line limit rejects part of
        # one chunk: the accepted prefix stored before the raise, so stream
        # capture persists the exact bytes recovery parsing reads.
        self.accepted_prefix: bytes = b""
        super().__init__(message)


class AgentCleanupError(AgentRunnerError):
    """Raised when an OpenCode process group cannot be guaranteed dead.

    The process may still run and can access ``retained_paths``, so callers
    must not delete those directories and should surface them for manual
    cleanup.  The original run error is attached as the cause.
    """

    def __init__(self, message: str, *, retained_paths: tuple[Path, ...] = ()) -> None:
        self.retained_paths = tuple(retained_paths)
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


class AgentRunJournalHandle(Protocol):
    """Caller-facing marks on the durable record of one agent run.

    Stages record semantic validation and publication outcome without knowing
    journal internals; test fakes may attach a real journal or leave None.
    """

    def mark_accepted(self) -> None: ...

    def mark_rejected(self, error_type: str, message: str) -> None: ...


class AgentRunRecorder(AgentRunJournalHandle, Protocol):
    """Runner-facing journal operations implemented by AgentRunJournal."""

    def record_workspace(self, workspace: str | Path) -> None: ...

    def record_workspace_disposition(
        self,
        disposition: Literal["deleted", "retained"],
        *,
        reason: str | None = None,
    ) -> None: ...

    def record_input(self, name: str, size_bytes: int, sha256: str) -> None: ...

    def process_started(self, label: str, *, capture_streams: bool = False) -> None: ...

    def process_pid(self, label: str, pid: int) -> None: ...

    def write_chunk(self, label: str, stream: str, chunk: bytes) -> None: ...

    def process_finished(self, label: str, returncode: int | None) -> None: ...

    def session_parsed(
        self,
        session_id: str | None,
        *,
        mode: Literal["strict", "best-effort"],
    ) -> None: ...

    def capture_interrupted(
        self, label: str, error_type: str, message: str
    ) -> None: ...

    def record_output(self, name: str, size_bytes: int, sha256: str) -> None: ...

    def output_collection_complete(self) -> None: ...

    def output_collection_failed(self, error_type: str, message: str) -> None: ...

    def mark_runner_failed(self, error_type: str, message: str) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class AgentRunResult:
    """Bounded output snapshots collected before workspace cleanup.

    ``journal`` exposes the caller-facing marks on the durable run record.
    It is None when journaling is disabled under ``never`` retention.  With
    deferred workspace retention (``on-failure`` or ``always``) the journal
    is an :class:`AgentRunLifecycleHandle` that owns the workspace cleanup —
    including when no durable journal exists (``journal_root`` unset) — so
    the caller's accepted/rejected mark still resolves the disposition and
    performs the deletion where the policy requires it.
    """

    outputs: Mapping[str, bytes]
    session_id: str | None = None
    journal: AgentRunJournalHandle | None = None


class AgentRunLifecycleHandle(AgentRunJournalHandle):
    """Caller-facing journal handle that owns deferred workspace cleanup.

    ``OpenCodeAgentRunner`` returns this handle as ``AgentRunResult.journal``
    whenever its retention policy keeps the workspace alive past the runner
    call (``on-failure``: removed only after the caller marks the run
    accepted, kept after rejections; ``always``: kept for every outcome).
    The handle resolves the workspace disposition before delegating the
    accepted/rejected mark, so the terminal manifest never claims a deletion
    the handle did not carry out.  A handle that is never finalized leaves
    the workspace and a pending manifest disposition behind: a caller crash
    conservatively keeps evidence.  ``never``-policy runs never build this
    handle: the runner removes the workspace eagerly and records the
    deletion itself.  The journal may be None (journaling disabled); the
    handle still performs the policy's deletion on acceptance, and records
    nothing.
    """

    def __init__(
        self,
        journal: AgentRunRecorder | None,
        workspace: Path | None,
        workspace_retention: str,
    ) -> None:
        self._journal = journal
        self._workspace = workspace
        self._workspace_retention = workspace_retention
        self._resolved = False

    def mark_accepted(self) -> None:
        self._resolve("accepted")
        if self._journal is not None:
            self._journal.mark_accepted()

    def mark_rejected(self, error_type: str, message: str) -> None:
        self._resolve("rejected")
        if self._journal is not None:
            self._journal.mark_rejected(error_type, message)

    def _resolve(self, outcome: Literal["accepted", "rejected"]) -> None:
        if self._resolved:
            # The journal's own guard raises on a repeated terminal mark; the
            # workspace must not be touched twice.
            return
        self._resolved = True
        if self._workspace is None or self._workspace_retention == "never":
            return
        if self._workspace_retention == "on-failure" and outcome == "accepted":
            try:
                _remove_workspace(self._workspace)
            except Exception as exc:
                # Cleanup failure after a successful publication: keep the
                # evidence, record the truth with a bounded reason, and let
                # the run stay accepted — the durable accepted record must
                # never claim a deletion that did not happen.  The retained
                # path is still verified, re-secured, and logged.
                _resolve_retained_workspace(
                    self._journal,
                    self._workspace,
                    f"workspace cleanup failed after acceptance: {_bounded_text(exc)}",
                )
                return
            _record_deleted_disposition(self._journal)
            return
        if self._workspace_retention == "always":
            reason = "workspace retained by the 'always' retention policy"
        else:
            reason = (
                "workspace retained by the 'on-failure' retention policy"
                " after a rejected run"
            )
        _resolve_retained_workspace(self._journal, self._workspace, reason)


class AgentRunner(Protocol):
    """Callable boundary used by production OpenCode runs and test fakes."""

    identity: AgentIdentity

    def __call__(self, request: AgentRunRequest) -> AgentRunResult: ...


@dataclass(frozen=True)
class _AgentDefinition:
    description: str
    prompt: str
    markdown_sha256: str


@dataclass
class _BoundedBytes:
    """Collector that always accepts the maximal valid prefix of each chunk.

    ``add`` stores and returns the longest prefix of the incoming chunk that
    fits the total byte budget without containing an over-limit line, then
    raises ``AgentExecutionError`` if the rejected remainder is non-empty.
    The accepted prefix is persisted before the raise and carried back on the
    error so the caller can forward the identical bytes to stream capture;
    recovery parsing then reads only complete JSONL lines from that prefix.
    """

    label: str
    maximum: int
    maximum_line: int
    data: bytearray
    current_line: int = 0

    def add(self, chunk: bytes) -> bytes:
        """Store and return the maximal valid prefix of *chunk*.

        A single linear scan bounds the prefix by both the remaining total
        byte budget and the per-line limit, so a 64KiB chunk costs one pass
        regardless of how many bytes were accepted before.  Line accounting
        carries across chunks: ``current_line`` counts the bytes of the
        unfinished line, and a newline closes it.  On a byte-limit failure the
        prefix ends exactly at the remaining budget; on a line-limit failure
        it ends before the first byte that would push a line over the limit,
        so the stored line is never over-limit.  The prefix is always stored
        before the raise and the raised error carries it in
        ``accepted_prefix``.
        """
        budget = self.maximum - len(self.data)
        if budget <= 0:
            error = AgentExecutionError(
                f"OpenCode {self.label} exceeded its byte limit"
            )
            error.accepted_prefix = b""
            raise error
        limit = budget if len(chunk) > budget else len(chunk)
        line_bytes = self.current_line
        accepted = limit
        for index in range(limit):
            if chunk[index] == 0x0A:
                line_bytes = 0
            elif line_bytes + 1 > self.maximum_line:
                accepted = index
                break
            else:
                line_bytes += 1
        self.data.extend(chunk[:accepted])
        self.current_line = line_bytes
        if accepted < limit:
            error = AgentExecutionError(
                f"OpenCode {self.label} exceeded its line limit"
            )
            error.accepted_prefix = chunk[:accepted]
            raise error
        if limit < len(chunk):
            error = AgentExecutionError(
                f"OpenCode {self.label} exceeded its byte limit"
            )
            error.accepted_prefix = chunk[:accepted]
            raise error
        return chunk


class OpenCodeAgentRunner:
    """Invoke OpenCode without exposing the repository or book workspace.

    ``capture_raw_main_streams`` is a dangerous debug-only opt-in: when True
    the run journal persists the main agent's bounded raw stdout/stderr bytes
    to disk, which may echo inherited environment or provider credentials.
    Production pipeline stages and direct journal-enabled runners must leave
    it False; the config probe never captures streams regardless of this
    flag.  Session parsing, process outcome, output hashes, and semantic
    status stay durable in both modes.

    ``workspace_retention`` selects where the isolated workspace goes after
    the run: ``never`` (the direct-runner default) removes it on every
    outcome as the legacy runner did; ``on-failure`` (the production pipeline
    default) keeps it after runner failures and semantic rejections and
    removes it only once the caller marks the run accepted; ``always`` keeps
    it for every outcome.  Deferred policies return an
    :class:`AgentRunLifecycleHandle` as ``AgentRunResult.journal`` so the
    caller's accepted/rejected mark resolves the durable workspace
    disposition.  The config probe and private config home directories keep
    their own unconditional cleanup rules under every policy.
    """

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
        execution_profile: str = DEFAULT_EXECUTION_PROFILE,
        workspace_retention: str = DEFAULT_WORKSPACE_RETENTION,
        temp_root: Path | None = None,
        journal_root: Path | None = None,
        capture_raw_main_streams: bool = False,
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
        _validate_execution_profile(execution_profile)
        _validate_workspace_retention(workspace_retention)

        definition = _load_agent_definition()
        self.identity = _identity_for(definition, execution_profile)
        self._definition = definition
        self._execution_profile = execution_profile
        self._permissions = _permissions_for(execution_profile)
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
        self._workspace_retention = workspace_retention
        self._journal_root = journal_root
        self._capture_raw_main_streams = capture_raw_main_streams

    def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        normalized_files, normalized_outputs = self._validate_request(request)
        from epubforge.agent_journal import AgentRunJournalError

        try:
            journal = self._begin_journal(request, normalized_outputs)
        except AgentRunJournalError as exc:
            raise AgentRunnerError(f"agent run journaling failed: {exc}") from exc

        workspace: Path | None = None
        try:
            workspace = self._create_workspace(request.forbidden_roots)
            if journal is not None:
                journal.record_workspace(workspace)
            self._write_inputs(
                workspace, normalized_files, normalized_outputs, journal=journal
            )
            stdout, stderr = self._invoke(workspace, request, journal)
            session_id = _parse_session_id(stdout)
            outputs: dict[str, bytes] = {}
            total_output_bytes = 0
            try:
                for relative, maximum in normalized_outputs.items():
                    remaining = self._max_output_bytes - total_output_bytes
                    if remaining <= 0:
                        raise AgentRunnerError(
                            "agent outputs exceed the total byte limit"
                        )
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
                        raise AgentRunnerError(
                            "agent outputs exceed the total byte limit"
                        )
                    outputs[relative] = data
                    if journal is not None:
                        journal.record_output(
                            relative,
                            len(data),
                            hashlib.sha256(data).hexdigest(),
                        )
            except BaseException as exc:
                if journal is not None:
                    journal.output_collection_failed(type(exc).__name__, str(exc))
                raise
            if journal is not None:
                journal.output_collection_complete()
            if stderr:
                _log.debug("OpenCode book-editor emitted %d stderr bytes", len(stderr))
            result = AgentRunResult(
                outputs=MappingProxyType(outputs),
                session_id=session_id,
                journal=journal,
            )
            if workspace is not None:
                if self._workspace_retention == "never":
                    # Clean up before returning: a cleanup failure on this
                    # otherwise-successful path is itself a run failure, so the
                    # journal must be finalized as failed before the cleanup
                    # error propagates.  This never leaves a started manifest
                    # or open run/root descriptors behind.
                    _remove_workspace_preserving(workspace, "isolated agent workspace")
                    if journal is not None:
                        journal.record_workspace_disposition("deleted")
                    workspace = None
                else:
                    # The workspace must survive the runner call: the caller
                    # resolves its disposition when it marks the run accepted
                    # or rejected through the returned lifecycle handle.  The
                    # path is logged now so a caller crash still leaves a
                    # discoverable record of where the workspace is.
                    _log.info(
                        "keeping agent workspace for deferred cleanup: %s"
                        " (%s retention policy)",
                        workspace,
                        self._workspace_retention,
                    )
                    result = AgentRunResult(
                        outputs=result.outputs,
                        session_id=session_id,
                        journal=AgentRunLifecycleHandle(
                            journal,
                            workspace,
                            self._workspace_retention,
                        ),
                    )
            return result
        except BaseException as exc:
            if workspace is None:
                if journal is not None:
                    # Workspace creation failed before any child ran: close out
                    # the run record without masking the workspace error.
                    _mark_run_failed_best_effort(journal, exc)
            else:
                # The disposition resolution runs with or without a journal:
                # retained paths are always logged so direct callers can find
                # them, and journal-less runs still record nothing.  The
                # failed mark itself is a no-op without a journal.
                self._resolve_workspace_before_failure(journal, workspace, exc)
                workspace = None
                _mark_runner_failed_preserving(journal, exc)
            raise
        finally:
            if workspace is not None and self._workspace_retention == "never":
                _remove_workspace_preserving(workspace, "isolated agent workspace")

    def _resolve_workspace_before_failure(
        self,
        journal: AgentRunRecorder | None,
        workspace: Path,
        error: BaseException,
    ) -> None:
        """Resolve the workspace disposition before a failed run is finalized.

        ``never`` retention removes the workspace here rather than in the
        ``finally`` so the recorded disposition always matches reality: a
        removal failure leaves the directory and resolves the retention
        truthfully, never claiming ``deleted`` for a path that still exists.
        ``on-failure`` and ``always`` keep the workspace as evidence and
        resolve the retention through the shared verify/re-secure/fallback
        path.  A possibly-live process (``AgentCleanupError`` retaining the
        workspace) keeps the directory: verification and re-securing still
        run, but the path is never removed.  The config home and probe
        directory keep their own unconditional cleanup rules: this policy
        never applies to them.  The resolution runs with or without a
        journal; retained paths are always logged.
        """
        if self._workspace_retention == "never":
            _remove_workspace_preserving(workspace, "isolated agent workspace")
            if workspace.exists() or workspace.is_symlink():
                active_error = sys.exc_info()[1]
                if (
                    isinstance(active_error, AgentCleanupError)
                    and workspace in active_error.retained_paths
                ):
                    _resolve_retained_workspace(
                        journal,
                        workspace,
                        "workspace kept because the agent process group could"
                        " not be guaranteed dead",
                        allow_removal=False,
                    )
                else:
                    _resolve_retained_workspace(
                        journal,
                        workspace,
                        "workspace removal failed; kept for manual cleanup",
                    )
                return
            _record_deleted_disposition(journal)
            return
        if isinstance(error, AgentCleanupError) and workspace in error.retained_paths:
            _resolve_retained_workspace(
                journal,
                workspace,
                "workspace kept because the agent process group could not be"
                " guaranteed dead",
                allow_removal=False,
            )
            return
        _resolve_retained_workspace(
            journal,
            workspace,
            f"workspace retained by the {self._workspace_retention!r} retention"
            " policy after a runner failure",
        )

    def _begin_journal(
        self, request: AgentRunRequest, normalized_outputs: Mapping[str, int]
    ) -> AgentRunRecorder | None:
        if self._journal_root is None:
            return None
        from epubforge.agent_journal import create_agent_run_journal

        return create_agent_run_journal(
            self._journal_root,
            title=request.title,
            identity=self.identity,
            execution_profile=self._execution_profile,
            declared_outputs=normalized_outputs,
        )

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
            _remove_workspace_preserving(workspace, "isolated agent workspace")
            raise

    def _write_inputs(
        self,
        workspace: Path,
        files: Mapping[str, bytes],
        outputs: Mapping[str, int],
        *,
        journal: AgentRunRecorder | None = None,
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
            if journal is not None:
                journal.record_input(
                    relative, len(data), hashlib.sha256(data).hexdigest()
                )

    def _invoke(
        self,
        workspace: Path,
        request: AgentRunRequest,
        journal: AgentRunRecorder | None,
    ) -> tuple[bytes, bytes]:
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
        environment = _agent_environment(inherited_environment, self._permissions)
        provider_probe_environment = _provider_probe_environment(
            inherited_environment, self._permissions
        )
        provider_config = self._resolve_provider_config(
            workspace.parent, provider_probe_environment, journal
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
                self._definition, provider_config, self._permissions
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
            if journal is not None:
                # Record the attempt before Popen: the process entry never
                # claims a PID until process_pid runs after a live child.
                # Raw main streams are persisted only under the dangerous
                # debug opt-in; the safe default keeps them in memory.
                journal.process_started(
                    _AGENT_LABEL, capture_streams=self._capture_raw_main_streams
                )
            try:
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
            except BaseException:
                # Popen failed: no child exists, so there is no process group
                # to terminate; record the failed attempt and let the caller
                # mark the run failed.
                _record_process_finished(journal, _AGENT_LABEL, None)
                raise
            with _child_process_guard(
                process,
                grace_seconds=self._terminate_grace_seconds,
                journal=journal,
                label=_AGENT_LABEL,
                retain_paths=(config_home, workspace),
            ) as record_finish:
                if journal is not None:
                    journal.process_pid(_AGENT_LABEL, process.pid)
                try:
                    stdout, stderr = self._communicate_bounded(
                        process,
                        journal=journal,
                        label=_AGENT_LABEL,
                        capture_streams=self._capture_raw_main_streams,
                    )
                except AgentExecutionError as exc:
                    # Timeout, byte/line limit, reader failure, or any other
                    # interrupted main-agent communication: persist bounded
                    # recovery facts (truncated capture plus one best-effort
                    # session ID) without ever masking the original error.
                    _record_interrupted_capture(journal, exc, exc.partial_stdout or b"")
                    raise
                record_finish()
                session_id = _parse_session_id(stdout)
                if journal is not None:
                    journal.session_parsed(session_id, mode="strict")
                if process.returncode != 0:
                    raise AgentExecutionError(
                        f"OpenCode exited with status {process.returncode} "
                        f"and emitted {len(stderr)} stderr bytes",
                        returncode=process.returncode,
                        session_id=session_id,
                    )
                return stdout, stderr
        finally:
            if config_home is not None:
                _remove_workspace_preserving(
                    config_home, "private OpenCode config home"
                )

    def _resolve_provider_config(
        self,
        parent: Path,
        environment: Mapping[str, str],
        journal: AgentRunRecorder | None,
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
            if journal is not None:
                # Record the attempt before Popen: the process entry never
                # claims a PID until process_pid runs after a live child.
                journal.process_started(_PROBE_LABEL, capture_streams=False)
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
                    raise AgentExecutionError(
                        "cannot start OpenCode config probe"
                    ) from exc
            except BaseException:
                # Popen failed: no child exists, so there is no process group
                # to terminate; record the failed attempt and let the caller
                # mark the run failed.
                _record_process_finished(journal, _PROBE_LABEL, None)
                raise
            with _child_process_guard(
                process,
                grace_seconds=self._terminate_grace_seconds,
                journal=journal,
                label=_PROBE_LABEL,
                retain_paths=(probe_dir,),
            ) as record_finish:
                if journal is not None:
                    journal.process_pid(_PROBE_LABEL, process.pid)
                try:
                    # The probe's raw streams may contain provider credentials,
                    # so only its metadata is journaled; the bytes stay in
                    # memory for allowlist parsing and are never persisted.
                    # The probe keeps its own fixed bound: a tiny main-agent
                    # task timeout never shortens the independent config probe
                    # window, so a slow probe cannot masquerade as a fast
                    # task timeout.
                    stdout, _ = self._communicate_bounded(
                        process,
                        timeout_seconds=_CONFIG_PROBE_TIMEOUT_SECONDS,
                        max_stdout_bytes=_MAX_CONFIG_PROBE_STDOUT_BYTES,
                        max_stderr_bytes=_MAX_CONFIG_PROBE_STDERR_BYTES,
                        journal=journal,
                        label=_PROBE_LABEL,
                        capture_streams=False,
                    )
                except BaseException:
                    raise AgentExecutionError("OpenCode config probe failed") from None
                record_finish()
                if process.returncode != 0:
                    raise AgentExecutionError(
                        "OpenCode config probe exited unsuccessfully"
                    )
                return _parse_provider_config(stdout)
        finally:
            _remove_workspace_preserving(
                probe_dir,
                "neutral OpenCode config probe directory",
                error_type=AgentExecutionError,
            )

    def _communicate_bounded(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        journal: AgentRunRecorder | None = None,
        label: str = _AGENT_LABEL,
        capture_streams: bool = False,
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
                    accepted: bytes = b""
                    try:
                        accepted = collectors[str(key.data)].add(chunk)
                    except AgentExecutionError as exc:
                        # The collector stored the maximal valid prefix of
                        # *chunk* before raising; forward the identical
                        # prefix so the durable capture matches the accepted
                        # bytes recovery parsing reads.
                        accepted = exc.accepted_prefix
                        raise
                    finally:
                        if accepted and journal is not None and capture_streams:
                            journal.write_chunk(label, str(key.data), accepted)
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise AgentExecutionError(
                    f"OpenCode timed out after {effective_timeout:g}s"
                ) from exc
        except AgentExecutionError as exc:
            # Interrupted communication: expose the bounded stdout bytes
            # accepted before the failure so the caller can run recovery
            # parsing over already-complete JSONL lines only.
            exc.partial_stdout = bytes(collectors["stdout"].data)
            raise
        finally:
            selector.close()
        return bytes(collectors["stdout"].data), bytes(collectors["stderr"].data)


def book_editor_identity(
    execution_profile: str = DEFAULT_EXECUTION_PROFILE,
) -> AgentIdentity:
    """Return the packaged identity for one execution profile."""
    _validate_execution_profile(execution_profile)
    return _identity_for(_load_agent_definition(), execution_profile)


def _validate_execution_profile(execution_profile: str) -> None:
    try:
        _EXECUTION_PROFILE_ADAPTER.validate_python(execution_profile)
    except Exception as exc:
        raise ValueError(
            f"unsupported execution profile: {execution_profile!r}"
        ) from exc


def _validate_workspace_retention(workspace_retention: str) -> None:
    try:
        _WORKSPACE_RETENTION_ADAPTER.validate_python(workspace_retention)
    except Exception as exc:
        raise ValueError(
            f"unsupported workspace retention: {workspace_retention!r}"
        ) from exc


def _record_deleted_disposition(journal: AgentRunRecorder | None) -> None:
    """Record the workspace as deleted without masking any active error."""
    if journal is None:
        return
    try:
        journal.record_workspace_disposition("deleted")
    except BaseException:
        _log.exception("cannot record the deleted agent workspace disposition")


def _record_retained_disposition(journal: AgentRunRecorder | None, reason: str) -> None:
    """Record the workspace as retained with a bounded reason, best effort."""
    if journal is None:
        return
    try:
        journal.record_workspace_disposition("retained", reason=_bounded_text(reason))
    except BaseException:
        _log.exception("cannot record the retained agent workspace disposition")


def _path_absent(path: Path) -> bool:
    """Return whether *path* does not exist, without following symlinks."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _inspect_workspace_for_retention(
    workspace: Path,
) -> tuple[str, str, tuple[int, int] | None]:
    """No-follow fd-based inspection of a workspace the policy wants to keep.

    The path is opened with ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` and every
    check (directory type, effective-user ownership, root mode) runs against
    the opened descriptor: ``fstat`` for type, owner, and the ``(st_dev,
    st_ino)`` identity, ``fchmod`` for the mode restore, ``fstat`` again to
    confirm.  A symlink at the path is never followed and never chmodded.  On
    success the current path is re-opened no-follow and its identity is
    compared with the pinned descriptor: only a path that still holds the
    same directory inode is classified ``secure``.  If the path disappeared,
    became a symlink or non-directory, or now points to a different directory
    inode, the *current* path is classified truthfully and the original is
    never recorded as a secure retained workspace.  Descriptors are closed on
    every path.

    Returns ``(state, reason, original_identity)`` where ``state`` is:

    - ``"gone"``: the path does not exist, so retention must never be
      claimed (the agent deleted its cwd or cleanup partially removed it);
    - ``"secure"``: the current path still holds the original user-owned
      directory with root mode 0700 — the mode is restored when a caller
      mutated it;
    - ``"insecure"``: the current path cannot be kept safely; *reason*
      explains why (symlink, non-directory, foreign owner, failed mode
      restore, or a replacement directory).

    ``original_identity`` is the ``(st_dev, st_ino)`` of the directory that
    was at the path when the inspection opened it, or None when the path
    never held a verifiable directory (symlink, non-directory, or an
    unreadable path).
    """
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                workspace,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return "gone", "workspace no longer exists", None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return (
                    "insecure",
                    "workspace path is a symlink, not the original directory",
                    None,
                )
            if exc.errno == errno.ENOTDIR:
                return (
                    "insecure",
                    "workspace path is not the original directory",
                    None,
                )
            return (
                "insecure",
                f"workspace cannot be inspected: {_bounded_text(exc)}",
                None,
            )
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if not stat.S_ISDIR(info.st_mode):
            return "insecure", "workspace path is not the original directory", identity
        if info.st_uid != os.geteuid():
            return (
                "insecure",
                "workspace is not owned by the effective user",
                identity,
            )
        if stat.S_IMODE(info.st_mode) != 0o700:
            try:
                os.fchmod(descriptor, 0o700)
            except OSError as exc:
                return (
                    "insecure",
                    f"workspace mode cannot be restored to 0700: {_bounded_text(exc)}",
                    identity,
                )
            try:
                restored = os.fstat(descriptor)
            except OSError as exc:
                return (
                    "insecure",
                    f"workspace cannot be re-inspected after chmod: {_bounded_text(exc)}",
                    identity,
                )
            if (
                not stat.S_ISDIR(restored.st_mode)
                or stat.S_IMODE(restored.st_mode) != 0o700
            ):
                return (
                    "insecure",
                    "workspace mode is still not 0700 after re-securing",
                    identity,
                )
        # The descriptor pinned the original directory; prove the current path
        # still holds that same inode before any secure-retained claim.
        current_descriptor: int | None = None
        try:
            try:
                current_descriptor = os.open(
                    workspace,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                )
            except FileNotFoundError:
                return "gone", "workspace path no longer exists", identity
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    return (
                        "insecure",
                        "workspace path no longer holds the original directory",
                        identity,
                    )
                return (
                    "insecure",
                    f"workspace path cannot be re-inspected: {_bounded_text(exc)}",
                    identity,
                )
            current = os.fstat(current_descriptor)
            if (current.st_dev, current.st_ino) != identity:
                return (
                    "insecure",
                    "workspace path now points to a different directory",
                    identity,
                )
        finally:
            if current_descriptor is not None:
                try:
                    os.close(current_descriptor)
                except OSError:
                    pass
        return "secure", "", identity
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _remove_unverified_workspace(
    workspace: Path, original_identity: tuple[int, int] | None
) -> None:
    """Remove a workspace that failed retention verification.

    A symlink or plain file at the path is unlinked as a path entry — the
    target, if any, is never touched.  A directory is removed only when it is
    owned by the effective user AND its ``(st_dev, st_ino)`` identity matches
    *original_identity*: a replacement directory at the path may hold other
    data and is refused, as is any path not owned by the effective user.  A
    missing path is a successful no-op.  Any other failure raises.
    """
    try:
        info = os.lstat(workspace)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OSError(f"cannot inspect the unsecurable workspace: {exc}") from exc
    if info.st_uid != os.geteuid():
        raise OSError("refusing to remove a path not owned by the effective user")
    if not stat.S_ISDIR(info.st_mode):
        # unlink the symlink or plain-file entry itself; never the target
        os.unlink(workspace)
        return
    if original_identity is None or (info.st_dev, info.st_ino) != original_identity:
        raise OSError(
            "refusing to remove a directory not proven to be the original workspace"
        )
    _remove_workspace(workspace)


def _resolve_retained_workspace(
    journal: AgentRunRecorder | None,
    workspace: Path,
    reason: str,
    *,
    allow_removal: bool = True,
) -> None:
    """Verify, re-secure, record, and log one workspace the policy retains.

    The path is inspected through a pinned no-follow descriptor before any
    ``retained`` claim: a path that no longer exists (the agent deleted its
    cwd, or cleanup partially removed it) is recorded and logged as
    ``deleted`` — retention is never claimed for a path that is not there.
    The current path must still hold the original user-owned directory with
    root mode 0700 (identity compared by ``(st_dev, st_ino)``); a mutated
    mode is restored on the pinned descriptor.  A path that cannot be kept
    securely is removed instead, unless ``allow_removal`` is False (a
    possibly-live process may still access it under ``AgentCleanupError``):
    then a clear security error is logged and the truthful existing state is
    recorded with the bounded reason.  Removal never deletes a replacement
    directory not proven to be the original workspace, nor any path not owned
    by the effective user.  When the secure-delete fallback raises after
    partial work, the path is re-inspected and its actual final state
    (missing, retained directory, or a symlink/non-directory replacement) is
    recorded and logged — ``retained`` is never recorded solely because
    removal raised.  The log line is emitted even without a journal so direct
    callers can locate retained workspaces.
    """
    state, state_reason, identity = _inspect_workspace_for_retention(workspace)
    if state == "gone":
        _log.info(
            "agent workspace no longer exists; recording deleted: %s (%s)",
            workspace,
            _bounded_text(reason),
        )
        _record_deleted_disposition(journal)
        return
    if state == "secure":
        _log.info(
            "retaining agent workspace for manual cleanup: %s (%s)",
            workspace,
            _bounded_text(reason),
        )
        _record_retained_disposition(journal, reason)
        return
    if not allow_removal:
        _log.error(
            "cannot securely keep agent workspace %s under a possibly-live process: %s",
            workspace,
            state_reason,
        )
        _record_retained_disposition(
            journal, f"{state_reason}; {_bounded_text(reason)}"
        )
        return
    try:
        _remove_unverified_workspace(workspace, identity)
    except Exception as exc:
        # Never record retained solely because the removal raised: re-inspect
        # the path without following symlinks and classify its actual final
        # state before recording.  A path the partial removal actually took
        # with it is recorded deleted; a path still present (a retained
        # directory, or a symlink/non-directory replacement) is recorded
        # retained with the truthful bounded state reason.
        final_state, final_reason, _ = _inspect_workspace_for_retention(workspace)
        if final_state == "gone":
            _log.warning(
                "unsecurable agent workspace was removed despite a removal"
                " error: %s (%s)",
                workspace,
                _bounded_text(exc),
            )
            _record_deleted_disposition(journal)
            return
        if final_state == "secure":
            effective_reason = f"{state_reason}; removal failed: {_bounded_text(exc)}"
        else:
            effective_reason = (
                f"{state_reason}; removal failed: {_bounded_text(exc)};"
                f" final state: {final_reason}"
            )
        _log.error(
            "cannot securely keep or remove agent workspace %s: %s",
            workspace,
            effective_reason,
        )
        _record_retained_disposition(journal, effective_reason)
        return
    if not _path_absent(workspace):
        _log.error(
            "cannot fully remove agent workspace %s: %s",
            workspace,
            state_reason,
        )
        _record_retained_disposition(journal, f"{state_reason}; removal was incomplete")
        return
    _log.warning(
        "removed unsecurable agent workspace instead of retaining it: %s (%s)",
        workspace,
        state_reason,
    )
    _record_deleted_disposition(journal)


def _identity_for(
    definition: _AgentDefinition, execution_profile: str
) -> AgentIdentity:
    permissions = _permissions_for(execution_profile)
    prompt_sha256 = hashlib.sha256(definition.prompt.encode("utf-8")).hexdigest()
    fingerprint_payload = {
        "contract_version": _RUNNER_CONTRACT_VERSION,
        "name": BOOK_EDITOR_AGENT_NAME,
        "model": BOOK_EDITOR_MODEL,
        "variant": BOOK_EDITOR_VARIANT,
        "markdown_sha256": definition.markdown_sha256,
        "execution_profile": execution_profile,
        "permissions": permissions,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AgentIdentity(
        name=BOOK_EDITOR_AGENT_NAME,
        model=BOOK_EDITOR_MODEL,
        variant=BOOK_EDITOR_VARIANT,
        prompt_sha256=prompt_sha256,
        fingerprint=fingerprint,
    )


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
        or permissions != _RESTRICTED_PERMISSIONS
        or not prompt.strip()
    ):
        raise AgentRunnerError("packaged book-editor identity is invalid")

    markdown_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return _AgentDefinition(
        description=metadata["description"],
        prompt=prompt,
        markdown_sha256=markdown_sha256,
    )


def _opencode_config_content(
    definition: _AgentDefinition,
    provider_config: Mapping[str, Any] | None = None,
    permissions: Mapping[str, str] | None = None,
) -> str:
    effective_permissions = dict(
        permissions if permissions is not None else _RESTRICTED_PERMISSIONS
    )
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "permission": dict(effective_permissions),
        "mcp": {},
        "plugin": [],
        "agent": {
            BOOK_EDITOR_AGENT_NAME: {
                "description": definition.description,
                "mode": "primary",
                "model": BOOK_EDITOR_MODEL,
                "variant": BOOK_EDITOR_VARIANT,
                "prompt": definition.prompt,
                "permission": dict(effective_permissions),
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


def _record_process_finished(
    journal: AgentRunRecorder | None,
    label: str,
    returncode: int | None,
) -> None:
    """Best-effort finish recording that never masks the primary error."""
    if journal is None:
        return
    try:
        journal.process_finished(label, returncode)
    except BaseException:
        _log.exception("cannot record agent run process finish for %s", label)


@contextmanager
def _child_process_guard(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
    journal: AgentRunRecorder | None,
    label: str,
    retain_paths: tuple[Path, ...] = (),
) -> Iterator[Callable[[], None]]:
    """Guard every step that follows a successful Popen of one agent process.

    Once ``Popen`` returns a live child, every recorder call, communication
    step, parse, and the finish recording runs inside this guard: any
    ``BaseException`` terminates and reaps the whole process group and closes
    the output pipes, so a journaling failure can never orphan the child.

    The yielded ``record_finish`` callback is the normal-path finish record:
    it is strict and raises on journal failure, so a stream sync, close, or
    final metadata failure fails the run immediately instead of being
    swallowed.  On its own error path the guard records the finish best
    effort only, so cleanup never masks the primary error.  The shared
    ``finished`` flag guarantees exactly one recording attempt either way.

    When termination or reaping itself fails, the process may still be live
    and can access ``retain_paths``: the guard raises ``AgentCleanupError``
    with the original error as its cause and the retained paths attached, so
    outer cleanup never deletes a directory a possibly-live process can
    still reach.  The process is not recorded as finished: the journal keeps
    the truthful running entry and the run is marked failed by the caller.
    """
    finished = False

    def record_finish() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        if journal is not None:
            journal.process_finished(label, process.returncode)

    def record_finish_best_effort() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        if journal is not None:
            try:
                journal.process_finished(label, process.returncode)
            except BaseException:
                _log.exception("cannot record agent run process finish for %s", label)

    try:
        yield record_finish
    except BaseException as primary_error:
        try:
            _terminate_process_group(process, grace_seconds)
        except BaseException:
            _log.exception(
                "cannot guarantee termination of the %s process group", label
            )
            retained = tuple(retain_paths)
            if retained:
                _log.error(
                    "retaining paths for possibly-live %s process: %s",
                    label,
                    ", ".join(str(path) for path in retained),
                )
            raise AgentCleanupError(
                f"cannot guarantee cleanup of the {label} process group",
                retained_paths=retained,
            ) from primary_error
        record_finish_best_effort()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _bounded_text(value: object, limit: int = 2000) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _mark_runner_failed_preserving(
    journal: AgentRunRecorder | None, error: BaseException
) -> None:
    """Mark a run failed without ever masking the primary run error.

    A secondary journaling failure adds a bounded note and a log line to
    *error* so the original runner error still propagates.  When *error*
    itself is a journaling failure it is the primary failure and stays
    untouched.
    """
    if journal is None:
        return
    try:
        journal.mark_runner_failed(type(error).__name__, str(error))
    except BaseException as journal_error:
        from epubforge.agent_journal import AgentRunJournalError

        if isinstance(error, AgentRunJournalError):
            return
        _log.error("cannot record agent run failure: %s", journal_error)
        error.add_note(
            f"agent run journaling also failed: {_bounded_text(journal_error)}"
        )


def _mark_run_failed_best_effort(
    journal: AgentRunRecorder | None, error: BaseException
) -> None:
    """Mark a run failed when its workspace never existed.

    The run record must not stay ``started``, but journaling is only best
    effort here so the original workspace error always propagates; the
    journal is closed if the failed mark cannot be persisted.
    """
    if journal is None:
        return
    try:
        journal.mark_runner_failed(type(error).__name__, str(error))
    except BaseException:
        _log.exception("cannot record agent run failure")
        try:
            journal.close()
        except BaseException:
            pass


def _terminate_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> None:
    """Terminate and reap *process* and its whole group, or raise.

    Returns only when the leader is reaped and no process remains in the
    group.  Otherwise it raises a bounded ``AgentExecutionError`` stating
    that cleanup could not be guaranteed, so callers never proceed as if the
    process group were dead while a possibly-live process still runs.
    """
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
            pass
    # Give SIGKILL a bounded grace period to take effect and let the group
    # leader's reaper clear any lingering members before judging the outcome.
    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.01)
    leader_reaped = process.poll() is not None
    group_remaining = _process_group_exists(process_group)
    if not leader_reaped or group_remaining:
        raise AgentExecutionError(
            "cannot guarantee OpenCode process group cleanup: "
            f"leader_reaped={leader_reaped}, group_remaining={group_remaining}"
        )


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


def _is_valid_session_id(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 256
        and all(ord(ch) >= 32 for ch in value)
        and value.isprintable()
    )


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
                        if not _is_valid_session_id(value):
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


def _parse_session_id_best_effort(stdout: bytes) -> str | None:
    """Recover at most one session ID from already-accepted stdout bytes.

    Error-only recovery path for interrupted agent communication: never
    raises.  An incomplete trailing fragment and malformed complete lines are
    ignored; invalid session ID values are skipped; two distinct valid IDs
    yield None so nothing unverified is recorded.  Successful runs never use
    this function.
    """
    if not stdout:
        return None
    text = stdout.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if lines and not text.endswith("\n"):
        lines = lines[:-1]
    session_ids: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(
                line,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        pending: list[object] = [event]
        visited = 0
        while pending:
            current = pending.pop()
            visited += 1
            if visited > 20_000:
                break
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in _SESSION_KEYS and isinstance(value, str):
                        if _is_valid_session_id(value):
                            session_ids.add(value)
                    elif isinstance(value, (dict, list)):
                        pending.append(value)
            elif isinstance(current, list):
                pending.extend(current)
    if len(session_ids) > 1:
        return None
    return next(iter(session_ids), None)


def _record_interrupted_capture(
    journal: AgentRunRecorder | None,
    error: AgentExecutionError,
    partial_stdout: bytes,
) -> None:
    """Persist bounded recovery facts after interrupted agent communication.

    Runs only on the error path so the original timeout, limit, or reader
    error always propagates untouched.  The best-effort session parse reads
    only complete JSONL lines already accepted into the bounded collector;
    the capture is marked truncated with the bounded non-secret reason.
    Journaling failures here only log.
    """
    if journal is None:
        return
    session_id = _parse_session_id_best_effort(partial_stdout)
    try:
        journal.session_parsed(session_id, mode="best-effort")
    except BaseException:
        _log.exception("cannot record interrupted agent run session")
    from epubforge.agent_journal import AgentRunJournalError

    try:
        journal.capture_interrupted(_AGENT_LABEL, type(error).__name__, str(error))
    except AgentRunJournalError:
        # The safe default keeps raw streams in memory, so there is no stream
        # capture to mark truncated; the best-effort session record above
        # stays durable.
        _log.debug("no main agent stream capture to interrupt")
    except BaseException:
        _log.exception("cannot record interrupted agent run capture")


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


def _remove_workspace_preserving(
    path: Path,
    label: str,
    *,
    error_type: type[AgentRunnerError] = AgentRunnerError,
) -> None:
    """Remove *path* from a cleanup ``finally`` without masking primary errors.

    When the propagating error is an ``AgentCleanupError`` that retains
    *path*, the removal is skipped and the path is logged: a possibly-live
    process may still access it, and deleting it could destroy evidence of
    the run.  A removal failure with any other active error adds a bounded
    note and a log line so the timeout, parse, journal, or semantic error
    keeps propagating.  Without an active error the removal failure raises
    *error_type* normally.
    """
    active_error = sys.exc_info()[1]
    if (
        isinstance(active_error, AgentCleanupError)
        and path in active_error.retained_paths
    ):
        _log.error("retaining %s for possibly-live process: %s", label, path)
        return
    try:
        _remove_workspace(path)
    except Exception as cleanup_error:
        message = f"cannot remove {label}: {path}"
        if active_error is not None:
            _log.error("%s: %s", message, cleanup_error)
            active_error.add_note(f"{message}: {_bounded_text(cleanup_error)}")
            return
        raise error_type(message) from cleanup_error


__all__ = [
    "AgentCleanupError",
    "AgentExecutionError",
    "AgentIdentity",
    "AgentRunJournalHandle",
    "AgentRunLifecycleHandle",
    "AgentRunRecorder",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRunner",
    "AgentRunnerError",
    "BOOK_EDITOR_AGENT_NAME",
    "BOOK_EDITOR_MODEL",
    "BOOK_EDITOR_PROMPT",
    "BOOK_EDITOR_VARIANT",
    "DEFAULT_EXECUTION_PROFILE",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_WORKSPACE_RETENTION",
    "EXECUTION_PROFILE_VALUES",
    "OpenCodeAgentRunner",
    "WORKSPACE_RETENTION_VALUES",
    "book_editor_identity",
    "_permissions_for",
]
