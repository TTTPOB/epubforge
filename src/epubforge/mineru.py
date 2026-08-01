"""Client for MinerU's official v4 local-file batch API."""

from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import httpx

from epubforge.config import MineruSettings

_SAFE_API_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


class MineruError(RuntimeError):
    """Base error for MinerU API operations."""


class MineruHTTPError(MineruError):
    """Raised when a MinerU or storage HTTP request fails."""


class MineruAPIError(MineruError):
    """Raised when MinerU returns a non-zero API code."""


class MineruProtocolError(MineruError):
    """Raised when MinerU returns an incomplete or malformed payload."""


class MineruJobFailedError(MineruError):
    """Raised when MinerU marks an extraction as failed."""


class MineruPollTimeoutError(MineruError):
    """Raised when an extraction does not complete within max_polls."""


class MineruDownloadError(MineruError):
    """Raised when the downloaded result is not a ZIP archive."""


@dataclass(frozen=True)
class MineruUpload:
    """One source file and its pre-signed upload URL."""

    file_name: str
    upload_url: str


@dataclass(frozen=True)
class MineruBatch:
    """A submitted MinerU batch and its upload targets."""

    batch_id: str
    uploads: tuple[MineruUpload, ...]


@dataclass(frozen=True)
class MineruFileResult:
    """The current status returned for one file in a batch."""

    file_name: str
    state: str
    full_zip_url: str | None = None


@dataclass(frozen=True)
class MineruDownloadResult:
    """A completed MinerU extraction saved as its original ZIP archive."""

    batch_id: str
    file_name: str
    zip_path: Path


class MineruClient:
    """Synchronous MinerU v4 client with public steps for future pipeline integration."""

    def __init__(
        self,
        settings: MineruSettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
        )
        self._owns_client = client is None
        self._sleep = sleep

    def close(self) -> None:
        """Close the internally-created HTTP client."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def request_file_urls(self, files: Sequence[Path]) -> MineruBatch:
        """Request a batch ID and one pre-signed upload URL per local file."""
        paths = tuple(Path(path) for path in files)
        if not paths:
            raise ValueError("MinerU batch requires at least one local file")
        if len({path.name for path in paths}) != len(paths):
            raise ValueError("MinerU batch requires unique file basenames")
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"MinerU input file does not exist: {path}")

        payload = {
            "model_version": self.settings.model_version,
            "enable_formula": self.settings.enable_formula,
            "enable_table": self.settings.enable_table,
            "language": self.settings.language,
            "files": [
                {
                    "name": path.name,
                    "is_ocr": self.settings.is_ocr,
                }
                for path in paths
            ],
        }
        data = self._api_data("POST", "/file-urls/batch", json=payload)
        batch_id = _required_str(data, "batch_id", "file-urls/batch response")
        raw_urls = _required_list(data, "file_urls", "file-urls/batch response")
        if len(raw_urls) != len(paths):
            raise MineruProtocolError(
                "file-urls/batch response file_urls count does not match requested files"
            )

        uploads = tuple(
            MineruUpload(path.name, _upload_url(raw_url, index))
            for index, (path, raw_url) in enumerate(zip(paths, raw_urls, strict=True))
        )
        return MineruBatch(batch_id=batch_id, uploads=uploads)

    def upload_file(self, file_path: Path, upload: MineruUpload) -> None:
        """Upload a local file to a pre-signed URL without API credentials."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"MinerU input file does not exist: {path}")
        with path.open("rb") as source:
            request = httpx.Request("PUT", upload.upload_url, content=source)
            self._send(request, "upload")

    def upload_batch(self, batch: MineruBatch, files: Sequence[Path]) -> None:
        """Upload each local file for a previously created batch."""
        paths = tuple(Path(path) for path in files)
        paths_by_name = {path.name: path for path in paths}
        target_names = [upload.file_name for upload in batch.uploads]
        if len(paths_by_name) != len(paths):
            raise ValueError("MinerU upload paths require unique file basenames")
        if len(set(target_names)) != len(target_names):
            raise ValueError("MinerU upload targets require unique file names")
        if set(paths_by_name) != set(target_names):
            raise ValueError("MinerU upload paths do not match batch upload targets")
        for upload in batch.uploads:
            self.upload_file(paths_by_name[upload.file_name], upload)

    def get_batch_results(self, batch_id: str) -> tuple[MineruFileResult, ...]:
        """Fetch the current extraction results for a submitted batch."""
        if not batch_id:
            raise ValueError("MinerU batch_id must not be empty")
        data = self._api_data("GET", f"/extract-results/batch/{batch_id}")
        raw_results = _required_list(data, "extract_result", "extract-results response")
        results: list[MineruFileResult] = []
        for index, raw_result in enumerate(raw_results):
            if not isinstance(raw_result, dict):
                raise MineruProtocolError(
                    f"extract-results response extract_result[{index}] must be an object"
                )
            file_name = _required_str(raw_result, "file_name", "extract result")
            state = _required_str(raw_result, "state", "extract result")
            full_zip_url = raw_result.get("full_zip_url")
            if full_zip_url is not None and not isinstance(full_zip_url, str):
                raise MineruProtocolError(
                    "extract result full_zip_url must be a string"
                )
            results.append(
                MineruFileResult(
                    file_name=file_name,
                    state=state,
                    full_zip_url=full_zip_url,
                )
            )
        return tuple(results)

    def wait_for_file(self, batch_id: str, file_name: str) -> MineruFileResult:
        """Poll a batch until one file succeeds, fails, or reaches max_polls."""
        for attempt in range(self.settings.max_polls):
            result = _result_for_file(self.get_batch_results(batch_id), file_name)
            state = result.state.lower()
            if state in {"done", "completed", "success"}:
                if not result.full_zip_url:
                    raise MineruProtocolError(
                        f"MinerU result for {file_name!r} completed without full_zip_url"
                    )
                return result
            if state in {"failed", "failure", "error"}:
                raise MineruJobFailedError(
                    f"MinerU extraction failed for {file_name!r} in batch {batch_id!r}"
                )
            if attempt + 1 < self.settings.max_polls:
                self._sleep(self.settings.poll_interval_seconds)
        raise MineruPollTimeoutError(
            f"MinerU extraction for {file_name!r} did not complete after "
            f"{self.settings.max_polls} polls"
        )

    def download_zip(self, full_zip_url: str, zip_path: Path) -> Path:
        """Download and atomically save a verified ZIP without extracting it."""
        if not full_zip_url:
            raise ValueError("MinerU full_zip_url must not be empty")
        destination = Path(zip_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                request = httpx.Request("GET", full_zip_url)
                with self._stream(request, "download") as response:
                    content_length = _content_length(response)
                    if (
                        content_length is not None
                        and content_length > self.settings.max_download_bytes
                    ):
                        raise MineruDownloadError(
                            "MinerU download Content-Length exceeds max_download_bytes"
                        )
                    downloaded = 0
                    for chunk in response.iter_bytes():
                        downloaded += len(chunk)
                        if downloaded > self.settings.max_download_bytes:
                            raise MineruDownloadError(
                                "MinerU download exceeds max_download_bytes"
                            )
                        temp_file.write(chunk)
            self._validate_zip(temp_path)
            os.replace(temp_path, destination)
            temp_path = None
            return destination
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def process_file(self, file_path: Path, zip_path: Path) -> MineruDownloadResult:
        """Run the complete local-file batch flow and retain the original ZIP."""
        path = Path(file_path)
        batch = self.request_file_urls([path])
        self.upload_batch(batch, [path])
        result = self.wait_for_file(batch.batch_id, path.name)
        assert result.full_zip_url is not None
        saved_zip = self.download_zip(result.full_zip_url, zip_path)
        return MineruDownloadResult(
            batch_id=batch.batch_id,
            file_name=path.name,
            zip_path=saved_zip,
        )

    def _api_data(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        api_key = self.settings.api_key
        if not api_key:
            raise MineruError(
                "MinerU API key is required (set [mineru].api_key or EPUBFORGE_MINERU_API_KEY)"
            )
        request = httpx.Request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            **kwargs,
        )
        response = self._send(request, f"MinerU API {path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MineruProtocolError(
                f"MinerU API {path} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise MineruProtocolError(
                f"MinerU API {path} returned a non-object JSON payload"
            )
        code = payload.get("code")
        if isinstance(code, bool) or not isinstance(code, (int, str)) or code == "":
            raise MineruProtocolError(
                f"MinerU API {path} response is missing integer or string code"
            )
        if code not in (0, "0"):
            raise MineruAPIError(
                f"MinerU API {path} returned code {_safe_api_code(code)}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MineruProtocolError(
                f"MinerU API {path} response is missing object data"
            )
        return data

    def _send(self, request: httpx.Request, operation: str) -> httpx.Response:
        response: httpx.Response | None = None
        try:
            response = self._client.send(request)
            response.raise_for_status()
            return response
        except httpx.HTTPError:
            if response is not None:
                response.close()
            raise _http_error(operation, request, response) from None

    @contextmanager
    def _stream(
        self, request: httpx.Request, operation: str
    ) -> Iterator[httpx.Response]:
        response: httpx.Response | None = None
        try:
            response = self._client.send(request, stream=True)
            response.raise_for_status()
            yield response
        except httpx.HTTPError:
            raise _http_error(operation, request, response) from None
        finally:
            if response is not None:
                response.close()

    def _validate_zip(self, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                uncompressed_bytes = sum(info.file_size for info in archive.infolist())
                if uncompressed_bytes > self.settings.max_uncompressed_bytes:
                    raise MineruDownloadError(
                        "MinerU download exceeds max_uncompressed_bytes"
                    )
                corrupt_member = archive.testzip()
        except MineruDownloadError:
            raise
        except Exception as exc:
            raise MineruDownloadError(
                "MinerU download is not a valid ZIP archive"
            ) from exc
        if corrupt_member is not None:
            raise MineruDownloadError(
                f"MinerU download has an invalid CRC for {corrupt_member!r}"
            )


def _required_str(payload: dict[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MineruProtocolError(f"{context} is missing non-empty string {key!r}")
    return value


def _required_list(payload: dict[str, Any], key: str, context: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise MineruProtocolError(f"{context} is missing list {key!r}")
    return value


def _upload_url(value: Any, index: int) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return _required_str(value, "url", f"file_urls[{index}]")
    raise MineruProtocolError(
        f"file_urls[{index}] must be a non-empty URL string or object"
    )


def _result_for_file(
    results: Sequence[MineruFileResult], file_name: str
) -> MineruFileResult:
    for result in results:
        if result.file_name == file_name:
            return result
    raise MineruProtocolError(
        f"MinerU extract-results response is missing file {file_name!r}"
    )


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None or not value.isdecimal():
        return None
    return int(value)


def _http_error(
    operation: str,
    request: httpx.Request,
    response: httpx.Response | None = None,
) -> MineruHTTPError:
    status = f" HTTP {response.status_code}" if response is not None else ""
    return MineruHTTPError(
        f"MinerU {operation} request failed{status}: {_safe_url(request.url)}"
    )


def _safe_url(url: httpx.URL) -> str:
    host = url.host or "unknown-host"
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{host}{port}{url.path or '/'}"


def _safe_api_code(code: int | str) -> int | str:
    if isinstance(code, str) and _SAFE_API_CODE_RE.fullmatch(code) is None:
        return "<redacted>"
    return code
