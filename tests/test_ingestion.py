from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from water_stress.config import Settings
from water_stress.http import HttpDownload, HttpResponse
from water_stress.ingestion import ibge, nasa_power
from water_stress.models import IngestionState
from water_stress.storage import LocalStorageClient, StorageClient


class StubHttpClient:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> HttpResponse:
        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return HttpResponse(content, 200, "application/json", str(kwargs["url"]))

    def post(self, **kwargs: object) -> HttpResponse:
        return self.get(**kwargs)

    def download(self, **kwargs: object) -> HttpDownload:
        content = self.responses.pop(0)
        storage = cast(StorageClient, kwargs["storage"])
        path = cast(Path, kwargs["path"])
        checksum, size, header = storage.write_chunks(path, [content])
        return HttpDownload(200, "image/tiff", str(kwargs["url"]), checksum, size, header)


def test_builds_exact_ibge_request(settings: Settings) -> None:
    url, params, headers = ibge.build_request(settings)

    assert url.endswith("/5107925")
    assert params == {"formato": "application/vnd.geo+json", "qualidade": "minima"}
    assert headers == {"Accept": "application/vnd.geo+json"}


def test_builds_exact_nasa_request(settings: Settings) -> None:
    url, params = nasa_power.build_request(settings, latitude=-12.5, longitude=-55.7)

    assert url.endswith("/daily/point")
    assert params == {
        "parameters": "T2M,T2M_MAX,T2M_MIN,RH2M,WS2M,ALLSKY_SFC_SW_DWN,PRECTOTCORR",
        "community": "AG",
        "longitude": -55.7,
        "latitude": -12.5,
        "start": "20230901",
        "end": "20240430",
        "format": "JSON",
        "time-standard": "UTC",
    }


def test_calculates_internal_representative_point(polygon_geojson: bytes) -> None:
    latitude, longitude = ibge.representative_point(polygon_geojson)

    assert (latitude, longitude) == (2.0, 2.0)


@pytest.mark.parametrize(
    ("validator", "content", "message"),
    [
        (ibge.validate_geojson, b"not-json", "not valid JSON"),
        (ibge.validate_geojson, b'{"type":"Unknown"}', "not a supported"),
        (nasa_power.validate_response, b"[]", "must be a JSON object"),
        (nasa_power.validate_response, b'{"properties":{}}', "no daily parameter"),
    ],
)
def test_rejects_invalid_source_responses(validator: object, content: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validator(content)  # type: ignore[operator]


def test_ibge_preserves_bytes_creates_manifest_and_reuses(
    settings: Settings, polygon_geojson: bytes
) -> None:
    storage = LocalStorageClient()
    http = StubHttpClient([polygon_geojson])

    first = ibge.ingest(settings, http=http, storage=storage)
    second = ibge.ingest(settings, http=http, storage=storage)

    assert first.state is IngestionState.DOWNLOADED
    assert second.state is IngestionState.REUSED
    assert first.artifact_path.read_bytes() == polygon_geojson
    manifest = json.loads(first.manifest_path.read_text())
    assert manifest["sha256"] == first.checksum
    assert manifest["municipality_code"] == "5107925"
    assert manifest["size_bytes"] == len(polygon_geojson)
    assert len(http.calls) == 1


def test_nasa_preserves_bytes_and_force_creates_new_version(
    settings: Settings, nasa_json: bytes
) -> None:
    storage = LocalStorageClient()
    http = StubHttpClient([nasa_json, nasa_json])

    first = nasa_power.ingest(
        settings,
        latitude=-12.5,
        longitude=-55.7,
        http=http,
        storage=storage,
    )
    forced = nasa_power.ingest(
        settings,
        latitude=-12.5,
        longitude=-55.7,
        http=http,
        storage=storage,
        force=True,
    )

    assert first.artifact_path.read_bytes() == nasa_json
    assert forced.state is IngestionState.DOWNLOADED
    assert forced.artifact_path != first.artifact_path
    assert first.artifact_path.exists()
    assert forced.artifact_path.exists()


def test_corrupt_artifact_is_not_reused(settings: Settings, polygon_geojson: bytes) -> None:
    storage = LocalStorageClient()
    http = StubHttpClient([polygon_geojson, polygon_geojson])
    first = ibge.ingest(settings, http=http, storage=storage)
    first.artifact_path.write_bytes(b"corrupt")

    result = ibge.ingest(settings, http=http, storage=storage)

    assert result.state is IngestionState.DOWNLOADED
    assert result.artifact_path.read_bytes() == polygon_geojson


def test_dry_run_does_not_call_http(settings: Settings) -> None:
    http = StubHttpClient([])

    result = ibge.ingest(
        settings,
        http=http,
        storage=LocalStorageClient(),
        dry_run=True,
    )

    assert result.state is IngestionState.PLANNED
    assert http.calls == []
    assert not result.artifact_path.exists()
