from __future__ import annotations

import json
from math import ceil
from pathlib import Path
from typing import Any

from water_stress.config import Settings
from water_stress.http import HttpGetter
from water_stress.ingestion.common import (
    fingerprint,
    persist_download,
    reusable_result,
    versioned_paths,
)
from water_stress.models import IngestionResult, IngestionState
from water_stress.storage import StorageClient

SOURCE = "nasa_power"


def build_request(
    settings: Settings, *, latitude: float, longitude: float
) -> tuple[str, dict[str, str | float]]:
    params: dict[str, str | float] = {
        "parameters": ",".join(settings.nasa_power.parameters),
        "community": settings.nasa_power.community,
        "longitude": longitude,
        "latitude": latitude,
        "start": settings.study.start_date.strftime("%Y%m%d"),
        "end": settings.study.end_date.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": settings.nasa_power.time_standard,
    }
    return str(settings.nasa_power.point_url), params


def artifact_path(settings: Settings) -> Path:
    return (
        settings.storage.root_path
        / "nasa_power"
        / "daily"
        / settings.study.partition_key
        / f"start_date={settings.study.start_date.isoformat()}"
        / f"end_date={settings.study.end_date.isoformat()}"
        / "weather.json"
    )


def validate_response(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("NASA POWER response is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("NASA POWER response must be a JSON object")
    parameters = document.get("properties", {}).get("parameter")
    if isinstance(parameters, dict) and parameters:
        return document
    features = document.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("NASA POWER response has no point or regional parameter data")
    for feature in features:
        if not isinstance(feature, dict):
            raise ValueError("NASA POWER regional response contains an invalid feature")
        coordinates = feature.get("geometry", {}).get("coordinates")
        feature_parameters = feature.get("properties", {}).get("parameter")
        if (
            not isinstance(coordinates, list)
            or len(coordinates) < 2
            or not isinstance(feature_parameters, dict)
            or not feature_parameters
        ):
            raise ValueError("NASA POWER regional response contains an invalid feature")
    return document


def ingest(
    settings: Settings,
    *,
    latitude: float,
    longitude: float,
    http: HttpGetter,
    storage: StorageClient,
    force: bool = False,
    dry_run: bool = False,
) -> IngestionResult:
    url, params = build_request(settings, latitude=latitude, longitude=longitude)
    request_payload = {"url": url, "params": params}
    request_fingerprint = fingerprint(request_payload)
    path, manifest_path = versioned_paths(artifact_path(settings), force=force)
    if dry_run:
        return IngestionResult(SOURCE, path, manifest_path, None, None, IngestionState.PLANNED)
    if not force:
        reused = reusable_result(
            source=SOURCE,
            storage=storage,
            artifact_path=path,
            manifest_path=manifest_path,
            request_fingerprint=request_fingerprint,
        )
        if reused:
            return reused
    response = http.get(source=SOURCE, url=url, params=params)
    validate_response(response.content)
    return persist_download(
        source=SOURCE,
        storage=storage,
        artifact_path=path,
        manifest_path=manifest_path,
        content=response.content,
        manifest={
            "source": SOURCE,
            "dataset": "daily_point_meteorology",
            "url": url,
            "final_url": response.final_url,
            "parameters": params,
            "http_status": response.status_code,
            "content_type": response.content_type,
            "area_type": settings.study.area_type,
            "area_code": settings.study.area_code,
            "area_name": settings.study.area_name,
            "study_start_date": settings.study.start_date.isoformat(),
            "study_end_date": settings.study.end_date.isoformat(),
            "representative_point": {"latitude": latitude, "longitude": longitude},
            "project_version": settings.project.version,
            "config_sha256": settings.config_hash,
            "request_fingerprint": request_fingerprint,
        },
    )


def build_regional_request(
    settings: Settings,
    *,
    parameter: str,
    bbox: tuple[float, float, float, float],
) -> tuple[str, dict[str, str | float]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    return str(settings.nasa_power.regional_url), {
        "parameters": parameter,
        "community": settings.nasa_power.community,
        "longitude-min": min_lon,
        "latitude-min": min_lat,
        "longitude-max": max_lon,
        "latitude-max": max_lat,
        "start": settings.study.start_date.strftime("%Y%m%d"),
        "end": settings.study.end_date.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": settings.nasa_power.time_standard,
    }


def regional_bboxes(
    bbox: tuple[float, float, float, float], max_degrees: float
) -> list[tuple[str, tuple[float, float, float, float]]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    longitude_span = max_lon - min_lon
    latitude_span = max_lat - min_lat
    if longitude_span < 2 or latitude_span < 2:
        raise ValueError("NASA POWER regional bounding boxes require at least a 2 degree span")
    columns = ceil(longitude_span / max_degrees)
    rows = ceil(latitude_span / max_degrees)
    longitude_step = longitude_span / columns
    latitude_step = latitude_span / rows
    return [
        (
            f"r{row:03d}_c{column:03d}",
            (
                min_lon + column * longitude_step,
                min_lat + row * latitude_step,
                min_lon + (column + 1) * longitude_step,
                min_lat + (row + 1) * latitude_step,
            ),
        )
        for row in range(rows)
        for column in range(columns)
    ]


def regional_artifact_path(settings: Settings, parameter: str, region_id: str) -> Path:
    return (
        settings.storage.root_path
        / "nasa_power"
        / "daily_regional"
        / settings.study.partition_key
        / f"start_date={settings.study.start_date.isoformat()}"
        / f"end_date={settings.study.end_date.isoformat()}"
        / f"parameter={parameter.lower()}"
        / f"region_id={region_id}"
        / "weather.json"
    )


def ingest_region(
    settings: Settings,
    *,
    bbox: tuple[float, float, float, float],
    http: HttpGetter,
    storage: StorageClient,
    force: bool = False,
    dry_run: bool = False,
) -> list[IngestionResult]:
    results: list[IngestionResult] = []
    regions = (
        [("r000_c000", bbox)]
        if dry_run and bbox == (0.0, 0.0, 0.0, 0.0)
        else regional_bboxes(bbox, settings.nasa_power.regional_chunk_degrees)
    )
    for region_id, region_bbox in regions:
        for parameter in settings.nasa_power.parameters:
            url, params = build_regional_request(settings, parameter=parameter, bbox=region_bbox)
            request_fingerprint = fingerprint({"url": url, "params": params})
            path, manifest_path = versioned_paths(
                regional_artifact_path(settings, parameter, region_id), force=force
            )
            if dry_run:
                results.append(
                    IngestionResult(SOURCE, path, manifest_path, None, None, IngestionState.PLANNED)
                )
                continue
            if not force:
                reused = reusable_result(
                    source=SOURCE,
                    storage=storage,
                    artifact_path=path,
                    manifest_path=manifest_path,
                    request_fingerprint=request_fingerprint,
                )
                if reused:
                    results.append(reused)
                    continue
            response = http.get(source=SOURCE, url=url, params=params)
            validate_response(response.content)
            results.append(
                persist_download(
                    source=SOURCE,
                    storage=storage,
                    artifact_path=path,
                    manifest_path=manifest_path,
                    content=response.content,
                    manifest={
                        "source": SOURCE,
                        "dataset": "daily_regional_meteorology",
                        "url": url,
                        "final_url": response.final_url,
                        "parameters": params,
                        "parameter": parameter,
                        "region_id": region_id,
                        "bbox": region_bbox,
                        "crs": settings.spatial.query_crs,
                        "http_status": response.status_code,
                        "content_type": response.content_type,
                        "area_type": settings.study.area_type,
                        "area_code": settings.study.area_code,
                        "area_name": settings.study.area_name,
                        "study_start_date": settings.study.start_date.isoformat(),
                        "study_end_date": settings.study.end_date.isoformat(),
                        "project_version": settings.project.version,
                        "config_sha256": settings.config_hash,
                        "request_fingerprint": request_fingerprint,
                    },
                )
            )
    return results
