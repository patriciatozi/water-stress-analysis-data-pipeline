from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from shapely.geometry import shape

from water_stress.config import Settings
from water_stress.http import HttpGetter
from water_stress.ingestion import ibge
from water_stress.ingestion.common import (
    fingerprint,
    persist_download,
    persist_download_manifest,
    reusable_result,
    versioned_paths,
)
from water_stress.ingestion.soilgrids import validate_tiff
from water_stress.models import IngestionResult, IngestionState
from water_stress.storage import StorageClient

SOURCE = "sentinel_2"


def build_search_body(settings: Settings, boundary: bytes) -> dict[str, Any]:
    document = ibge.validate_geojson(boundary)
    if document["type"] == "FeatureCollection":
        geometry = document["features"][0]["geometry"]
    elif document["type"] == "Feature":
        geometry = document["geometry"]
    else:
        geometry = document
    return {
        "collections": [settings.sentinel_2.collection],
        "intersects": geometry,
        "datetime": (
            f"{settings.study.start_date.isoformat()}T00:00:00Z/"
            f"{settings.study.end_date.isoformat()}T23:59:59Z"
        ),
        "query": {"eo:cloud_cover": {"lte": settings.sentinel_2.max_cloud_cover}},
        "limit": settings.sentinel_2.page_limit,
    }


def _validate_feature_collection(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Sentinel-2 STAC response is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ValueError("Sentinel-2 STAC response must be a FeatureCollection")
    if not isinstance(document.get("features"), list):
        raise ValueError("Sentinel-2 STAC response has no feature list")
    return document


def search_items(
    settings: Settings, *, boundary: bytes, http: HttpGetter
) -> tuple[bytes, list[dict[str, Any]], dict[str, Any]]:
    body = build_search_body(settings, boundary)
    url = str(settings.sentinel_2.search_url)
    items: dict[str, dict[str, Any]] = {}
    while True:
        response = http.post(source=SOURCE, url=url, json_body=body)
        page = _validate_feature_collection(response.content)
        for item in page["features"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("Sentinel-2 STAC item has no valid id")
            items[item["id"]] = item
        next_link = next(
            (
                link
                for link in page.get("links", [])
                if isinstance(link, dict) and link.get("rel") == "next"
            ),
            None,
        )
        if next_link is None:
            break
        url = str(next_link["href"])
        method = str(next_link.get("method", "GET")).upper()
        if method == "POST":
            body = next_link.get("body", body)
        else:
            response = http.get(source=SOURCE, url=url)
            page = _validate_feature_collection(response.content)
            for item in page["features"]:
                items[item["id"]] = item
            break
    ordered = sorted(items.values(), key=lambda item: (item["properties"]["datetime"], item["id"]))
    catalog = {
        "type": "FeatureCollection",
        "features": ordered,
        "query": body,
        "source": str(settings.sentinel_2.search_url),
    }
    return (
        (json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),
        ordered,
        build_search_body(settings, boundary),
    )


def date_windows(start: date, end: date, count: int) -> list[tuple[date, date]]:
    total_days = (end - start).days + 1
    windows: list[tuple[date, date]] = []
    for index in range(count):
        window_start = start + timedelta(days=(total_days * index) // count)
        window_end = start + timedelta(days=(total_days * (index + 1)) // count - 1)
        windows.append((window_start, window_end))
    return windows


def select_representative_items(
    settings: Settings, *, boundary: bytes, items: list[dict[str, Any]]
) -> list[tuple[tuple[date, date], dict[str, Any]]]:
    aoi = ibge.geometry_from_geojson(boundary)
    selected: list[tuple[tuple[date, date], dict[str, Any]]] = []
    for window in date_windows(
        settings.study.start_date,
        settings.study.end_date,
        settings.sentinel_2.representative_scenes,
    ):
        eligible: list[tuple[float, float, str, str, dict[str, Any]]] = []
        for item in items:
            acquired = datetime.fromisoformat(
                item["properties"]["datetime"].replace("Z", "+00:00")
            ).date()
            if not window[0] <= acquired <= window[1]:
                continue
            footprint = shape(item["geometry"])
            intersection_ratio = footprint.intersection(aoi).area / aoi.area
            if intersection_ratio <= 0:
                continue
            cloud_cover = float(item["properties"].get("eo:cloud_cover", 100))
            eligible.append(
                (-intersection_ratio, cloud_cover, acquired.isoformat(), item["id"], item)
            )
        if not eligible:
            raise ValueError(f"No eligible Sentinel-2 scene for window {window[0]} to {window[1]}")
        eligible.sort(key=lambda candidate: candidate[:4])
        selected.append((window, eligible[0][4]))
    return selected


def catalog_path(settings: Settings) -> Path:
    return (
        settings.storage.root_path
        / "sentinel_2"
        / "l2a"
        / f"municipality_code={settings.study.municipality_code}"
        / f"start_date={settings.study.start_date.isoformat()}"
        / f"end_date={settings.study.end_date.isoformat()}"
        / "search-results.json"
    )


def asset_path(settings: Settings, item_id: str, asset: str) -> Path:
    return (
        settings.storage.root_path
        / "sentinel_2"
        / "l2a"
        / f"municipality_code={settings.study.municipality_code}"
        / f"item_id={item_id}"
        / f"{asset}.tif"
    )


def ingest(
    settings: Settings,
    *,
    boundary: bytes,
    http: HttpGetter,
    storage: StorageClient,
    force: bool = False,
    dry_run: bool = False,
) -> list[IngestionResult]:
    path, manifest_path = versioned_paths(catalog_path(settings), force=force)
    if dry_run:
        return [IngestionResult(SOURCE, path, manifest_path, None, None, IngestionState.PLANNED)]
    search_body = build_search_body(settings, boundary)
    search_fingerprint = fingerprint(
        {"url": str(settings.sentinel_2.search_url), "body": search_body}
    )
    catalog_result = None
    if not force:
        catalog_result = reusable_result(
            source=SOURCE,
            storage=storage,
            artifact_path=path,
            manifest_path=manifest_path,
            request_fingerprint=search_fingerprint,
        )
    if catalog_result:
        saved_catalog = _validate_feature_collection(storage.read_bytes(path))
        items = saved_catalog["features"]
    else:
        catalog_bytes, items, original_body = search_items(settings, boundary=boundary, http=http)
        catalog_result = persist_download(
            source=SOURCE,
            storage=storage,
            artifact_path=path,
            manifest_path=manifest_path,
            content=catalog_bytes,
            manifest={
                "source": SOURCE,
                "dataset": "stac_search",
                "url": str(settings.sentinel_2.search_url),
                "parameters": original_body,
                "item_count": len(items),
                "http_status": 200,
                "municipality_code": settings.study.municipality_code,
                "project_version": settings.project.version,
                "config_sha256": settings.config_hash,
                "request_fingerprint": search_fingerprint,
            },
        )
    results = [catalog_result]
    for window, item in select_representative_items(settings, boundary=boundary, items=items):
        for asset_name in settings.sentinel_2.assets:
            asset = item.get("assets", {}).get(asset_name)
            if not isinstance(asset, dict) or not asset.get("href"):
                raise ValueError(f"Sentinel-2 item {item['id']} is missing asset {asset_name}")
            url = str(asset["href"])
            request_fingerprint = fingerprint({"url": url, "item_id": item["id"]})
            output, asset_manifest = versioned_paths(
                asset_path(settings, item["id"], asset_name), force=force
            )
            if not force:
                reused = reusable_result(
                    source=SOURCE,
                    storage=storage,
                    artifact_path=output,
                    manifest_path=asset_manifest,
                    request_fingerprint=request_fingerprint,
                )
                if reused:
                    results.append(reused)
                    continue
            response = http.download(source=SOURCE, url=url, storage=storage, path=output)
            try:
                validate_tiff(response.header)
            except ValueError:
                storage.delete(output)
                raise
            results.append(
                persist_download_manifest(
                    source=SOURCE,
                    storage=storage,
                    artifact_path=output,
                    manifest_path=asset_manifest,
                    checksum=response.checksum,
                    size_bytes=response.size_bytes,
                    manifest={
                        "source": SOURCE,
                        "dataset": "sentinel_2_l2a_asset",
                        "item_id": item["id"],
                        "asset": asset_name,
                        "url": url,
                        "final_url": response.final_url,
                        "window_start": window[0].isoformat(),
                        "window_end": window[1].isoformat(),
                        "acquired_at": item["properties"]["datetime"],
                        "cloud_cover": item["properties"].get("eo:cloud_cover"),
                        "http_status": response.status_code,
                        "content_type": response.content_type,
                        "municipality_code": settings.study.municipality_code,
                        "project_version": settings.project.version,
                        "config_sha256": settings.config_hash,
                        "request_fingerprint": request_fingerprint,
                    },
                )
            )
    return results
