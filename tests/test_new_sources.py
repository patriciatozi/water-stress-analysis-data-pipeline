from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from water_stress.config import Settings
from water_stress.http import HttpDownload, HttpResponse
from water_stress.ingestion import mapbiomas, sentinel_2, soilgrids
from water_stress.models import IngestionState
from water_stress.storage import LocalStorageClient, StorageClient

TIFF = b"II*\x00" + b"test-tiff"


class SourceHttpClient:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, **kwargs: object) -> HttpResponse:
        self.calls.append(("GET", kwargs))
        return HttpResponse(self.responses.pop(0), 200, "image/tiff", str(kwargs["url"]))

    def post(self, **kwargs: object) -> HttpResponse:
        self.calls.append(("POST", kwargs))
        return HttpResponse(self.responses.pop(0), 200, "application/geo+json", str(kwargs["url"]))

    def download(self, **kwargs: object) -> HttpDownload:
        self.calls.append(("DOWNLOAD", kwargs))
        content = self.responses.pop(0)
        storage = cast(StorageClient, kwargs["storage"])
        checksum, size, header = storage.write_chunks(cast(Path, kwargs["path"]), [content])
        return HttpDownload(200, "image/tiff", str(kwargs["url"]), checksum, size, header)


def stac_item(
    item_id: str, acquired: str, cloud: float, geometry: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": item_id,
        "geometry": geometry,
        "properties": {"datetime": f"{acquired}T10:00:00Z", "eo:cloud_cover": cloud},
        "assets": {
            asset: {"href": f"https://example.test/{item_id}/{asset}.tif"}
            for asset in ("red", "nir", "swir16", "scl")
        },
    }


def test_soilgrids_builds_requests_and_downloads_twelve_files(
    settings: Settings, polygon_geojson: bytes
) -> None:
    http = SourceHttpClient([TIFF] * 12)
    results = soilgrids.ingest(
        settings,
        boundary=polygon_geojson,
        http=http,
        storage=LocalStorageClient(),
    )

    assert len(results) == 12
    assert all(result.state is IngestionState.DOWNLOADED for result in results)
    assert all(result.artifact_path.read_bytes() == TIFF for result in results)
    first_params = http.calls[0][1]["params"]
    assert first_params["map"] == "/map/clay.map"  # type: ignore[index]
    assert first_params["COVERAGEID"] == "clay_0-5cm_Q0.5"  # type: ignore[index]
    assert first_params["REQUEST"] == "GetCoverage"  # type: ignore[index]


def test_soilgrids_rejects_invalid_tiff() -> None:
    with pytest.raises(ValueError, match="not a valid TIFF or BigTIFF"):
        soilgrids.validate_tiff(b"invalid")


@pytest.mark.parametrize("signature", [b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"])
def test_tiff_validator_accepts_classic_tiff_and_bigtiff(signature: bytes) -> None:
    soilgrids.validate_tiff(signature + b"metadata")


def test_mapbiomas_builds_official_url_and_downloads_classification(
    settings: Settings,
) -> None:
    http = SourceHttpClient([TIFF])

    result = mapbiomas.ingest(settings, http=http, storage=LocalStorageClient())

    assert mapbiomas.build_url(settings) == (
        "https://storage.googleapis.com/mapbiomas-public/initiatives/brasil/"
        "collection_10/lulc/coverage/brazil_coverage_2023.tif"
    )
    assert result.artifact_path == (
        settings.storage.root_path
        / "mapbiomas/land_cover/collection=10/year=2023/brazil_coverage_2023.tif"
    )
    assert result.artifact_path.read_bytes() == TIFF
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["target_class_code"] == 39
    assert manifest["dataset"] == "annual_land_cover_classification"


def test_mapbiomas_rejects_invalid_tiff_and_removes_artifact(settings: Settings) -> None:
    storage = LocalStorageClient()

    with pytest.raises(ValueError, match="not a valid TIFF or BigTIFF"):
        mapbiomas.ingest(
            settings,
            http=SourceHttpClient([b"invalid"]),
            storage=storage,
        )

    assert not storage.exists(mapbiomas.artifact_path(settings))


def test_date_windows_cover_period_without_gaps() -> None:
    windows = sentinel_2.date_windows(date(2023, 9, 1), date(2024, 4, 30), 3)

    assert windows[0][0] == date(2023, 9, 1)
    assert windows[-1][1] == date(2024, 4, 30)
    assert windows[0][1] + timedelta(days=1) == windows[1][0]


def test_selects_best_intersection_then_cloud_per_window(
    settings: Settings, polygon_geojson: bytes
) -> None:
    full = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]}
    half = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 4], [0, 4], [0, 0]]]}
    items = [
        stac_item("early-half", "2023-09-10", 1, half),
        stac_item("early-full", "2023-09-11", 20, full),
        stac_item("middle", "2023-12-15", 5, full),
        stac_item("late", "2024-03-01", 3, full),
    ]

    selected = sentinel_2.select_representative_items(
        settings,
        boundary=polygon_geojson,
        items=items,
    )

    assert [item["id"] for _, item in selected] == ["early-full", "middle", "late"]


def test_sentinel_ingests_catalog_and_twelve_assets(
    settings: Settings, polygon_geojson: bytes
) -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]],
    }
    items = [
        stac_item("early", "2023-09-10", 5, geometry),
        stac_item("middle", "2023-12-15", 4, geometry),
        stac_item("late", "2024-03-01", 3, geometry),
    ]
    catalog = json.dumps({"type": "FeatureCollection", "features": items, "links": []}).encode()
    http = SourceHttpClient([catalog, *([TIFF] * 12)])

    results = sentinel_2.ingest(
        settings,
        boundary=polygon_geojson,
        http=http,
        storage=LocalStorageClient(),
    )

    assert len(results) == 13
    assert results[0].artifact_path.name == "search-results.json"
    assert {result.artifact_path.name for result in results[1:]} == {
        "red.tif",
        "nir.tif",
        "swir16.tif",
        "scl.tif",
    }


def test_sentinel_fails_when_required_asset_is_missing(
    settings: Settings, polygon_geojson: bytes
) -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]],
    }
    items = [
        stac_item("early", "2023-09-10", 5, geometry),
        stac_item("middle", "2023-12-15", 4, geometry),
        stac_item("late", "2024-03-01", 3, geometry),
    ]
    del items[0]["assets"]["red"]
    catalog = json.dumps({"type": "FeatureCollection", "features": items, "links": []}).encode()

    with pytest.raises(ValueError, match="missing asset red"):
        sentinel_2.ingest(
            settings,
            boundary=polygon_geojson,
            http=SourceHttpClient([catalog]),
            storage=LocalStorageClient(),
        )
