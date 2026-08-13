from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from water_stress.config import HttpSettings
from water_stress.storage import StorageClient

LOGGER = logging.getLogger(__name__)


class HttpRequestError(RuntimeError):
    """Raised after an HTTP request exhausts its retry policy."""


@dataclass(frozen=True)
class HttpResponse:
    content: bytes
    status_code: int
    content_type: str | None
    final_url: str


@dataclass(frozen=True)
class HttpDownload:
    status_code: int
    content_type: str | None
    final_url: str
    checksum: str
    size_bytes: int
    header: bytes


class HttpGetter(Protocol):
    def get(
        self,
        *,
        source: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...

    def post(
        self,
        *,
        source: str,
        url: str,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...

    def download(
        self,
        *,
        source: str,
        url: str,
        storage: StorageClient,
        path: Path,
    ) -> HttpDownload: ...


class HttpClient:
    def __init__(
        self,
        settings: HttpSettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(
        self,
        *,
        source: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self._request(
            source=source,
            method="GET",
            url=url,
            params=params,
            headers=headers,
        )

    def post(
        self,
        *,
        source: str,
        url: str,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self._request(
            source=source,
            method="POST",
            url=url,
            json_body=json_body,
            headers=headers,
        )

    def download(
        self,
        *,
        source: str,
        url: str,
        storage: StorageClient,
        path: Path,
    ) -> HttpDownload:
        last_error: Exception | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                with self._client.stream("GET", url) as response:
                    response.raise_for_status()
                    checksum, size_bytes, header = storage.write_chunks(path, response.iter_bytes())
                    return HttpDownload(
                        status_code=response.status_code,
                        content_type=response.headers.get("content-type"),
                        final_url=str(response.url),
                        checksum=checksum,
                        size_bytes=size_bytes,
                        header=header,
                    )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500
                )
                if not retryable or attempt == self._settings.max_attempts:
                    break
                self._sleep(self._settings.backoff_seconds * (2 ** (attempt - 1)))
        raise HttpRequestError(
            f"{source} download failed after {self._settings.max_attempts} attempt(s)"
        ) from last_error

    def _request(
        self,
        *,
        source: str,
        method: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        last_error: Exception | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                response = self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
                response.raise_for_status()
                return HttpResponse(
                    content=response.content,
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    final_url=str(response.url),
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500
                )
                if not retryable or attempt == self._settings.max_attempts:
                    break
                delay = self._settings.backoff_seconds * (2 ** (attempt - 1))
                LOGGER.warning(
                    "HTTP request failed; retrying",
                    extra={"source": source, "event": "http_retry", "attempt": attempt},
                )
                self._sleep(delay)
        raise HttpRequestError(
            f"{source} request failed after {self._settings.max_attempts} attempt(s)"
        ) from last_error
