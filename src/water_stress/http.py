from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from water_stress.config import HttpSettings

LOGGER = logging.getLogger(__name__)


class HttpRequestError(RuntimeError):
    """Raised after an HTTP request exhausts its retry policy."""


@dataclass(frozen=True)
class HttpResponse:
    content: bytes
    status_code: int
    content_type: str | None
    final_url: str


class HttpGetter(Protocol):
    def get(
        self,
        *,
        source: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


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
        last_error: Exception | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                response = self._client.get(url, params=params, headers=headers)
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
