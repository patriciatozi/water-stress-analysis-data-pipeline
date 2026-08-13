from pathlib import Path

import httpx
import pytest

from water_stress.config import HttpSettings
from water_stress.http import HttpClient, HttpRequestError
from water_stress.storage import LocalStorageClient


def http_settings(*, attempts: int = 3) -> HttpSettings:
    return HttpSettings(timeout_seconds=1, max_attempts=attempts, backoff_seconds=0.5)


def test_returns_response_metadata() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=b"raw", headers={"content-type": "application/json"}, request=request
        )
    )
    client = httpx.Client(transport=transport)

    response = HttpClient(http_settings(), client=client).get(
        source="test", url="https://example.test/data", params={"a": "b"}
    )

    assert response.content == b"raw"
    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.final_url == "https://example.test/data?a=b"


def test_retries_server_errors_with_exponential_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        status = 503 if attempts < 3 else 200
        return httpx.Response(status, content=b"ok", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = HttpClient(http_settings(), client=client, sleep=delays.append).get(
        source="test", url="https://example.test"
    )

    assert response.content == b"ok"
    assert attempts == 3
    assert delays == [0.5, 1.0]


def test_does_not_retry_client_errors() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(HttpRequestError, match="after 3 attempt"):
        HttpClient(http_settings(), client=client).get(source="test", url="https://example.test")
    assert attempts == 1


def test_retries_timeout_then_raises() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(HttpRequestError):
        HttpClient(http_settings(attempts=2), client=client, sleep=lambda _: None).get(
            source="test", url="https://example.test"
        )
    assert attempts == 2


def test_streams_download_to_storage(tmp_path: Path) -> None:
    content = b"II*\x00" + (b"chunk" * 1000)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=content,
            headers={"content-type": "image/tiff"},
            request=request,
        )
    )
    output = tmp_path / "asset.tif"

    result = HttpClient(http_settings(), client=httpx.Client(transport=transport)).download(
        source="sentinel_2",
        url="https://example.test/asset.tif",
        storage=LocalStorageClient(),
        path=output,
    )

    assert output.read_bytes() == content
    assert result.size_bytes == len(content)
    assert result.header.startswith(b"II*\x00")
    assert len(result.checksum) == 64
