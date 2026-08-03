"""Durable records for every OpenCode agent run.

Production pipeline stages journal each agent process into
``logs/agent-runs/<run-id>/``: an atomically replaced strict-versioned
``manifest.json`` plus, only for the main agent when the runner's dangerous
debug stream-capture opt-in is enabled, bounded raw stdout JSONL and stderr
bytes.  Raw main-agent streams are disabled by default: the development bash
profile can echo inherited environment or provider credentials into either
stream, so persisting them without an explicit caller opt-in risks credential
exposure.  Direct and test runners leave journaling disabled unless a run
root is configured.

The config probe runs before the agent and its raw streams may contain
provider credentials, so they are never captured and expose no opt-in: the
probe process records only non-sensitive metadata (timestamps, PID, return
code, and a generic result) with an explicit ``capture: "disabled"`` marker.

Session parsing, process outcome, output hashes, and the semantic
accepted/rejected status stay durable regardless of stream capture.  Callers
(Stage 3 and Stage 5) mark the run ``accepted`` after semantic validation and
atomic publication succeed, or ``rejected`` with a bounded error otherwise.
A failed run keeps its manifest and streams; nothing here depends on
published artifacts such as ``chapters.json`` or ``revision.json``.

Every terminal transition is two-phase: the journal first persists a pending
finalization intent (target status, attempt timestamp, and the bounded
validation or runner-error payload of the transition) and only then writes
the terminal manifest and marks the intent completed.  A failure or crash
between the two writes leaves a strictly readable non-terminal manifest that
carries the pending intent, so the last durable record never masquerades as
an ordinary running run when a finalization attempt was interrupted.

The manifest also carries a strict workspace disposition (``pending``,
``deleted``, ``retained``) with bounded reason and timestamps.  ``record_workspace``
starts the record as ``pending``; the runner or the lifecycle handle resolves
it to ``deleted`` or ``retained`` before finalizing the run, so a terminal
manifest never claims a disposition the caller did not carry out.  A run
finalized while its workspace disposition is still unresolved is resolved
conservatively from the filesystem: a still-existing workspace is recorded as
``retained`` (evidence is kept, never silently deleted), a vanished one as
``deleted``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from epubforge.agent_runner import AgentIdentity, AgentRunJournalHandle

AGENT_RUN_SCHEMA = "epubforge.agent-run"
AGENT_RUN_SCHEMA_VERSION = 2

_MANIFEST_NAME = "manifest.json"
_MAX_MESSAGE_CHARS = 2000
_MAX_ERROR_TYPE_CHARS = 200
_RUN_ID_ATTEMPTS = 20
_STREAM_LABELS = frozenset({"config-probe", "agent"})
_STREAM_NAMES = frozenset({"stdout", "stderr"})
_PROBE_LABEL = "config-probe"
_AGENT_LABEL = "agent"
# The exact run id shape produced by _new_run_id: %Y%m%d-%H%M%S plus an
# 8-byte hex token.  Strict readers constrain run_id to this generated
# format so a manifest can never smuggle a path-like id.
_RUN_ID_PATTERN = r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{16}$"
# Crash-left temporary manifest names: .manifest.json.<16 hex>.tmp
_INIT_TMP_RE = re.compile(r"^\." + re.escape(_MANIFEST_NAME) + r"\.[0-9a-f]{16}\.tmp$")

_log = logging.getLogger(__name__)


class AgentRunJournalError(RuntimeError):
    """Raised when a run journal cannot be created or updated durably."""


@dataclass
class _StreamState:
    descriptor: int
    digest: Any
    size: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(8)}"


def _bounded_message(value: object, limit: int = _MAX_MESSAGE_CHARS) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _process_result(returncode: int | None) -> str:
    if returncode is None or returncode < 0:
        return "error"
    if returncode == 0:
        return "success"
    return "nonzero"


def _stream_filename(label: str, stream: str) -> str:
    prefix = "" if label == _AGENT_LABEL else f"{label}."
    if stream == "stdout":
        return f"{prefix}stdout.jsonl"
    return f"{prefix}stderr.bin"


# The complete set of direct-child capture filenames the journal ever
# creates.  Strict readers accept only these basenames.
_STREAM_FILENAMES = frozenset(
    _stream_filename(label, stream)
    for label in _STREAM_LABELS
    for stream in _STREAM_NAMES
)


def _open_or_create_directory(path: Path) -> int:
    """Open *path* no-follow, creating missing components as mode 0700 dirs.

    Returns an fd for the final directory.  Symlinked or non-directory
    components fail the whole walk so journaling never follows attacker paths.
    Components the caller already created keep their own mode; directories
    created here are chmodded to 0700.  The final journal root must be owned
    by the effective user and is forced to mode 0700 (or the walk rejects the
    root); unrelated ancestor directories are left untouched.
    """
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise AgentRunJournalError(
            "agent run journaling requires POSIX O_NOFOLLOW support"
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    components = absolute.parts[1:]
    if not components or any(part in {"", ".", ".."} for part in components):
        raise AgentRunJournalError(f"agent run root is unsafe: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open("/", flags)
    except OSError as exc:
        raise AgentRunJournalError("cannot open the agent run root filesystem") from exc
    try:
        for component in components:
            try:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise AgentRunJournalError(
                        f"cannot create agent run root: {absolute}"
                    ) from exc
                try:
                    os.chmod(component, 0o700, dir_fd=directory_fd)
                except OSError as exc:
                    raise AgentRunJournalError(
                        f"cannot restrict agent run root: {absolute}"
                    ) from exc
                try:
                    next_fd = os.open(component, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise AgentRunJournalError(
                        f"cannot open agent run root: {absolute}"
                    ) from exc
            except OSError as exc:
                raise AgentRunJournalError(
                    f"cannot open agent run root: {absolute}"
                ) from exc
            info = os.fstat(next_fd)
            if not stat.S_ISDIR(info.st_mode):
                os.close(next_fd)
                raise AgentRunJournalError(
                    f"agent run root is not a directory: {absolute}"
                )
            os.close(directory_fd)
            directory_fd = next_fd
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid():
            # the outer handler releases directory_fd
            raise AgentRunJournalError(
                f"agent run root is not owned by the effective user: {absolute}"
            )
        if stat.S_IMODE(info.st_mode) != 0o700:
            try:
                os.fchmod(directory_fd, 0o700)
            except OSError as exc:
                # the outer handler releases directory_fd
                raise AgentRunJournalError(
                    f"cannot restrict agent run root: {absolute}"
                ) from exc
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _reserve_run_directory(root_fd: int, run_id: str) -> int:
    """Create one mode 0700 run directory under *root_fd* and open it."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        os.mkdir(run_id, 0o700, dir_fd=root_fd)
    except FileExistsError as exc:
        raise AgentRunJournalError(f"agent run id collision: {run_id}") from exc
    try:
        os.chmod(run_id, 0o700, dir_fd=root_fd)
        run_fd = os.open(run_id, flags, dir_fd=root_fd)
    except BaseException:
        try:
            os.rmdir(run_id, dir_fd=root_fd)
        except OSError:
            pass
        raise
    info = os.fstat(run_fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        os.close(run_fd)
        raise AgentRunJournalError(f"agent run directory is unsafe: {run_id}")
    return run_fd


def _remove_initialization_run(root_fd: int, run_id: str) -> None:
    """Best-effort removal of an initialization-only run directory.

    Called when journal construction fails after the run directory was
    reserved: the directory can hold at most the initial manifest and
    crash-left temporary manifest files.  Only those known names are
    unlinked (never following symlinks); anything else keeps the directory
    and surfaces as a bounded note so no orphan silently survives.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        run_fd = os.open(run_id, flags, dir_fd=root_fd)
    except OSError:
        run_fd = None
    if run_fd is not None:
        try:
            for entry in os.listdir(run_fd):
                if entry == _MANIFEST_NAME or _INIT_TMP_RE.match(entry):
                    try:
                        os.unlink(entry, dir_fd=run_fd)
                    except OSError:
                        pass
        except OSError as exc:
            raise AgentRunJournalError(
                f"cannot inspect initialization-only run directory {run_id}: {exc}"
            ) from exc
        finally:
            os.close(run_fd)
    try:
        os.rmdir(run_id, dir_fd=root_fd)
    except OSError as exc:
        raise AgentRunJournalError(
            f"cannot remove initialization-only run directory {run_id}: {exc}"
        ) from exc


def create_agent_run_journal(
    run_root: str | Path,
    *,
    title: str,
    identity: AgentIdentity,
    execution_profile: str,
    declared_outputs: Mapping[str, int],
) -> AgentRunJournal:
    """Create a unique run directory and its initial manifest.

    The run id stays unique under concurrency; collisions retry with a fresh
    random id.  Any failure here raises :class:`AgentRunJournalError` so the
    caller treats journal setup as a hard run failure.
    """
    root_fd = _open_or_create_directory(Path(run_root))
    try:
        last_error: BaseException | None = None
        for _ in range(_RUN_ID_ATTEMPTS):
            run_id = _new_run_id()
            try:
                run_fd = _reserve_run_directory(root_fd, run_id)
            except AgentRunJournalError as exc:
                if "collision" not in str(exc):
                    raise
                last_error = exc
                continue
            try:
                return AgentRunJournal(
                    root_fd,
                    run_fd,
                    run_id,
                    run_root_path=Path(run_root),
                    title=title,
                    identity=identity,
                    execution_profile=execution_profile,
                    declared_outputs=declared_outputs,
                )
            except BaseException as setup_error:
                # The constructor released the run descriptor on failure but
                # kept the root descriptor open; close the run descriptor
                # again (harmless) and remove the initialization-only run
                # directory (and any crash-left temporary manifest) so no
                # orphan survives without a manifest.  A cleanup failure
                # becomes a bounded log line and note on the primary
                # initialization error.
                try:
                    os.close(run_fd)
                except OSError:
                    pass
                try:
                    _remove_initialization_run(root_fd, run_id)
                except AgentRunJournalError as cleanup_error:
                    _log.error(
                        "cannot remove agent run directory %s after"
                        " initialization failure: %s",
                        run_id,
                        _bounded_message(cleanup_error),
                    )
                    try:
                        setup_error.add_note(
                            "agent run journal cleanup also failed: "
                            + _bounded_message(cleanup_error)
                        )
                    except (AttributeError, TypeError):
                        pass
                raise
        raise AgentRunJournalError(
            "cannot allocate a unique agent run id"
        ) from last_error
    except BaseException:
        os.close(root_fd)
        raise


class AgentRunJournal:
    """Strict-versioned, atomically updated record of one agent run.

    The runner owns process-level events and streams; callers own the final
    accepted/rejected marks.  Every state change rewrites ``manifest.json``
    through a same-directory temporary file, fsync, and atomic rename.
    """

    def __init__(
        self,
        root_fd: int,
        run_fd: int,
        run_id: str,
        *,
        run_root_path: Path,
        title: str,
        identity: AgentIdentity,
        execution_profile: str,
        declared_outputs: Mapping[str, int],
    ) -> None:
        self._root_fd = root_fd
        self._run_fd = run_fd
        self._run_id = run_id
        self._title = title
        self._identity = identity
        self._execution_profile = execution_profile
        self._started_at_utc = _utc_now()
        self._ended_at_utc: str | None = None
        self._workspace_path: str | None = None
        self._workspace_disposition: dict[str, Any] | None = None
        self._inputs: list[tuple[str, int, str]] = []
        self._outputs: dict[str, dict[str, Any]] = {
            name: {
                "name": name,
                "limit": limit,
                "status": "uncollected",
                "size_bytes": None,
                "sha256": None,
            }
            for name, limit in declared_outputs.items()
        }
        self._processes: dict[str, dict[str, Any]] = {}
        self._streams: dict[tuple[str, str], _StreamState] = {}
        self._session_id: str | None = None
        self._session_parsed = False
        self._session_parse_mode: Literal["strict", "best-effort"] | None = None
        self._output_collection: dict[str, Any] = {
            "status": "pending",
            "error_type": None,
            "error_message": None,
        }
        self._validation: dict[str, Any] = {
            "status": "pending",
            "published": False,
            "published_at_utc": None,
            "error_type": None,
            "error_message": None,
        }
        self._runner_error: dict[str, str] | None = None
        self._final_status = "started"
        self._finalization: dict[str, Any] | None = None
        self._finalized = False
        self._manifest_path = (
            Path(os.path.abspath(os.fspath(run_root_path))) / run_id / _MANIFEST_NAME
        )
        try:
            self._write_manifest()
        except BaseException:
            # Release the run descriptor but keep the root descriptor open:
            # the caller (create_agent_run_journal) still needs the root to
            # remove this initialization-only run directory.
            self._close_run_fd()
            raise
        _log.info("agent run %s started: manifest=%s", run_id, self.manifest_path)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    # -- runner-facing records ------------------------------------------------

    def record_workspace(self, workspace: str | Path) -> None:
        self._workspace_path = os.fspath(workspace)
        # Every recorded workspace starts with a pending disposition: only the
        # runner (immediate deletion) or the lifecycle handle (deferred
        # cleanup) may resolve it to deleted or retained, and the terminal
        # manifest must carry that resolution whenever a workspace exists.
        self._workspace_disposition = {
            "disposition": "pending",
            "deleted_at_utc": None,
            "retained_at_utc": None,
            "reason": None,
        }
        self._write_manifest()

    def record_workspace_disposition(
        self,
        disposition: Literal["deleted", "retained"],
        *,
        reason: str | None = None,
    ) -> None:
        """Resolve the workspace disposition before the run is finalized.

        ``pending`` is not caller-recordable: it is the initial state of a
        recorded workspace and the durable marker of an unresolved cleanup.
        Only ``deleted`` and ``retained`` are accepted; anything else raises
        so no caller can map an arbitrary value onto a retention claim.
        ``deleted`` carries the resolution timestamp; ``retained`` carries the
        retention timestamp and an optional bounded non-empty string reason.
        Recording after finalization raises, so a terminal manifest can never
        be rewritten to change what happened to the workspace.
        """
        if self._workspace_path is None:
            raise AgentRunJournalError(
                f"agent run {self._run_id} has no workspace to dispose"
            )
        if self._finalized:
            raise AgentRunJournalError(f"agent run {self._run_id} is already finalized")
        if disposition not in ("deleted", "retained"):
            raise AgentRunJournalError(
                f"agent run {self._run_id} has an invalid workspace disposition:"
                f" {disposition!r}"
            )
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise AgentRunJournalError(
                "agent run workspace disposition reason must be a non-empty string"
            )
        bounded_reason = _bounded_message(reason) if reason is not None else None
        if disposition == "deleted":
            self._workspace_disposition = {
                "disposition": "deleted",
                "deleted_at_utc": _utc_now(),
                "retained_at_utc": None,
                "reason": bounded_reason,
            }
        else:
            self._workspace_disposition = {
                "disposition": "retained",
                "deleted_at_utc": None,
                "retained_at_utc": _utc_now(),
                "reason": bounded_reason,
            }
        self._write_manifest()

    def record_input(self, name: str, size_bytes: int, sha256: str) -> None:
        self._inputs.append((name, size_bytes, sha256))
        self._write_manifest()

    def process_started(self, label: str, *, capture_streams: bool = False) -> None:
        if label not in _STREAM_LABELS:
            raise AgentRunJournalError(f"unsupported agent run process: {label}")
        if label in self._processes:
            raise AgentRunJournalError(f"agent run process already started: {label}")
        process = {
            "label": label,
            "started_at_utc": _utc_now(),
            "ended_at_utc": None,
            "pid": None,
            "returncode": None,
            "result": "not_run",
            # capture_complete stays None while the process runs: process_finished
            # marks the capture complete and capture_interrupted marks it
            # truncated.  The record never claims a completed capture before the
            # process finished.
            "capture": "streams" if capture_streams else "disabled",
            "capture_complete": None,
            "capture_error_type": None,
            "capture_error_message": None,
            "stdout": None,
            "stderr": None,
        }
        if not capture_streams:
            self._processes[label] = process
            self._write_manifest()
            return
        opened: list[tuple[str, _StreamState]] = []
        try:
            for stream in _STREAM_NAMES:
                name = _stream_filename(label, stream)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                try:
                    descriptor = os.open(name, flags, 0o600, dir_fd=self._run_fd)
                except OSError as exc:
                    raise AgentRunJournalError(
                        f"cannot create agent run stream {name}"
                    ) from exc
                opened.append((stream, _StreamState(descriptor, hashlib.sha256(), 0)))
                # O_EXCL|O_NOFOLLOW guarantee a fresh single-link regular
                # file; verify it so a placeholder can never alias an
                # existing target through a symlink or unsafe hardlink.  The
                # descriptor is already tracked so a failure here unlinks
                # the placeholder in the cleanup below.
                placeholder_info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(placeholder_info.st_mode)
                    or placeholder_info.st_nlink != 1
                ):
                    raise AgentRunJournalError(
                        f"agent run stream placeholder is unsafe: {name}"
                    )
        except BaseException:
            for stream, state in opened:
                try:
                    os.close(state.descriptor)
                except OSError:
                    pass
                try:
                    os.unlink(_stream_filename(label, stream), dir_fd=self._run_fd)
                except OSError:
                    pass
            raise
        empty_digest = hashlib.sha256(b"").hexdigest()
        for stream, state in opened:
            process[stream] = {
                "filename": _stream_filename(label, stream),
                # Zero-byte records keep every intermediate manifest strictly
                # readable: a crash between start and finish leaves a valid
                # running capture, never a record that claims stream metadata
                # it did not persist.
                "size_bytes": 0,
                "sha256": empty_digest,
            }
            self._streams[(label, stream)] = state
        self._processes[label] = process
        self._write_manifest()

    def process_pid(self, label: str, pid: int) -> None:
        process = self._processes.get(label)
        if process is None:
            raise AgentRunJournalError(f"agent run process was not started: {label}")
        process["pid"] = pid
        self._write_manifest()

    def write_chunk(self, label: str, stream: str, chunk: bytes) -> None:
        state = self._streams.get((label, stream))
        if state is None:
            raise AgentRunJournalError(
                f"agent run stream is not open: {label}/{stream}"
            )
        remaining = memoryview(chunk)
        while remaining:
            try:
                written = os.write(state.descriptor, remaining)
            except OSError as exc:
                raise AgentRunJournalError(
                    f"cannot write agent run stream {label}/{stream}"
                ) from exc
            if written <= 0:
                raise AgentRunJournalError(
                    f"cannot write agent run stream {label}/{stream}"
                )
            remaining = remaining[written:]
        state.digest.update(chunk)
        state.size += len(chunk)

    def process_finished(self, label: str, returncode: int | None) -> None:
        process = self._processes.get(label)
        if process is None:
            raise AgentRunJournalError(f"agent run process was not started: {label}")
        failure: AgentRunJournalError | None = None
        if process["capture"] != "disabled":
            # Deterministic stream order so a mid-loop failure always leaves
            # the first stream's metadata recorded and every stream state
            # handled by the same path.
            for stream in ("stdout", "stderr"):
                state = self._streams.pop((label, stream), None)
                if state is None:
                    if failure is None:
                        failure = AgentRunJournalError(
                            f"agent run stream is not open: {label}/{stream}"
                        )
                    continue
                try:
                    os.fsync(state.descriptor)
                except OSError as exc:
                    if failure is None:
                        failure = AgentRunJournalError(
                            f"cannot sync agent run stream {label}/{stream}"
                        )
                        failure.__cause__ = exc
                try:
                    os.close(state.descriptor)
                except OSError as exc:
                    if failure is None:
                        failure = AgentRunJournalError(
                            f"cannot close agent run stream {label}/{stream}"
                        )
                        failure.__cause__ = exc
                # Preserve the actual accepted bytes' size and hash even when
                # the sync or close failed: the stream contents are known and
                # the persisted metadata must never lie about them.
                process[stream] = {
                    "filename": _stream_filename(label, stream),
                    "size_bytes": state.size,
                    "sha256": state.digest.hexdigest(),
                }
            if failure is not None:
                # The capture is incomplete with a bounded reason, so the
                # record stays strictly readable; the run is failed by the
                # caller.
                process["capture_complete"] = False
                process["capture_error_type"] = _bounded_message(
                    type(failure).__name__, _MAX_ERROR_TYPE_CHARS
                )
                process["capture_error_message"] = _bounded_message(str(failure))
            elif process["capture_complete"] is not False:
                # The capture ended with this process: the streams were synced,
                # closed, and recorded.  A capture_interrupted mark stays
                # truncated with its reason.
                process["capture_complete"] = True
        process["ended_at_utc"] = _utc_now()
        process["returncode"] = returncode
        process["result"] = _process_result(returncode)
        # Persist the end record (including any bounded incomplete-capture
        # reason) before surfacing the stream failure: a crash in between
        # leaves a strictly readable nonterminal record, never a silently
        # dropped diagnosis.
        self._write_manifest()
        if failure is not None:
            raise failure

    def session_parsed(
        self,
        session_id: str | None,
        *,
        mode: Literal["strict", "best-effort"],
    ) -> None:
        """Record that session parsing completed with *session_id* or None.

        ``mode`` names the parser that ran: ``strict`` for the normal run
        contract parser, ``best-effort`` for the interrupted-communication
        recovery parser.  ``session_id`` stays None when the agent emitted no
        valid session ID; the parsing completion itself is still persisted so
        manifests can prove the parse ran and how.
        """
        self._session_parsed = True
        self._session_parse_mode = mode
        if session_id is not None and session_id != self._session_id:
            self._session_id = session_id
            _log.info(
                "agent run %s session_id=%s manifest=%s",
                self.run_id,
                session_id,
                self.manifest_path,
            )
        self._write_manifest()

    def capture_interrupted(self, label: str, error_type: str, message: str) -> None:
        """Mark *label*'s stream capture truncated with a bounded reason.

        Called only on the interrupted-communication path: the process keeps
        the bounded non-secret error type and message while ``process_finished``
        still records the stream metadata for the bytes accepted before the
        failure.
        """
        process = self._processes.get(label)
        if process is None:
            raise AgentRunJournalError(f"agent run process was not started: {label}")
        if process["capture"] == "disabled":
            raise AgentRunJournalError(
                f"agent run process {label} has no stream capture to interrupt"
            )
        process["capture_complete"] = False
        process["capture_error_type"] = _bounded_message(
            error_type, _MAX_ERROR_TYPE_CHARS
        )
        process["capture_error_message"] = _bounded_message(message)
        self._write_manifest()

    def record_output(self, name: str, size_bytes: int, sha256: str) -> None:
        record = self._outputs.get(name)
        if record is None:
            raise AgentRunJournalError(f"agent run output was not declared: {name}")
        record["status"] = "collected"
        record["size_bytes"] = size_bytes
        record["sha256"] = sha256
        self._write_manifest()

    def output_collection_complete(self) -> None:
        self._output_collection["status"] = "complete"
        self._write_manifest()

    def output_collection_failed(self, error_type: str, message: str) -> None:
        self._output_collection["status"] = "failed"
        self._output_collection["error_type"] = _bounded_message(
            error_type, _MAX_ERROR_TYPE_CHARS
        )
        self._output_collection["error_message"] = _bounded_message(message)
        self._write_manifest()

    def mark_runner_failed(self, error_type: str, message: str) -> None:
        self._finalize(
            "failed",
            runner_error={
                "error_type": _bounded_message(error_type, _MAX_ERROR_TYPE_CHARS),
                "error_message": _bounded_message(message),
            },
        )

    def close(self) -> None:
        """Release run directory descriptors without finalizing the record.

        A best-effort safety net for runs that cannot be finalized (for
        example their workspace never existed); finalized journals are
        already closed.
        """
        self._close_streams()
        self._close_fds()

    # -- caller-facing marks ---------------------------------------------------

    def mark_accepted(self) -> None:
        self._assert_semantic_prerequisites()
        self._finalize(
            "accepted",
            validation={
                "status": "accepted",
                "published": True,
                "published_at_utc": _utc_now(),
                "error_type": None,
                "error_message": None,
            },
        )

    def mark_rejected(self, error_type: str, message: str) -> None:
        self._assert_semantic_prerequisites()
        self._finalize(
            "rejected",
            validation={
                "status": "rejected",
                "published": False,
                "published_at_utc": None,
                "error_type": _bounded_message(error_type, _MAX_ERROR_TYPE_CHARS),
                "error_message": _bounded_message(message),
            },
        )

    def _assert_semantic_prerequisites(self) -> None:
        """Reject accepted/rejected marks unless the runner fully succeeded.

        Both outcomes are semantic caller decisions made only after the main
        agent process returned success, session parsing completed through the
        strict parser, and every declared output was collected.  Stream
        capture is only required when the runner opted into persisting raw
        streams: capture-disabled runs rely on strict session parsing and the
        successful process outcome instead.
        """
        agent = self._processes.get(_AGENT_LABEL)
        if agent is None:
            raise AgentRunJournalError(
                f"agent run {self._run_id} has no main agent process to accept"
                " or reject"
            )
        if agent["result"] != "success" or agent["returncode"] != 0:
            raise AgentRunJournalError(
                f"agent run {self._run_id} main agent process did not succeed"
            )
        if agent["capture"] == "streams" and (
            agent["stdout"] is None
            or agent["stderr"] is None
            or agent["capture_complete"] is not True
        ):
            raise AgentRunJournalError(
                f"agent run {self._run_id} main agent stream capture is incomplete"
            )
        if not self._session_parsed:
            raise AgentRunJournalError(
                f"agent run {self._run_id} session parsing did not complete"
            )
        if self._session_parse_mode != "strict":
            raise AgentRunJournalError(
                f"agent run {self._run_id} session parsing was not strict"
            )
        if self._output_collection["status"] != "complete":
            raise AgentRunJournalError(
                f"agent run {self._run_id} output collection did not complete"
            )
        if any(record["status"] != "collected" for record in self._outputs.values()):
            raise AgentRunJournalError(
                f"agent run {self._run_id} has uncollected declared outputs"
            )

    # -- internals --------------------------------------------------------------

    def _finalize(
        self,
        final_status: str,
        *,
        validation: dict[str, Any] | None = None,
        runner_error: dict[str, str] | None = None,
    ) -> None:
        if self._finalized:
            raise AgentRunJournalError(f"agent run {self._run_id} is already finalized")
        try:
            self._close_streams()
            if self._ended_at_utc is None:
                self._ended_at_utc = _utc_now()
            self._resolve_unresolved_workspace_disposition()
            # Durable pending finalization intent: the run ended and is about
            # to transition to *final_status*.  This write happens before the
            # terminal manifest, so a crash or write failure in between leaves
            # a record that never reads like an ordinary running ``started``
            # run: it carries the pending intent with its target, timestamp,
            # and the bounded payload of the attempted transition.
            self._finalization = {
                "status": "pending",
                "target_status": final_status,
                "attempted_at_utc": _utc_now(),
                "completed_at_utc": None,
                "validation": dict(validation) if validation is not None else None,
                "runner_error": (
                    dict(runner_error) if runner_error is not None else None
                ),
            }
            self._write_manifest()
            if validation is not None:
                self._validation = validation
            if runner_error is not None:
                self._runner_error = runner_error
            self._final_status = final_status
            self._finalization["status"] = "completed"
            self._finalization["completed_at_utc"] = _utc_now()
            self._write_manifest()
        finally:
            # The record is closed on every path, including a manifest write
            # failure: streams are fsynced and all descriptors are released
            # before the failure propagates.
            self._close_streams()
            self._close_fds()
        self._finalized = True

    def _resolve_unresolved_workspace_disposition(self) -> None:
        """Conservatively resolve a pending workspace disposition at finalize.

        A run finalized while its recorded workspace still carries an
        unresolved disposition (a direct journal user, or a crash between the
        runner returning and the lifecycle handle acting) must never produce a
        terminal manifest with a pending disposition.  The resolution opens
        the path with ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` and verifies the
        opened descriptor, so a symlink at the path is never followed and a
        path replacement cannot redirect the mode restore onto another
        target: a path that no longer exists, or whose final component is a
        symlink or plain file, is recorded as ``deleted``; a real directory
        owned by the effective user is recorded as ``retained`` with its root
        mode restored to 0700 through ``fchmod`` on the pinned descriptor.  A
        directory or replacement not owned by the effective user is never
        treated as an ordinary retained workspace: it is recorded as
        ``retained`` with a truthful bounded reason naming the unsafe state,
        and it is never deleted — the journal only records, it never removes
        paths, and a foreign-owned path may hold another user's data.
        """
        if (
            self._workspace_path is None
            or self._workspace_disposition is None
            or self._workspace_disposition.get("disposition") != "pending"
        ):
            return
        workspace = Path(self._workspace_path)
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    workspace,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                )
            except FileNotFoundError:
                self._workspace_disposition = {
                    "disposition": "deleted",
                    "deleted_at_utc": _utc_now(),
                    "retained_at_utc": None,
                    "reason": (
                        "workspace no longer exists when the run was finalized "
                        "without an explicit disposition"
                    ),
                }
                return
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    # The final component is a symlink or a plain file, never
                    # the original directory; claiming retention would claim a
                    # workspace that is no longer there.
                    self._workspace_disposition = {
                        "disposition": "deleted",
                        "deleted_at_utc": _utc_now(),
                        "retained_at_utc": None,
                        "reason": (
                            "workspace path is no longer the original directory "
                            "when the run was finalized without an explicit "
                            "disposition"
                        ),
                    }
                    return
                self._workspace_disposition = {
                    "disposition": "retained",
                    "deleted_at_utc": None,
                    "retained_at_utc": _utc_now(),
                    "reason": (
                        "workspace could not be inspected when the run was "
                        "finalized without an explicit disposition: "
                        + _bounded_message(exc)
                    ),
                }
                return
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISDIR(info.st_mode):
                self._workspace_disposition = {
                    "disposition": "deleted",
                    "deleted_at_utc": _utc_now(),
                    "retained_at_utc": None,
                    "reason": (
                        "workspace path is no longer the original directory when "
                        "the run was finalized without an explicit disposition"
                    ),
                }
                return
            if info.st_uid != os.geteuid():
                # A foreign-owned replacement is never silently treated as an
                # ordinary retained workspace: the journal records the truthful
                # unsafe state with a bounded reason and never deletes the path.
                self._workspace_disposition = {
                    "disposition": "retained",
                    "deleted_at_utc": None,
                    "retained_at_utc": _utc_now(),
                    "reason": (
                        "workspace is not owned by the effective user when the run "
                        "was finalized without an explicit disposition; kept for "
                        "manual inspection, not verified as a safe retained "
                        "workspace"
                    ),
                }
                return
            if stat.S_IMODE(info.st_mode) != 0o700:
                try:
                    os.fchmod(descriptor, 0o700)
                except OSError as exc:
                    self._workspace_disposition = {
                        "disposition": "retained",
                        "deleted_at_utc": None,
                        "retained_at_utc": _utc_now(),
                        "reason": (
                            "workspace mode could not be restored to 0700 when the "
                            "run was finalized without an explicit disposition: "
                            + _bounded_message(exc)
                        ),
                    }
                    return
                try:
                    restored = os.fstat(descriptor)
                except OSError as exc:
                    self._workspace_disposition = {
                        "disposition": "retained",
                        "deleted_at_utc": None,
                        "retained_at_utc": _utc_now(),
                        "reason": (
                            "workspace could not be re-inspected after mode restore "
                            "when the run was finalized without an explicit "
                            "disposition: " + _bounded_message(exc)
                        ),
                    }
                    return
                if (
                    not stat.S_ISDIR(restored.st_mode)
                    or stat.S_IMODE(restored.st_mode) != 0o700
                ):
                    self._workspace_disposition = {
                        "disposition": "retained",
                        "deleted_at_utc": None,
                        "retained_at_utc": _utc_now(),
                        "reason": (
                            "workspace mode is still not 0700 after the restore "
                            "attempt when the run was finalized without an "
                            "explicit disposition"
                        ),
                    }
                    return
            # The descriptor pinned the original directory; prove the current path
            # still holds that same inode before any retained claim is recorded.
            # A path that disappeared, became a symlink or non-directory, or now
            # points to a different directory inode is classified truthfully and
            # the original is never recorded as a verified retained workspace.
            current_descriptor: int | None = None
            try:
                try:
                    current_descriptor = os.open(
                        workspace,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    )
                except FileNotFoundError:
                    self._workspace_disposition = {
                        "disposition": "deleted",
                        "deleted_at_utc": _utc_now(),
                        "retained_at_utc": None,
                        "reason": (
                            "workspace path no longer exists when the run was "
                            "finalized without an explicit disposition"
                        ),
                    }
                    return
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        self._workspace_disposition = {
                            "disposition": "deleted",
                            "deleted_at_utc": _utc_now(),
                            "retained_at_utc": None,
                            "reason": (
                                "workspace path no longer holds the original "
                                "directory when the run was finalized without an "
                                "explicit disposition"
                            ),
                        }
                        return
                    self._workspace_disposition = {
                        "disposition": "retained",
                        "deleted_at_utc": None,
                        "retained_at_utc": _utc_now(),
                        "reason": (
                            "workspace path could not be re-inspected when the run "
                            "was finalized without an explicit disposition: "
                            + _bounded_message(exc)
                        ),
                    }
                    return
                current = os.fstat(current_descriptor)
                if (current.st_dev, current.st_ino) != identity:
                    self._workspace_disposition = {
                        "disposition": "retained",
                        "deleted_at_utc": None,
                        "retained_at_utc": _utc_now(),
                        "reason": (
                            "workspace path now points to a different directory than "
                            "the recorded workspace when the run was finalized "
                            "without an explicit disposition; not verified as the "
                            "original workspace"
                        ),
                    }
                    return
            finally:
                if current_descriptor is not None:
                    try:
                        os.close(current_descriptor)
                    except OSError:
                        pass
            self._workspace_disposition = {
                "disposition": "retained",
                "deleted_at_utc": None,
                "retained_at_utc": _utc_now(),
                "reason": (
                    "workspace disposition was not resolved before finalization; "
                    "the workspace was kept conservatively"
                ),
            }
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _close_streams(self) -> None:
        for state in self._streams.values():
            try:
                os.fsync(state.descriptor)
            except OSError:
                pass
            try:
                os.close(state.descriptor)
            except OSError:
                pass
        self._streams.clear()

    def _close_run_fd(self) -> None:
        descriptor = getattr(self, "_run_fd")
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass
        setattr(self, "_run_fd", None)

    def _close_fds(self) -> None:
        for attribute in ("_run_fd", "_root_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass
            setattr(self, attribute, None)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "schema": AGENT_RUN_SCHEMA,
            "schema_version": AGENT_RUN_SCHEMA_VERSION,
            "run_id": self._run_id,
            "title": self._title,
            "agent": {
                "name": self._identity.name,
                "model": self._identity.model,
                "variant": self._identity.variant,
                "prompt_sha256": self._identity.prompt_sha256,
                "fingerprint": self._identity.fingerprint,
                "execution_profile": self._execution_profile,
            },
            "started_at_utc": self._started_at_utc,
            "ended_at_utc": self._ended_at_utc,
            "workspace_path": self._workspace_path,
            "workspace_disposition": (
                dict(self._workspace_disposition)
                if self._workspace_disposition is not None
                else None
            ),
            "inputs": [
                {"name": name, "size_bytes": size, "sha256": digest}
                for name, size, digest in self._inputs
            ],
            "outputs": sorted(self._outputs.values(), key=lambda r: r["name"]),
            "processes": [
                self._processes[label]
                for label in (_PROBE_LABEL, _AGENT_LABEL)
                if label in self._processes
            ],
            "session_id": self._session_id,
            "session_parsed": self._session_parsed,
            "session_parse_mode": self._session_parse_mode,
            "output_collection": dict(self._output_collection),
            "validation": dict(self._validation),
            "runner_error": (
                dict(self._runner_error) if self._runner_error is not None else None
            ),
            "finalization": (
                dict(self._finalization) if self._finalization is not None else None
            ),
            "final_status": self._final_status,
        }

    def _lstat_target(self) -> os.stat_result | None:
        """Lstat the persisted manifest target without following symlinks.

        Returns None when the target does not exist yet (first write).
        """
        try:
            return os.lstat(_MANIFEST_NAME, dir_fd=self._run_fd)
        except FileNotFoundError:
            return None
        except TypeError:
            # dir_fd is unsupported on this platform; fall back to a path.
            try:
                return os.lstat(self._manifest_path)
            except FileNotFoundError:
                return None

    def _write_manifest(self) -> None:
        if self._run_fd is None:
            raise AgentRunJournalError(f"agent run {self._run_id} journal is closed")
        data = (json.dumps(self._snapshot(), indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        temporary = f".{_MANIFEST_NAME}.{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._run_fd,
            )
            # O_EXCL|O_NOFOLLOW guarantee a fresh single-link regular file;
            # verify it so the placeholder can never alias an existing
            # target through a symlink or unsafe hardlink.
            placeholder_info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(placeholder_info.st_mode)
                or placeholder_info.st_nlink != 1
            ):
                raise AgentRunJournalError(
                    "the agent run manifest temporary is not a fresh regular file"
                )
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise AgentRunJournalError("cannot write the agent run manifest")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            target_info = self._lstat_target()
            if target_info is not None:
                if stat.S_ISLNK(target_info.st_mode):
                    raise AgentRunJournalError(
                        "the agent run manifest target is a symlink"
                    )
                if target_info.st_nlink > 1:
                    raise AgentRunJournalError(
                        "the agent run manifest target is an unsafe hardlink"
                    )
            try:
                os.replace(
                    temporary,
                    _MANIFEST_NAME,
                    src_dir_fd=self._run_fd,
                    dst_dir_fd=self._run_fd,
                )
            except TypeError:
                os.replace(self._manifest_path.parent / temporary, self._manifest_path)
            os.fsync(self._run_fd)
        except AgentRunJournalError:
            raise
        except OSError as exc:
            raise AgentRunJournalError(
                f"cannot update the agent run manifest: {exc}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=self._run_fd)
                except (OSError, ValueError):
                    pass


# -- strict reader -------------------------------------------------------------


class AgentRunAgentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    model: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_profile: Literal["development", "restricted"]


class AgentRunInputRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentRunOutputRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    limit: int = Field(ge=1)
    status: Literal["uncollected", "collected"]
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _coherent(self) -> AgentRunOutputRecord:
        if self.status == "collected":
            if self.size_bytes is None or self.sha256 is None:
                raise ValueError(f"collected output {self.name!r} lacks size or hash")
        else:
            if self.size_bytes is not None or self.sha256 is not None:
                raise ValueError(
                    f"uncollected output {self.name!r} carries size or hash"
                )
        return self


class AgentRunStreamRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    filename: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _safe_direct_child(self) -> AgentRunStreamRecord:
        # Only the known direct-child capture names are accepted; this
        # rejects absolute paths, separators, dot components, and unknown
        # filenames in one check.  The label/stream match is validated by
        # the owning process record.
        if self.filename not in _STREAM_FILENAMES:
            raise ValueError(
                f"stream filename {self.filename!r} is not a known"
                " direct-child capture name"
            )
        return self


class AgentRunProcessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: Literal["config-probe", "agent"]
    started_at_utc: str
    ended_at_utc: str | None = None
    pid: int | None = Field(default=None, ge=0)
    returncode: int | None = None
    result: Literal["not_run", "success", "nonzero", "error"]
    capture: Literal["streams", "disabled"] = "streams"
    capture_complete: bool | None = None
    capture_error_type: str | None = Field(default=None, min_length=1)
    capture_error_message: str | None = Field(default=None, min_length=1)
    stdout: AgentRunStreamRecord | None = None
    stderr: AgentRunStreamRecord | None = None

    @model_validator(mode="after")
    def _coherent(self) -> AgentRunProcessRecord:
        if self.capture == "streams":
            if self.stdout is None or self.stderr is None:
                raise ValueError(f"process {self.label!r} stream capture is incomplete")
            if self.capture_complete is None:
                # Running capture: the process started but has not finished
                # (or the host crashed before the finish record).
                if (
                    self.capture_error_type is not None
                    or self.capture_error_message is not None
                ):
                    raise ValueError(
                        f"process {self.label!r} running capture carries an error"
                    )
            elif self.capture_complete:
                if (
                    self.capture_error_type is not None
                    or self.capture_error_message is not None
                ):
                    raise ValueError(
                        f"process {self.label!r} complete capture carries an error"
                    )
            elif self.capture_error_type is None or self.capture_error_message is None:
                raise ValueError(
                    f"process {self.label!r} incomplete capture lacks a reason"
                )
        else:
            if self.stdout is not None or self.stderr is not None:
                raise ValueError(
                    f"disabled-capture process {self.label!r} has stream records"
                )
            if (
                self.capture_complete is not None
                or self.capture_error_type is not None
                or self.capture_error_message is not None
            ):
                raise ValueError(
                    f"disabled-capture process {self.label!r} has capture records"
                )
        if self.result == "not_run":
            # A running process may already carry its pid; only a returncode
            # or end timestamp would claim an outcome it never recorded.
            if self.returncode is not None or self.ended_at_utc is not None:
                raise ValueError(
                    f"process {self.label!r} is running but claims an outcome"
                )
        elif self.result == "success":
            if self.returncode != 0 or self.ended_at_utc is None or self.pid is None:
                raise ValueError(f"process {self.label!r} success record is incoherent")
        elif self.result == "nonzero":
            if (
                self.returncode is None
                or self.returncode <= 0
                or self.ended_at_utc is None
                or self.pid is None
            ):
                raise ValueError(f"process {self.label!r} nonzero record is incoherent")
        else:
            if self.ended_at_utc is None:
                raise ValueError(f"process {self.label!r} error record never ended")
            if self.returncode is not None and self.returncode >= 0:
                raise ValueError(
                    f"process {self.label!r} error record has a nonnegative returncode"
                )
        # Stream filenames must match the owning process label and stream
        # field: a record may never point a capture at another process's
        # file.  Capture-disabled records carry no stream records at all.
        if self.stdout is not None and self.stdout.filename != _stream_filename(
            self.label, "stdout"
        ):
            raise ValueError(
                f"process {self.label!r} stdout filename does not match its"
                " capture name"
            )
        if self.stderr is not None and self.stderr.filename != _stream_filename(
            self.label, "stderr"
        ):
            raise ValueError(
                f"process {self.label!r} stderr filename does not match its"
                " capture name"
            )
        return self


class AgentRunCollectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["pending", "complete", "failed"]
    error_type: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> AgentRunCollectionRecord:
        if self.status == "complete":
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("complete output collection cannot carry an error")
        elif self.status == "failed":
            if self.error_type is None or self.error_message is None:
                raise ValueError("failed output collection must carry an error")
        elif self.error_type is not None or self.error_message is not None:
            raise ValueError("pending output collection cannot carry an error")
        return self


class AgentRunValidationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["pending", "accepted", "rejected"]
    published: bool = False
    published_at_utc: str | None = None
    error_type: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> AgentRunValidationRecord:
        if self.status == "accepted":
            if not self.published or self.published_at_utc is None:
                raise ValueError("accepted validation must be published")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("accepted validation cannot carry an error")
        elif self.status == "rejected":
            if self.published or self.published_at_utc is not None:
                raise ValueError("rejected validation cannot be published")
            if self.error_type is None or self.error_message is None:
                raise ValueError("rejected validation must carry an error")
        elif self.published or self.published_at_utc is not None:
            raise ValueError("pending validation cannot be published")
        elif self.error_type is not None or self.error_message is not None:
            raise ValueError("pending validation cannot carry an error")
        return self


class AgentRunErrorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    error_type: str = Field(min_length=1)
    error_message: str = Field(min_length=1)


class AgentRunFinalizationRecord(BaseModel):
    """Durable intent of one attempted terminal transition.

    ``pending`` intents survive a failure or crash between the intent write
    and the terminal manifest write: the run record then stays non-terminal
    (``final_status: "started"``) while still exposing the target it was
    about to transition to.  ``completed`` intents mark that the terminal
    manifest was durably persisted for the same target.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["pending", "completed"]
    target_status: Literal["accepted", "rejected", "failed"]
    attempted_at_utc: str
    completed_at_utc: str | None = None
    validation: AgentRunValidationRecord | None = None
    runner_error: AgentRunErrorRecord | None = None

    @model_validator(mode="after")
    def _coherent(self) -> AgentRunFinalizationRecord:
        if self.status == "pending":
            if self.completed_at_utc is not None:
                raise ValueError(
                    "pending finalization intent cannot carry a completion timestamp"
                )
        elif self.completed_at_utc is None:
            raise ValueError(
                "completed finalization intent lacks a completion timestamp"
            )
        if self.target_status == "failed":
            if self.validation is not None:
                raise ValueError(
                    "failed finalization intent cannot carry semantic validation"
                )
            if self.runner_error is None:
                raise ValueError("failed finalization intent must carry a runner error")
        elif self.runner_error is not None:
            raise ValueError(
                f"{self.target_status} finalization intent cannot carry a runner error"
            )
        elif self.validation is None or self.validation.status != self.target_status:
            raise ValueError(
                f"{self.target_status} finalization intent must carry matching"
                " validation"
            )
        return self


class AgentRunWorkspaceDisposition(BaseModel):
    """Strict record of what happened to one run's agent workspace.

    ``pending`` marks a recorded workspace whose cleanup was never resolved
    (a running run, a crash-left finalization intent, or a run without a
    recorded workspace at all).  ``deleted`` and ``retained`` are terminal:
    they carry the resolution timestamp, and ``retained`` may carry a bounded
    reason.  A terminal manifest with a recorded workspace must never carry a
    ``pending`` disposition: evidence is either gone or explicitly kept.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    disposition: Literal["pending", "deleted", "retained"]
    deleted_at_utc: str | None = None
    retained_at_utc: str | None = None
    reason: str | None = Field(
        default=None, min_length=1, max_length=_MAX_MESSAGE_CHARS
    )

    @model_validator(mode="after")
    def _coherent(self) -> AgentRunWorkspaceDisposition:
        if self.disposition == "pending":
            if self.deleted_at_utc is not None or self.retained_at_utc is not None:
                raise ValueError(
                    "pending workspace disposition cannot carry a timestamp"
                )
        elif self.disposition == "deleted":
            if self.deleted_at_utc is None:
                raise ValueError(
                    "deleted workspace disposition lacks a deletion timestamp"
                )
            if self.retained_at_utc is not None:
                raise ValueError(
                    "deleted workspace disposition cannot carry a retention timestamp"
                )
        elif self.retained_at_utc is None:
            raise ValueError(
                "retained workspace disposition lacks a retention timestamp"
            )
        elif self.deleted_at_utc is not None:
            raise ValueError(
                "retained workspace disposition cannot carry a deletion timestamp"
            )
        return self


class AgentRunManifest(BaseModel):
    """Strict contract for a persisted ``manifest.json`` run record."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_name: Literal["epubforge.agent-run"] = Field(
        default=AGENT_RUN_SCHEMA, alias="schema"
    )
    schema_version: Literal[2] = AGENT_RUN_SCHEMA_VERSION
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    title: str = Field(min_length=1)
    agent: AgentRunAgentInfo
    started_at_utc: str
    ended_at_utc: str | None = None
    workspace_path: str | None = None
    workspace_disposition: AgentRunWorkspaceDisposition | None = None
    inputs: list[AgentRunInputRecord] = Field(default_factory=list)
    outputs: list[AgentRunOutputRecord] = Field(default_factory=list)
    processes: list[AgentRunProcessRecord] = Field(default_factory=list)
    session_id: str | None = Field(default=None, max_length=256)
    session_parsed: bool = False
    session_parse_mode: Literal["strict", "best-effort"] | None = None
    output_collection: AgentRunCollectionRecord
    validation: AgentRunValidationRecord
    runner_error: AgentRunErrorRecord | None = None
    finalization: AgentRunFinalizationRecord | None = None
    final_status: Literal["started", "failed", "rejected", "accepted"]

    @model_validator(mode="after")
    def _coherent(self) -> AgentRunManifest:
        if self.session_parsed and self.session_parse_mode is None:
            raise ValueError("session parsing completed without a parsing mode")
        if not self.session_parsed and self.session_parse_mode is not None:
            raise ValueError("session parsing mode without completed parsing")
        if self.workspace_path is None:
            if self.workspace_disposition is not None:
                raise ValueError("workspace disposition without a workspace path")
        else:
            if self.workspace_disposition is None:
                raise ValueError("workspace path without a workspace disposition")
            if (
                self.final_status in ("accepted", "rejected", "failed")
                and self.workspace_disposition.disposition == "pending"
            ):
                raise ValueError(
                    "terminal runs cannot carry a pending workspace disposition"
                )
        if self.final_status in ("accepted", "rejected"):
            if self.validation.status != self.final_status:
                raise ValueError(
                    f"{self.final_status} runs must carry a matching validation outcome"
                )
            if self.runner_error is not None:
                raise ValueError(
                    f"{self.final_status} runs cannot carry a runner error"
                )
            agent = next(
                (process for process in self.processes if process.label == "agent"),
                None,
            )
            if agent is None:
                raise ValueError(
                    f"{self.final_status} runs require a main agent process"
                )
            if (
                agent.result != "success"
                or agent.returncode != 0
                or agent.ended_at_utc is None
            ):
                raise ValueError(
                    f"{self.final_status} runs require a successful main agent process"
                )
            if agent.capture == "streams" and (
                agent.stdout is None
                or agent.stderr is None
                or agent.capture_complete is not True
            ):
                raise ValueError(
                    f"{self.final_status} runs require complete main stream capture"
                )
            if not self.session_parsed or self.session_parse_mode != "strict":
                raise ValueError(
                    f"{self.final_status} runs require strict session parsing"
                )
            if self.output_collection.status != "complete":
                raise ValueError(
                    f"{self.final_status} runs require completed output collection"
                )
            if any(output.status != "collected" for output in self.outputs):
                raise ValueError(
                    f"{self.final_status} runs require every declared output collected"
                )
        elif self.final_status == "failed":
            if self.validation.status != "pending":
                raise ValueError(
                    "failed runs cannot carry semantic validation outcomes"
                )
            if self.runner_error is None:
                raise ValueError("failed runs must carry a runner error")
        else:
            if self.validation.status != "pending":
                raise ValueError(
                    "started runs cannot carry semantic validation outcomes"
                )
            if self.runner_error is not None:
                raise ValueError("started runs cannot carry a runner error")
        intent = self.finalization
        if intent is not None:
            if self.final_status == "started":
                # Only a pending intent is coherent on a running record; a
                # completed intent on ``started`` would claim a terminal
                # transition that never happened.
                if intent.status != "pending":
                    raise ValueError(
                        "started runs cannot carry a completed finalization intent"
                    )
            else:
                # A terminal manifest must have completed (or cleared) its
                # intent: the target must match the final status and the
                # intent must be a faithful copy of the terminal payload.
                if intent.status != "completed":
                    raise ValueError(
                        f"{self.final_status} runs cannot carry a pending"
                        " finalization intent"
                    )
                if intent.target_status != self.final_status:
                    raise ValueError(
                        "completed finalization intent target does not match the"
                        f" {self.final_status} final status"
                    )
                if (
                    intent.validation is not None
                    and self.validation != intent.validation
                ):
                    raise ValueError(
                        "completed finalization intent validation diverges from"
                        " the manifest"
                    )
                if (
                    intent.runner_error is not None
                    and self.runner_error != intent.runner_error
                ):
                    raise ValueError(
                        "completed finalization intent runner error diverges from"
                        " the manifest"
                    )
        return self


def read_agent_run_manifest(path: str | Path) -> AgentRunManifest:
    """Parse and strictly validate one persisted run manifest."""
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AgentRunJournalError(
            f"cannot read the agent run manifest {manifest_path}"
        ) from exc
    try:
        return AgentRunManifest.model_validate(payload)
    except ValidationError as exc:
        raise AgentRunJournalError(
            f"agent run manifest is invalid: {manifest_path}"
        ) from exc


# -- caller helpers --------------------------------------------------------------


def record_rejected(handle: AgentRunJournalHandle | None, error: BaseException) -> None:
    """Mark one run rejected without ever masking an existing error.

    A secondary journaling failure adds a bounded note and a log line to
    *error*; the original exception still propagates from the caller.  When
    *error* itself is a journaling failure it is re-raised untouched.
    """
    if handle is None:
        return
    try:
        handle.mark_rejected(type(error).__name__, str(error))
    except BaseException as journal_error:
        if isinstance(error, AgentRunJournalError):
            raise error
        _log.error("cannot record agent run rejection: %s", journal_error)
        error.add_note(
            "agent run journal rejection also failed: "
            f"{_bounded_message(journal_error)}"
        )


def record_accepted(handle: AgentRunJournalHandle | None) -> None:
    """Mark one run accepted only after a durable accepted manifest exists.

    A failure here raises so the stage never reports success while the
    durable run record still lacks the accepted outcome.
    """
    if handle is not None:
        handle.mark_accepted()


__all__ = [
    "AGENT_RUN_SCHEMA",
    "AGENT_RUN_SCHEMA_VERSION",
    "AgentRunCollectionRecord",
    "AgentRunErrorRecord",
    "AgentRunFinalizationRecord",
    "AgentRunInputRecord",
    "AgentRunJournal",
    "AgentRunJournalError",
    "AgentRunManifest",
    "AgentRunOutputRecord",
    "AgentRunProcessRecord",
    "AgentRunStreamRecord",
    "AgentRunValidationRecord",
    "AgentRunWorkspaceDisposition",
    "create_agent_run_journal",
    "read_agent_run_manifest",
    "record_accepted",
    "record_rejected",
]
