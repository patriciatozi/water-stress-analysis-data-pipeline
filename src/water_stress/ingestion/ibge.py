from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

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

SOURCE = "ibge"


def build_request(settings: Settings) -> tuple[str, dict[str, str], dict[str, str]]:
    resource = "estados" if settings.study.area_type == "state" else "municipios"
    url = f"{str(settings.ibge.base_url).rstrip('/')}/{resource}/{settings.study.area_code}"
    params = {"formato": "application/vnd.geo+json", "qualidade": settings.ibge.quality}
    headers = {"Accept": "application/vnd.geo+json"}
    return url, params, headers


def artifact_path(settings: Settings) -> Path:
    return (
        settings.storage.root_path
        / "ibge"
        / settings.study.area_type
        / settings.study.partition_key
        / f"{settings.study.area_type}.geojson"
    )


def validate_geojson(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("IBGE response is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("type") not in {
        "Feature",
        "FeatureCollection",
        "Polygon",
        "MultiPolygon",
    }:
        raise ValueError("IBGE response is not a supported GeoJSON document")
    return document


def representative_point(content: bytes) -> tuple[float, float]:
    document = validate_geojson(content)
    if document["type"] == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list) or not features:
            raise ValueError("IBGE FeatureCollection has no features")
        geometry = shape(features[0]["geometry"])
    elif document["type"] == "Feature":
        geometry = shape(document["geometry"])
    else:
        geometry = shape(document)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("IBGE response contains an empty or invalid geometry")
    point = geometry.representative_point()
    return point.y, point.x


def geometry_from_geojson(content: bytes) -> BaseGeometry:
    document = validate_geojson(content)
    if document["type"] == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list) or not features:
            raise ValueError("IBGE FeatureCollection has no features")
        return shape(features[0]["geometry"])
    if document["type"] == "Feature":
        return shape(document["geometry"])
    return shape(document)


def ingest(
    settings: Settings,
    *,
    http: HttpGetter,
    storage: StorageClient,
    force: bool = False,
    dry_run: bool = False,
) -> IngestionResult:
    url, params, headers = build_request(settings)
    request_payload = {"url": url, "params": params, "headers": headers}
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
    response = http.get(source=SOURCE, url=url, params=params, headers=headers)
    validate_geojson(response.content)
    return persist_download(
        source=SOURCE,
        storage=storage,
        artifact_path=path,
        manifest_path=manifest_path,
        content=response.content,
        manifest={
            "source": SOURCE,
            "dataset": f"{settings.study.area_type}_boundary",
            "url": url,
            "final_url": response.final_url,
            "parameters": params,
            "http_status": response.status_code,
            "content_type": response.content_type,
            "area_type": settings.study.area_type,
            "area_code": settings.study.area_code,
            "area_name": settings.study.area_name,
            "crs": "EPSG:4326",
            "study_start_date": settings.study.start_date.isoformat(),
            "study_end_date": settings.study.end_date.isoformat(),
            "project_version": settings.project.version,
            "config_sha256": settings.config_hash,
            "request_fingerprint": request_fingerprint,
        },
    )
