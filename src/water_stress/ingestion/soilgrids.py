from __future__ import annotations

from pathlib import Path
from typing import Any

from pyproj import Transformer

from water_stress.config import Settings
from water_stress.http import HttpGetter
from water_stress.ingestion import ibge
from water_stress.ingestion.common import (
    fingerprint,
    persist_download,
    reusable_result,
    versioned_paths,
)
from water_stress.models import IngestionResult, IngestionState
from water_stress.storage import StorageClient

SOURCE = "soilgrids"


def transformed_bbox(settings: Settings, boundary: bytes) -> tuple[float, float, float, float]:
    geometry = ibge.geometry_from_geojson(boundary)
    transformer = Transformer.from_crs(
        settings.soilgrids.source_crs,
        settings.soilgrids.subset_crs,
        always_xy=True,
    )
    min_x, min_y, max_x, max_y = geometry.bounds
    xs: list[float] = []
    ys: list[float] = []
    for x, y in ((min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)):
        transformed_x, transformed_y = transformer.transform(x, y)
        xs.append(transformed_x)
        ys.append(transformed_y)
    return min(xs), min(ys), max(xs), max(ys)


def build_request(
    settings: Settings,
    *,
    property_name: str,
    depth: str,
    bbox: tuple[float, float, float, float],
) -> tuple[str, dict[str, Any]]:
    min_x, min_y, max_x, max_y = bbox
    url = str(settings.soilgrids.base_url)
    params: dict[str, Any] = {
        "map": f"/map/{property_name}.map",
        "SERVICE": settings.soilgrids.service,
        "VERSION": settings.soilgrids.version,
        "REQUEST": "GetCoverage",
        "COVERAGEID": f"{property_name}_{depth}_{settings.soilgrids.quantile}",
        "FORMAT": settings.soilgrids.format,
        "SUBSET": [f"X({min_x},{max_x})", f"Y({min_y},{max_y})"],
        "SUBSETTINGCRS": "http://www.opengis.net/def/crs/EPSG/0/152160",
        "OUTPUTCRS": "http://www.opengis.net/def/crs/EPSG/0/152160",
    }
    return url, params


def artifact_path(settings: Settings, property_name: str, depth: str) -> Path:
    filename = f"{property_name}_{depth}_{settings.soilgrids.quantile}.tif"
    return (
        settings.storage.root_path
        / "soilgrids"
        / f"municipality_code={settings.study.municipality_code}"
        / f"property={property_name}"
        / f"depth={depth}"
        / filename
    )


def validate_tiff(content: bytes) -> None:
    if len(content) < 8 or content[:4] not in {b"II*\x00", b"MM\x00*"}:
        raise ValueError("SoilGrids response is not a valid TIFF")


def ingest(
    settings: Settings,
    *,
    boundary: bytes,
    http: HttpGetter,
    storage: StorageClient,
    force: bool = False,
    dry_run: bool = False,
) -> list[IngestionResult]:
    bbox = (0.0, 0.0, 0.0, 0.0) if dry_run else transformed_bbox(settings, boundary)
    results: list[IngestionResult] = []
    for property_name in settings.soilgrids.properties:
        for depth in settings.soilgrids.depths:
            url, params = build_request(
                settings, property_name=property_name, depth=depth, bbox=bbox
            )
            request_fingerprint = fingerprint({"url": url, "params": params})
            path, manifest_path = versioned_paths(
                artifact_path(settings, property_name, depth), force=force
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
            validate_tiff(response.content)
            results.append(
                persist_download(
                    source=SOURCE,
                    storage=storage,
                    artifact_path=path,
                    manifest_path=manifest_path,
                    content=response.content,
                    manifest={
                        "source": SOURCE,
                        "dataset": "soil_property",
                        "property": property_name,
                        "depth": depth,
                        "quantile": settings.soilgrids.quantile,
                        "url": url,
                        "final_url": response.final_url,
                        "parameters": params,
                        "bbox": bbox,
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
