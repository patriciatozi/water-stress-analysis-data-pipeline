from __future__ import annotations

from pathlib import Path

from water_stress.config import Settings
from water_stress.http import HttpGetter
from water_stress.ingestion.common import (
    fingerprint,
    persist_download_manifest,
    reusable_result,
    versioned_paths,
)
from water_stress.ingestion.soilgrids import validate_tiff
from water_stress.models import IngestionResult, IngestionState
from water_stress.storage import StorageClient

SOURCE = "mapbiomas"


def build_url(settings: Settings) -> str:
    base_url = str(settings.mapbiomas.base_url).rstrip("/")
    collection = settings.mapbiomas.collection
    year = settings.mapbiomas.reference_year
    return f"{base_url}/collection_{collection}/lulc/coverage/brazil_coverage_{year}.tif"


def artifact_path(settings: Settings) -> Path:
    collection = settings.mapbiomas.collection
    year = settings.mapbiomas.reference_year
    return (
        settings.storage.root_path
        / "mapbiomas"
        / "land_cover"
        / f"collection={collection}"
        / f"year={year}"
        / f"brazil_coverage_{year}.tif"
    )


def ingest(
    settings: Settings,
    *,
    http: HttpGetter,
    storage: StorageClient,
    force: bool = False,
    dry_run: bool = False,
) -> IngestionResult:
    url = build_url(settings)
    request_fingerprint = fingerprint({"url": url})
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

    response = http.download(source=SOURCE, url=url, storage=storage, path=path)
    try:
        validate_tiff(response.header)
    except ValueError:
        storage.delete(path)
        raise
    return persist_download_manifest(
        source=SOURCE,
        storage=storage,
        artifact_path=path,
        manifest_path=manifest_path,
        checksum=response.checksum,
        size_bytes=response.size_bytes,
        manifest={
            "source": SOURCE,
            "dataset": "annual_land_cover_classification",
            "url": url,
            "final_url": response.final_url,
            "collection": settings.mapbiomas.collection,
            "reference_year": settings.mapbiomas.reference_year,
            "target_class": "soybean",
            "target_class_code": settings.mapbiomas.soybean_class,
            "spatial_extent": "Brazil",
            "license": settings.mapbiomas.license,
            "http_status": response.status_code,
            "content_type": response.content_type,
            "area_type": settings.study.area_type,
            "area_code": settings.study.area_code,
            "area_name": settings.study.area_name,
            "project_version": settings.project.version,
            "config_sha256": settings.config_hash,
            "request_fingerprint": request_fingerprint,
        },
    )
