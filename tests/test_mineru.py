"""Focused tests for the MinerU official cloud API client."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from epubforge.config import Config, MineruSettings, load_config
from epubforge.mineru import (
    MineruAPIError,
    MineruBatch,
    MineruClient,
    MineruDownloadError,
    MineruHTTPError,
    MineruJobFailedError,
    MineruPollTimeoutError,
    MineruUpload,
)


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.md", "# Converted")
    return buffer.getvalue()


def _crc_corrupted_zip_bytes() -> bytes:
    content = bytearray(_zip_bytes())
    content[content.index(b"# Converted")] ^= 1
    return bytes(content)


def _settings(
    *,
    max_polls: int = 300,
    max_download_bytes: int = 2 * 1024**3,
    max_uncompressed_bytes: int = 8 * 1024**3,
) -> MineruSettings:
    return MineruSettings(
        api_key="secret",
        base_url="https://api.test/api/v4",
        max_polls=max_polls,
        max_download_bytes=max_download_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )


def _no_sleep(_: float) -> None:
    return None


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    settings: MineruSettings,
    sleeps: list[float] | None = None,
) -> MineruClient:
    transport = httpx.MockTransport(handler)

    def record_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return MineruClient(
        settings,
        client=httpx.Client(transport=transport),
        sleep=record_sleep if sleeps is not None else _no_sleep,
    )


class TestMineruConfig:
    def test_toml_and_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            """[mineru]
api_key = ""
model_version = "pipeline"
timeout_seconds = 45
poll_interval_seconds = 0.5
max_polls = 12
max_download_bytes = 100
max_uncompressed_bytes = 200
is_ocr = true
enable_formula = false
enable_table = false
language = "en"
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("EPUBFORGE_MINERU_API_KEY", "env-key")
        monkeypatch.setenv("EPUBFORGE_MINERU_MAX_POLLS", "20")
        monkeypatch.setenv("EPUBFORGE_MINERU_MAX_DOWNLOAD_BYTES", "300")
        monkeypatch.setenv("EPUBFORGE_MINERU_MAX_UNCOMPRESSED_BYTES", "400")

        cfg = load_config(config_path)

        assert cfg.mineru.api_key == "env-key"
        assert cfg.mineru.model_version == "pipeline"
        assert cfg.mineru.max_polls == 20
        assert cfg.mineru.max_download_bytes == 300
        assert cfg.mineru.max_uncompressed_bytes == 400
        assert cfg.mineru.is_ocr is True
        assert cfg.mineru.enable_formula is False
        assert cfg.mineru.language == "en"

    def test_empty_api_key_is_preserved(self) -> None:
        cfg = Config(mineru=MineruSettings(api_key=""))
        assert cfg.mineru.api_key == ""
        with pytest.raises(SystemExit, match="EPUBFORGE_MINERU_API_KEY"):
            cfg.require_mineru()

    def test_invalid_mineru_values_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MineruSettings.model_validate({"max_polls": 0})
        with pytest.raises(ValidationError):
            MineruSettings.model_validate({"max_download_bytes": 0})
        with pytest.raises(ValidationError):
            MineruSettings.model_validate({"model_version": "unknown"})
        with pytest.raises(ValidationError):
            MineruSettings.model_validate({"unexpected": True})


class TestMineruClient:
    def test_process_file_uploads_polls_and_saves_zip(self, tmp_path: Path) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"%PDF-1.7")
        destination = tmp_path / "nested" / "result.zip"
        calls: list[httpx.Request] = []
        polls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal polls
            calls.append(request)
            if request.method == "POST":
                assert request.headers["Authorization"] == "Bearer secret"
                payload = json.loads(request.content)
                assert payload == {
                    "model_version": "vlm",
                    "enable_formula": True,
                    "enable_table": True,
                    "language": "ch",
                    "files": [{"name": "book.pdf", "is_ocr": False}],
                }
                return httpx.Response(
                    200,
                    json={
                        "code": "0",
                        "data": {
                            "batch_id": "batch-1",
                            "file_urls": ["https://storage.test/uploads/book.pdf"],
                        },
                    },
                )
            if request.method == "PUT":
                assert request.url.host == "storage.test"
                assert "authorization" not in request.headers
                assert request.content == b"%PDF-1.7"
                return httpx.Response(200)
            if request.url.path.endswith("/batch/batch-1"):
                polls += 1
                state = "processing" if polls == 1 else "done"
                result = {"file_name": "book.pdf", "state": state}
                if state == "done":
                    result["full_zip_url"] = "https://storage.test/results/book.zip"
                return httpx.Response(
                    200, json={"code": 0, "data": {"extract_result": [result]}}
                )
            if request.url.path == "/results/book.zip":
                assert "authorization" not in request.headers
                return httpx.Response(
                    200, content=_zip_bytes(), headers={"Content-Type": "text/plain"}
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        sleeps: list[float] = []
        result = _client(handler, _settings(), sleeps).process_file(source, destination)

        assert result.batch_id == "batch-1"
        assert result.file_name == "book.pdf"
        assert result.zip_path == destination
        assert zipfile.is_zipfile(destination)
        assert sleeps == [2.0]
        assert [request.method for request in calls] == [
            "POST",
            "PUT",
            "GET",
            "GET",
            "GET",
        ]

    def test_api_error_stops_before_upload(self, tmp_path: Path) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(
                200, json={"code": 1004, "msg": "invalid key", "data": {}}
            )

        with pytest.raises(MineruAPIError, match="1004"):
            _client(handler, _settings()).process_file(source, tmp_path / "result.zip")

    @pytest.mark.parametrize("code", ["A0202", "-60010"])
    def test_string_api_error_code_raises(self, tmp_path: Path, code: str) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(
                200, json={"code": code, "msg": "invalid key", "data": {}}
            )

        with pytest.raises(MineruAPIError) as exc_info:
            _client(handler, _settings()).process_file(source, tmp_path / "result.zip")
        assert code in str(exc_info.value)

    def test_malicious_string_api_code_is_redacted(self, tmp_path: Path) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")
        malicious_code = (
            "Bearer api-key-secret\n"
            "https://store.test/result.zip?X-Amz-Signature=url-secret"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(200, json={"code": malicious_code, "data": {}})

        with pytest.raises(MineruAPIError) as exc_info:
            _client(handler, _settings()).process_file(source, tmp_path / "result.zip")
        message = str(exc_info.value)
        assert "MinerU API /file-urls/batch" in message
        assert "<redacted>" in message
        assert "api-key-secret" not in message
        assert "X-Amz-Signature" not in message
        assert "Bearer" not in message
        assert "?" not in message

    def test_api_error_redacts_server_message(self, tmp_path: Path) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")
        leaked_message = (
            "Authorization: Bearer api-key-secret "
            "https://store.test/result.zip?X-Amz-Signature=url-secret"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(
                200, json={"code": "1004", "msg": leaked_message, "data": {}}
            )

        with pytest.raises(MineruAPIError) as exc_info:
            _client(handler, _settings()).process_file(source, tmp_path / "result.zip")
        message = str(exc_info.value)
        assert "MinerU API /file-urls/batch" in message
        assert "1004" in message
        assert "api-key-secret" not in message
        assert "X-Amz-Signature" not in message
        assert "?" not in message
        assert "Bearer" not in message

    def test_upload_batch_maps_unordered_paths_to_targets(self, tmp_path: Path) -> None:
        first = tmp_path / "first.pdf"
        second = tmp_path / "second.pdf"
        first.write_bytes(b"first content")
        second.write_bytes(b"second content")
        batch = MineruBatch(
            batch_id="batch",
            uploads=(
                MineruUpload("second.pdf", "https://store.test/uploads/second"),
                MineruUpload("first.pdf", "https://store.test/uploads/first"),
            ),
        )
        uploaded: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PUT"
            assert "authorization" not in request.headers
            uploaded[request.url.path] = request.content
            return httpx.Response(200)

        _client(handler, _settings()).upload_batch(batch, [first, second])

        assert uploaded == {
            "/uploads/first": b"first content",
            "/uploads/second": b"second content",
        }

    def test_upload_batch_rejects_mismatches_before_put(self, tmp_path: Path) -> None:
        first = tmp_path / "first.pdf"
        second = tmp_path / "second.pdf"
        extra = tmp_path / "extra.pdf"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        extra.write_bytes(b"extra")
        calls: list[httpx.Request] = []
        batch = MineruBatch(
            batch_id="batch",
            uploads=(
                MineruUpload("first.pdf", "https://store.test/first"),
                MineruUpload("second.pdf", "https://store.test/second"),
            ),
        )
        duplicate_targets = MineruBatch(
            batch_id="batch",
            uploads=(
                MineruUpload("first.pdf", "https://store.test/first-a"),
                MineruUpload("first.pdf", "https://store.test/first-b"),
            ),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise AssertionError("invalid upload batch must not send a PUT")

        client = _client(handler, _settings())
        with pytest.raises(ValueError, match="do not match"):
            client.upload_batch(batch, [first, extra])
        with pytest.raises(ValueError, match="unique file basenames"):
            client.upload_batch(batch, [first, first])
        with pytest.raises(ValueError, match="unique file names"):
            client.upload_batch(duplicate_targets, [first, second])
        assert calls == []

    def test_duplicate_basenames_are_rejected_before_request(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "one" / "book.pdf"
        second = tmp_path / "two" / "book.pdf"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise AssertionError("duplicate basenames must not send a request")

        with pytest.raises(ValueError, match="unique file basenames"):
            _client(handler, _settings()).request_file_urls([first, second])
        assert calls == []

    def test_failed_result_raises(self, tmp_path: Path) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "b",
                            "file_urls": ["https://store.test/u"],
                        },
                    },
                )
            if request.method == "PUT":
                return httpx.Response(200)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [{"file_name": "book.pdf", "state": "failed"}]
                    },
                },
            )

        with pytest.raises(MineruJobFailedError):
            _client(handler, _settings()).process_file(source, tmp_path / "result.zip")

    def test_poll_timeout_raises_after_configured_attempts(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")
        polls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal polls
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "b",
                            "file_urls": ["https://store.test/u"],
                        },
                    },
                )
            if request.method == "PUT":
                return httpx.Response(200)
            polls += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"file_name": "book.pdf", "state": "processing"}
                        ]
                    },
                },
            )

        sleeps: list[float] = []
        with pytest.raises(MineruPollTimeoutError):
            _client(handler, _settings(max_polls=2), sleeps).process_file(
                source, tmp_path / "result.zip"
            )
        assert polls == 2
        assert sleeps == [2.0]

    def test_bad_zip_does_not_replace_existing_destination(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")
        destination = tmp_path / "result.zip"
        destination.write_bytes(b"previous archive")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "b",
                            "file_urls": ["https://store.test/u"],
                        },
                    },
                )
            if request.method == "PUT":
                return httpx.Response(200)
            if request.url.path.endswith("/batch/b"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "extract_result": [
                                {
                                    "file_name": "book.pdf",
                                    "state": "done",
                                    "full_zip_url": "https://store.test/result.zip",
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(
                200, content=b"not a zip", headers={"Content-Type": "application/zip"}
            )

        with pytest.raises(MineruDownloadError):
            _client(handler, _settings()).process_file(source, destination)
        assert destination.read_bytes() == b"previous archive"
        assert list(tmp_path.glob(".result.zip.*")) == []

    def test_crc_corruption_does_not_replace_destination(self, tmp_path: Path) -> None:
        destination = tmp_path / "result.zip"
        destination.write_bytes(b"previous archive")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(200, content=_crc_corrupted_zip_bytes())

        with pytest.raises(MineruDownloadError, match="invalid CRC"):
            _client(handler, _settings()).download_zip(
                "https://store.test/result.zip", destination
            )
        assert destination.read_bytes() == b"previous archive"
        assert list(tmp_path.glob(".result.zip.*")) == []

    def test_download_byte_limit_preserves_destination(self, tmp_path: Path) -> None:
        destination = tmp_path / "result.zip"
        destination.write_bytes(b"previous archive")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(
                200, content=b"12345", headers={"Content-Length": "4"}
            )

        with pytest.raises(MineruDownloadError, match="max_download_bytes"):
            _client(handler, _settings(max_download_bytes=4)).download_zip(
                "https://store.test/result.zip", destination
            )
        assert destination.read_bytes() == b"previous archive"
        assert list(tmp_path.glob(".result.zip.*")) == []

    def test_uncompressed_size_limit_preserves_destination(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / "result.zip"
        destination.write_bytes(b"previous archive")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(200, content=_zip_bytes())

        with pytest.raises(MineruDownloadError, match="max_uncompressed_bytes"):
            _client(handler, _settings(max_uncompressed_bytes=1)).download_zip(
                "https://store.test/result.zip", destination
            )
        assert destination.read_bytes() == b"previous archive"
        assert list(tmp_path.glob(".result.zip.*")) == []

    @pytest.mark.parametrize(
        ("operation", "url", "status"),
        [
            (
                "upload",
                "https://store.test/upload?X-Amz-Signature=upload-secret#fragment-secret",
                403,
            ),
            (
                "download",
                "https://store.test/result.zip?X-Amz-Signature=download-secret#fragment-secret",
                503,
            ),
        ],
    )
    def test_presigned_http_errors_redact_query_strings(
        self,
        tmp_path: Path,
        operation: str,
        url: str,
        status: int,
    ) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=b"Bearer response-body-secret")

        client = _client(handler, _settings())
        with pytest.raises(MineruHTTPError) as exc_info:
            if operation == "upload":
                client.upload_file(source, MineruUpload("book.pdf", url))
            else:
                client.download_zip(url, tmp_path / "result.zip")
        message = str(exc_info.value)
        assert f"HTTP {status}" in message
        assert "X-Amz-Signature" not in message
        assert "secret" not in message
        assert "?" not in message
        assert "fragment" not in message
        assert "Bearer" not in message
        assert "response-body-secret" not in message

    def test_transport_errors_redact_presigned_query_string(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "book.pdf"
        source.write_bytes(b"pdf")
        url = (
            "https://store.test/upload?X-Amz-Signature=transport-secret#fragment-secret"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"unable to reach {request.url}", request=request)

        with pytest.raises(MineruHTTPError) as exc_info:
            _client(handler, _settings()).upload_file(
                source, MineruUpload("book.pdf", url)
            )
        message = str(exc_info.value)
        assert "X-Amz-Signature" not in message
        assert "transport-secret" not in message
        assert "?" not in message
        assert "fragment" not in message
